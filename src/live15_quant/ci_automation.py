"""Deterministic, no-token CI failure detection and repair planning.

The module deliberately separates observation/classification from side effects.  A caller may
provide a GitHub client and a repair executor, while tests can exercise the safety gates without
network access, shells, agents, or credentials.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class FailureClass(StrEnum):
    RUFF_FORMAT = "RUFF_FORMAT"
    RUFF_SAFE_LINT = "RUFF_SAFE_LINT"
    JSON_FORMAT = "JSON_FORMAT"
    KNOWN_NON_CODE_BLOCKER = "KNOWN_NON_CODE_BLOCKER"
    AGENT_REQUIRED = "AGENT_REQUIRED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    UNKNOWN = "UNKNOWN"


class AutomationStatus(StrEnum):
    IDLE = "IDLE"
    FAILURE_DETECTED = "FAILURE_DETECTED"
    CLASSIFIED = "CLASSIFIED"
    SAFE_AUTOFIX = "SAFE_AUTOFIX"
    VALIDATING = "VALIDATING"
    PR_READY = "PR_READY"
    WAITING_FOR_CI = "WAITING_FOR_CI"
    RESOLVED = "RESOLVED"
    AGENT_REQUIRED = "AGENT_REQUIRED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class WorkflowRun:
    run_id: int
    workflow: str
    status: str
    conclusion: str | None
    head_sha: str
    branch: str
    event: str

    @property
    def is_failed(self) -> bool:
        return self.status.lower() == "completed" and self.conclusion in {"failure", "cancelled"}


@dataclass(frozen=True)
class FailureContext:
    run_id: int
    head_sha: str
    branch: str
    job_id: int
    job_name: str
    failed_step: str
    log_excerpt: str
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class Classification:
    failure_class: FailureClass
    reason: str
    allowed_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepairPlan:
    failure_class: FailureClass
    branch: str
    commands: tuple[str, ...]
    max_files: int = 5
    max_attempts: int = 2


class RunContextClient(Protocol):
    def list_workflow_runs(self, workflow: str, limit: int = 20) -> Iterable[WorkflowRun]: ...

    def failure_context(self, run: WorkflowRun) -> FailureContext: ...


PROTECTED_MARKERS = (
    "dataset",
    "label",
    "settlement",
    "hard_risk",
    "production",
    "execution",
    "model_promotion",
    "governance",
    "protected-boundar",
)
SAFE_AUTOFIX_CLASSES = frozenset(
    {FailureClass.RUFF_FORMAT, FailureClass.RUFF_SAFE_LINT, FailureClass.JSON_FORMAT}
)
REPO_WIDE_VALIDATION_COMMANDS = (
    "ruff check .",
    "ruff format --check .",
    "pytest",
    "git diff --check",
)
MAX_REPAIR_ATTEMPTS = 2


def repair_attempt_allowed(state: dict[str, Any], fingerprint: str) -> bool:
    attempts = state.get("repair_attempts", {})
    return int(attempts.get(fingerprint, 0)) < MAX_REPAIR_ATTEMPTS


def record_repair_attempt(state: dict[str, Any], fingerprint: str) -> dict[str, Any]:
    attempts = dict(state.get("repair_attempts", {}))
    attempts[fingerprint] = int(attempts.get(fingerprint, 0)) + 1
    state["repair_attempts"] = attempts
    if attempts[fingerprint] >= MAX_REPAIR_ATTEMPTS:
        state["status"] = "CI_AUTOFIX_EXHAUSTED"
    return state


def _normalized(value: str) -> str:
    return value.replace("\\", "/").casefold()


def protected_boundary_hits(paths: Iterable[str], text: str = "") -> tuple[str, ...]:
    candidates = [_normalized(path) for path in paths]
    candidates.append(_normalized(text))
    return tuple(
        sorted(
            {marker for marker in PROTECTED_MARKERS if any(marker in item for item in candidates)}
        )
    )


def classify_failure(context: FailureContext) -> Classification:
    hits = protected_boundary_hits(
        context.paths, " ".join((context.failed_step, context.log_excerpt))
    )
    if hits:
        return Classification(FailureClass.HUMAN_REQUIRED, f"protected boundary: {', '.join(hits)}")
    step = _normalized(context.failed_step)
    log = _normalized(context.log_excerpt)
    combined = f"{step} {log}"
    if "credential" in combined or "authentication" in combined or "api unavailable" in combined:
        return Classification(
            FailureClass.KNOWN_NON_CODE_BLOCKER,
            "credential or external service unavailable",
        )
    if "ruff" in combined and "format" in combined and "check" in combined:
        return Classification(
            FailureClass.RUFF_FORMAT,
            "repository format check failed",
            context.paths,
        )
    if "ruff" in combined and "lint" in combined:
        return Classification(
            FailureClass.RUFF_SAFE_LINT,
            "Ruff lint failure requires safe-fix review",
            context.paths,
        )
    if "json" in combined and ("format" in combined or "schema" in combined):
        return Classification(
            FailureClass.JSON_FORMAT,
            "JSON formatting/schema drift",
            context.paths,
        )
    if "pytest" in combined or "test" in step:
        return Classification(FailureClass.AGENT_REQUIRED, "test or behavioral failure")
    return Classification(FailureClass.UNKNOWN, "unsupported failure signature")


def failure_fingerprint(
    context: FailureContext, classification: Classification | None = None
) -> str:
    classification = classification or classify_failure(context)
    payload = {
        "run_id": context.run_id,
        "head_sha": context.head_sha,
        "branch": context.branch,
        "job_id": context.job_id,
        "job_name": context.job_name,
        "failed_step": context.failed_step.strip().casefold(),
        "failure_class": classification.failure_class.value,
        "paths": sorted(_normalized(path) for path in context.paths),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def repair_branch_name(failure_class: str, fingerprint: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", failure_class.casefold()).strip("-") or "failure"
    return f"agent/ci-auto/{slug}-{fingerprint.casefold()[:12]}"[:80]


def build_repair_plan(context: FailureContext, classification: Classification) -> RepairPlan | None:
    if classification.failure_class not in SAFE_AUTOFIX_CLASSES:
        return None
    fingerprint = failure_fingerprint(context, classification)
    branch = repair_branch_name(classification.failure_class.value, fingerprint)
    if classification.failure_class is FailureClass.RUFF_FORMAT:
        commands = ("ruff format <confirmed-failing-paths>", "ruff format --check .")
    elif classification.failure_class is FailureClass.RUFF_SAFE_LINT:
        commands = ("ruff check --fix <confirmed-failing-paths>", "ruff check .")
    else:
        commands = ("<canonical-json-formatter> <confirmed-json-paths>", "ruff format --check .")
    return RepairPlan(classification.failure_class, branch, commands)


def make_handoff(context: FailureContext, classification: Classification) -> dict[str, Any]:
    return {
        "failure_fingerprint": failure_fingerprint(context, classification),
        "workflow_run_id": context.run_id,
        "job_id": context.job_id,
        "failed_step": context.failed_step,
        "sanitized_log_excerpt": _sanitize_log(context.log_excerpt),
        "head_sha": context.head_sha,
        "branch": context.branch,
        "classification": classification.failure_class.value,
        "suspected_file_paths": list(context.paths),
        "allowed_change_scope": "deterministic safe autofix only",
        "protected_boundaries": list(PROTECTED_MARKERS),
        "max_future_attempts": 2,
    }


def _sanitize_log(text: str) -> str:
    return re.sub(
        r"(?i)(token|password|secret|authorization)\s*[:=]\s*\S+", r"\1=[REDACTED]", text
    )[:4000]


def _without_secrets(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _without_secrets(v, k) for k, v in value.items() if not _secret_key(k)}
    if isinstance(value, list):
        return [_without_secrets(item, key) for item in value]
    if _secret_key(key):
        return "[REDACTED]"
    return value


def _secret_key(key: str) -> bool:
    return any(
        part in key.casefold() for part in ("token", "password", "secret", "credential", "pat")
    )


class CIStateStore:
    """Atomic JSON state with secret-key filtering and restart-safe defaults."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "status": AutomationStatus.IDLE.value,
                "processed_run_ids": [],
                "processed_fingerprints": [],
                "last_checked_run_id": None,
            }
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("CI automation state must be a JSON object")
        return value

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(_without_secrets(state), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(self.path)


class FailureWatcher:
    def __init__(self, client: RunContextClient, store: CIStateStore, workflow: str = "CI") -> None:
        self.client = client
        self.store = store
        self.workflow = workflow

    def check_once(self) -> dict[str, Any]:
        state = self.store.load()
        processed_ids = {int(item) for item in state.get("processed_run_ids", [])}
        processed_fingerprints = set(state.get("processed_fingerprints", []))
        runs = sorted(
            self.client.list_workflow_runs(self.workflow),
            key=lambda item: item.run_id,
            reverse=True,
        )
        for run in runs:
            if not run.is_failed or run.run_id in processed_ids:
                continue
            context = self.client.failure_context(run)
            classification = classify_failure(context)
            fingerprint = failure_fingerprint(context, classification)
            if fingerprint in processed_fingerprints:
                processed_ids.add(run.run_id)
                continue
            processed_ids.add(run.run_id)
            processed_fingerprints.add(fingerprint)
            status = (
                AutomationStatus.SAFE_AUTOFIX
                if classification.failure_class in SAFE_AUTOFIX_CLASSES
                else AutomationStatus.HUMAN_REQUIRED
                if classification.failure_class is FailureClass.HUMAN_REQUIRED
                else AutomationStatus.AGENT_REQUIRED
                if classification.failure_class is FailureClass.AGENT_REQUIRED
                else AutomationStatus.CLASSIFIED
            )
            result = {
                "status": status.value,
                "workflow": self.workflow,
                "run": asdict(run),
                "failure": asdict(context),
                "classification": classification.failure_class.value,
                "reason": classification.reason,
                "failure_fingerprint": fingerprint,
                "repair_plan": asdict(build_repair_plan(context, classification))
                if build_repair_plan(context, classification)
                else None,
                "handoff": make_handoff(context, classification),
                "processed_run_ids": sorted(processed_ids),
                "processed_fingerprints": sorted(processed_fingerprints),
            }
            self.store.save(result)
            return result
        state.update(
            {
                "status": AutomationStatus.IDLE.value,
                "processed_run_ids": sorted(processed_ids),
                "processed_fingerprints": sorted(processed_fingerprints),
                "last_checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "last_checked_run_id": runs[0].run_id if runs else None,
            }
        )
        self.store.save(state)
        return state
