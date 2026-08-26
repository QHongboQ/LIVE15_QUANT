"""Run the bounded CI-AUTO-001 watcher.

Normal operation is deterministic and does not invoke an Agent or an LLM.  The non-dry-run path
requires a clean isolated worktree, an explicit GitHub token supplied by the host auth boundary,
and never targets ``main`` for a push or merge.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from live15_quant.ci_automation import (
    CIStateStore,
    FailureContext,
    FailureWatcher,
    WorkflowRun,
    build_repair_plan,
    classify_failure,
    protected_boundary_hits,
    record_repair_attempt,
    repair_attempt_allowed,
)


class GitHubAuthUnavailable(RuntimeError):
    pass


class GitHubAPIError(RuntimeError):
    pass


@dataclass
class GitHubActionsClient:
    repository: str
    token: str | None
    api_base: str = "https://api.github.com"

    def __post_init__(self) -> None:
        if not self.token:
            raise GitHubAuthUnavailable("GITHUB_TOKEN or GH_TOKEN is not configured")

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.api_base}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "LIVE15-CI-AUTO-001",
            },
        )
        for attempt in range(2):
            try:
                with urlopen(request, timeout=30) as response:
                    raw = response.read()
                break
            except HTTPError as exc:
                retryable = method == "GET" and (exc.code == 429 or exc.code >= 500)
                if not retryable or attempt:
                    raise GitHubAPIError(
                        f"GitHub API request failed: {getattr(exc, 'code', 'network')}"
                    ) from exc
                time.sleep(1)
            except URLError as exc:
                raise GitHubAPIError("GitHub API request failed: network") from exc
        return json.loads(raw.decode("utf-8")) if raw else None

    def list_workflow_runs(self, workflow: str, limit: int = 20) -> list[WorkflowRun]:
        encoded = workflow.replace("/", "%2F")
        result = self._request(
            "GET",
            f"/repos/{self.repository}/actions/workflows/{encoded}/runs?per_page={min(limit, 100)}",
        )
        return [
            WorkflowRun(
                run_id=int(item["id"]),
                workflow=str(item.get("name", workflow)),
                status=str(item.get("status", "")),
                conclusion=item.get("conclusion"),
                head_sha=str(item.get("head_sha", "")),
                branch=str(item.get("head_branch", "")),
                event=str(item.get("event", "")),
            )
            for item in result.get("workflow_runs", [])
        ]

    def failure_context(self, run: WorkflowRun) -> FailureContext:
        jobs = self._request(
            "GET", f"/repos/{self.repository}/actions/runs/{run.run_id}/jobs?per_page=100"
        ).get("jobs", [])
        failed_job = next(
            (job for job in jobs if job.get("conclusion") in {"failure", "cancelled"}), None
        )
        if failed_job is None:
            raise GitHubAPIError(f"run {run.run_id} has no failed job")
        step = next(
            (
                step
                for step in failed_job.get("steps", [])
                if step.get("conclusion") in {"failure", "cancelled"}
            ),
            None,
        )
        failed_step = str(step.get("name", "unknown")) if step else "unknown"
        log = self._request_text(f"/repos/{self.repository}/actions/jobs/{failed_job['id']}/logs")
        paths = tuple(
            sorted(
                set(
                    re.findall(
                        r"(?<![A-Za-z0-9_])(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+",
                        log,
                    )
                )
            )[:20]
        )
        return FailureContext(
            run_id=run.run_id,
            head_sha=run.head_sha,
            branch=run.branch,
            job_id=int(failed_job["id"]),
            job_name=str(failed_job.get("name", "unknown")),
            failed_step=failed_step,
            log_excerpt=log[-4000:],
            paths=paths,
        )

    def _request_text(self, path: str) -> str:
        request = Request(
            f"{self.api_base}{path}",
            method="GET",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "LIVE15-CI-AUTO-001",
            },
        )
        for attempt in range(2):
            try:
                with urlopen(request, timeout=30) as response:
                    return response.read().decode("utf-8", errors="replace")
            except HTTPError as exc:
                retryable = exc.code == 429 or exc.code >= 500
                if not retryable or attempt:
                    raise GitHubAPIError(
                        f"GitHub log request failed: {getattr(exc, 'code', 'network')}"
                    ) from exc
                time.sleep(1)
            except URLError as exc:
                raise GitHubAPIError("GitHub log request failed: network") from exc
        raise GitHubAPIError("GitHub log request failed: retry exhausted")

    def create_pull(self, branch: str, title: str, body: str) -> str:
        result = self._request(
            "POST",
            f"/repos/{self.repository}/pulls",
            {"title": title, "head": branch, "base": "main", "body": body, "draft": False},
        )
        return str(result["html_url"])


def _run(command: list[str], root: Path) -> None:
    completed = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
    if completed.returncode:
        raise RuntimeError(
            f"command failed: {' '.join(command)}\n{completed.stdout}\n{completed.stderr}"
        )


def _repair(root: Path, client: GitHubActionsClient, context: FailureContext) -> dict[str, Any]:
    classification = classify_failure(context)
    plan = build_repair_plan(context, classification)
    if plan is None or classification.failure_class.value == "JSON_FORMAT":
        return {"status": "NOT_SAFE_AUTOFIX", "classification": classification.failure_class.value}
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, text=True, capture_output=True, check=True
    )
    current_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()
    if status.stdout.strip() or current_branch.casefold() == "main":
        return {"status": "CI_AUTOFIX_DIFF_UNSAFE", "reason": "worktree must be clean and off main"}
    safe_paths: list[str] = []
    root_resolved = root.resolve()
    for path in context.paths:
        candidate_path = Path(path)
        if not path or candidate_path.is_absolute() or ".." in candidate_path.parts:
            continue
        candidate = (root / candidate_path).resolve()
        if candidate.is_relative_to(root_resolved):
            safe_paths.append(candidate.relative_to(root_resolved).as_posix())
    if not safe_paths:
        return {"status": "CI_AUTOFIX_DIFF_UNSAFE", "reason": "no confirmed repair paths"}
    _run(["git", "switch", "-c", plan.branch], root)
    if classification.failure_class.value == "RUFF_FORMAT":
        _run(["ruff", "format", *safe_paths], root)
    elif classification.failure_class.value == "RUFF_SAFE_LINT":
        _run(["ruff", "check", "--fix", *safe_paths], root)
    else:
        return {"status": "AGENT_REQUIRED", "reason": "JSON formatter must be project-provided"}
    changed = subprocess.run(
        ["git", "diff", "--name-only"], cwd=root, text=True, capture_output=True, check=True
    ).stdout.splitlines()
    diff_text = subprocess.run(
        ["git", "diff", "--"], cwd=root, text=True, capture_output=True, check=True
    ).stdout
    if (
        len(changed) > 5
        or any(path not in safe_paths for path in changed)
        or protected_boundary_hits(changed)
        or re.search(r"(?i)(token|secret|password|authorization)\s*[:=]", diff_text)
    ):
        return {"status": "CI_AUTOFIX_DIFF_UNSAFE", "changed": changed}
    for command in (
        ("ruff", "check", "."),
        ("ruff", "format", "--check", "."),
        ("pytest",),
        ("git", "diff", "--check"),
    ):
        _run(list(command), root)
    _run(["git", "add", *changed], root)
    _run(["git", "commit", "-m", "ci: auto-fix deterministic CI failure"], root)
    _run(["git", "push", "--set-upstream", "origin", plan.branch], root)
    body = (
        f"Trigger run: {context.run_id}\nHead SHA: {context.head_sha}\n"
        f"Failure class: {classification.failure_class.value}\nFailed step: {context.failed_step}\n"
        f"Files changed: {', '.join(changed)}\nAUTO_REPAIR=true\nNo Agent/LLM used."
    )
    return {
        "status": "PR_READY",
        "branch": plan.branch,
        "pr_url": client.create_pull(plan.branch, "ci: auto-fix deterministic CI failure", body),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repo", default="QHongboQ/LIVE15_QUANT")
    parser.add_argument("--workflow", default="CI")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--state", type=Path, default=Path(".agents/state/runs/ci-auto-001.json"))
    parser.add_argument("--interval", type=int, default=300)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    try:
        client = GitHubActionsClient(args.repo, token)
        watcher = FailureWatcher(client, CIStateStore(args.state))
        while True:
            result = watcher.check_once()
            if result.get("status") == "SAFE_AUTOFIX" and not args.dry_run:
                fingerprint = str(result["failure_fingerprint"])
                state = watcher.store.load()
                if not repair_attempt_allowed(state, fingerprint):
                    result = {"status": "CI_AUTOFIX_EXHAUSTED", "failure_fingerprint": fingerprint}
                else:
                    attempt_state = record_repair_attempt(state, fingerprint)
                    watcher.store.save(attempt_state)
                    result = _repair(args.root, client, FailureContext(**result["failure"]))
                    result["failure_fingerprint"] = fingerprint
                    result["repair_attempts"] = attempt_state["repair_attempts"]
                watcher.store.save(result)
            print(json.dumps(result, indent=2, sort_keys=True))
            if not args.watch:
                return 0
            time.sleep(max(args.interval, 60))
    except GitHubAuthUnavailable as exc:
        print(json.dumps({"status": "CI_AUTOMATION_GITHUB_AUTH_UNAVAILABLE", "reason": str(exc)}))
        return 2
    except (GitHubAPIError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
