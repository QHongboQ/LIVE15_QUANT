"""Fail-closed CI validation planning for changed repository paths."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path


class ValidationTier(StrEnum):
    METADATA = "TIER_0_METADATA"
    LOCALIZED = "TIER_1_LOCALIZED"
    SUBSYSTEM = "TIER_2_SUBSYSTEM"
    HIGH_RISK = "TIER_3_HIGH_RISK"
    FULL = "TIER_4_FULL"


class TestGroup(StrEnum):
    __test__ = False

    GOVERNANCE = "governance"
    CONTROL_CENTER = "control-center"
    RECORDER_WS = "recorder-ws"
    DATA_STORAGE = "data-storage"
    RESEARCH_MODEL = "research-model"
    FULL = "full"


@dataclass(frozen=True)
class ValidationPlan:
    tier: ValidationTier
    groups: tuple[TestGroup, ...]
    full_suite: bool
    reasons: tuple[str, ...]

    @property
    def required_jobs(self) -> tuple[str, ...]:
        return ("static", *(group.value for group in self.groups))


_FULL_EXACT = frozenset(
    {
        "pyproject.toml",
        "requirements.lock",
        "conftest.py",
        "src/live15_quant/ci_plan.py",
        "tests/test_ci_plan.py",
        "src/live15_quant/records.py",
        "src/live15_quant/storage.py",
    }
)
_FULL_PREFIXES = (
    ".github/",
    "deploy/windows/",
    "tools/release_",
    "tools/bootstrap_",
    "src/live15_quant/release_",
    "src/live15_quant/risk",
    "src/live15_quant/execution",
    "src/live15_quant/settlement",
    "tests/conftest.py",
    "tests/test_ci_",
)
_GOVERNANCE_EXACT = frozenset(
    {
        "AGENTS.md",
        "BUG_REGISTRY.md",
        "CONTEXT.md",
        "CURRENT_STATE.md",
        "PROJECT_CHARTER.md",
        "README.md",
    }
)
_GROUP_PREFIXES: tuple[tuple[TestGroup, tuple[str, ...]], ...] = (
    (TestGroup.CONTROL_CENTER, ("src/live15_quant/control_center", "tests/test_control_center")),
    (
        TestGroup.RECORDER_WS,
        (
            "src/live15_quant/recorder",
            "src/live15_quant/ws_",
            "src/live15_quant/kalshi_ws",
            "src/live15_quant/providers/kalshi",
            "tests/test_recorder",
            "tests/test_ws_",
            "tests/test_kalshi_ws",
        ),
    ),
    (
        TestGroup.DATA_STORAGE,
        (
            "src/live15_quant/archive",
            "src/live15_quant/gap",
            "src/live15_quant/adaptive_retention",
            "tests/test_storage",
            "tests/test_gaps",
            "tests/test_ws_archive",
        ),
    ),
    (
        TestGroup.RESEARCH_MODEL,
        (
            "src/live15_quant/research",
            "src/live15_quant/historical",
            "src/live15_quant/factor",
            "src/live15_quant/sequence",
            "src/live15_quant/model",
            "tests/test_research",
            "tests/test_historical",
            "tests/test_factor",
            "tests/test_sequence",
            "tests/test_model",
        ),
    ),
)


def _full_plan(reason: str, tier: ValidationTier = ValidationTier.FULL) -> ValidationPlan:
    return ValidationPlan(tier=tier, groups=(TestGroup.FULL,), full_suite=True, reasons=(reason,))


def _group_for_path(path: str) -> TestGroup | None:
    if path in _GOVERNANCE_EXACT or path.startswith("docs/") or path.endswith(".md"):
        return TestGroup.GOVERNANCE
    for group, prefixes in _GROUP_PREFIXES:
        if path.startswith(prefixes):
            return group
    return None


def plan_validation(
    changed_paths: Iterable[str], *, event: str, ref: str = "", force_full: bool = False
) -> ValidationPlan:
    """Return the least validation tier justified by explicit repository policy.

    Any unrecognised event or path is deliberately escalated to the full suite.
    """

    paths = tuple(sorted({path.replace("\\", "/").lstrip("./") for path in changed_paths if path}))
    if force_full or event == "workflow_dispatch":
        return _full_plan("explicit full validation")
    if event == "push" and ref == "refs/heads/main":
        return _full_plan("authoritative main validation")
    if event != "pull_request":
        return _full_plan(f"unsupported event: {event}")
    if not paths:
        return _full_plan("empty change set")

    groups: set[TestGroup] = set()
    for path in paths:
        if path in _FULL_EXACT or path.startswith(_FULL_PREFIXES):
            tier = ValidationTier.HIGH_RISK if "release" in path else ValidationTier.FULL
            return _full_plan(f"full-suite path: {path}", tier)
        group = _group_for_path(path)
        if group is None:
            return _full_plan(f"unknown path: {path}")
        groups.add(group)

    if groups == {TestGroup.GOVERNANCE}:
        return ValidationPlan(
            tier=ValidationTier.METADATA,
            groups=(TestGroup.GOVERNANCE,),
            full_suite=False,
            reasons=("governance-only paths",),
        )
    return ValidationPlan(
        tier=ValidationTier.SUBSYSTEM,
        groups=tuple(sorted(groups, key=lambda group: group.value)),
        full_suite=False,
        reasons=("explicit subsystem mapping",),
    )


def evaluate_gate(plan: ValidationPlan, completed_jobs: dict[str, str]) -> bool:
    """Return true only when every planned validation job explicitly succeeded."""

    return all(completed_jobs.get(job) == "success" for job in plan.required_jobs)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("--ref", default="")
    parser.add_argument("--changed-paths-file", type=Path, required=True)
    parser.add_argument("--force-full", action="store_true")
    parser.add_argument("--github-output", type=Path)
    return parser


def main() -> None:
    args = _parser().parse_args()
    paths = args.changed_paths_file.read_text(encoding="utf-8").splitlines()
    plan = plan_validation(paths, event=args.event, ref=args.ref, force_full=args.force_full)
    result = {
        **asdict(plan),
        "tier": plan.tier.value,
        "groups": [group.value for group in plan.groups],
        "required_jobs": list(plan.required_jobs),
    }
    print(json.dumps(result, sort_keys=True))
    if args.github_output is not None:
        args.github_output.write_text(
            "\n".join(
                (
                    f"tier={plan.tier.value}",
                    f"groups={','.join(group.value for group in plan.groups)}",
                    f"full_suite={str(plan.full_suite).lower()}",
                )
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
