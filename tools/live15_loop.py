"""Bounded, local LOOP-001 contract and state helper.

This tool intentionally does not invoke shells, agents, GitHub, schedulers, or deployment. It
validates contracts, applies path-based protected-boundary checks, and records a transparent dry
run so a human can orchestrate Maker and Checker steps safely.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_REQUIRED = {
    "task_id",
    "title",
    "task_type",
    "objective",
    "allowed_scope",
    "forbidden_scope",
    "acceptance_criteria",
    "validation_commands",
    "risk_level",
    "change_budget",
    "max_iterations",
    "human_approval_requirements",
    "expected_output",
    "rollback_expectation",
}
RISK_LEVELS = {"L0", "L1", "L2", "L3", "L4"}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def validate_contract(contract: dict[str, Any]) -> list[str]:
    errors = sorted(CONTRACT_REQUIRED - contract.keys())
    if errors:
        return [f"missing required fields: {', '.join(errors)}"]
    if contract["risk_level"] not in RISK_LEVELS:
        return [f"invalid risk_level: {contract['risk_level']}"]
    if not isinstance(contract["max_iterations"], int) or not 1 <= contract["max_iterations"] <= 3:
        return ["max_iterations must be an integer from 1 through 3"]
    budget = contract["change_budget"]
    if not isinstance(budget, dict):
        return ["change_budget must be an object"]
    for key in ("max_files", "max_added_lines", "max_deleted_lines", "hard"):
        if key not in budget:
            errors.append(f"change_budget missing {key}")
    if errors:
        return errors
    if any(
        not isinstance(budget[key], int) or budget[key] < 0
        for key in ("max_files", "max_added_lines", "max_deleted_lines")
    ):
        return ["change budget counts must be non-negative integers"]
    if not isinstance(budget["hard"], bool):
        return ["change_budget.hard must be boolean"]
    return []


def _matches(path: str, pattern: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    candidate = pattern.replace("\\", "/")
    short_candidate = candidate[3:] if candidate.startswith("**/") else candidate
    return fnmatch.fnmatch(normalized, candidate) or fnmatch.fnmatch(normalized, short_candidate)


def protected_paths(changed: list[str], risk: str, rules: dict[str, Any]) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for item in rules.get("rules", []):
        for path in changed:
            if any(_matches(path, pattern) for pattern in item.get("patterns", [])):
                hits.append(
                    {"path": path, "rule": item["name"], "minimum_risk": item["minimum_risk"]}
                )
    return [] if risk in {"L3", "L4"} else hits


def make_state(
    task_id: str, status: str, *, iteration: int = 0, next_action: str = ""
) -> dict[str, Any]:
    timestamp = _now()
    return {
        "task_id": task_id,
        "iteration": iteration,
        "status": status,
        "timestamps": {status.lower(): timestamp},
        "maker": {"status": "NOT_STARTED"},
        "validation": {"status": "NOT_STARTED"},
        "checker": {"status": "NOT_STARTED"},
        "previous_failure_reason": None,
        "commit": None,
        "next_action": next_action,
        "blocker": None,
    }


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def command_validate(args: argparse.Namespace) -> int:
    contract = _load(Path(args.contract))
    errors = validate_contract(contract)
    print(
        json.dumps(
            {
                "status": "INVALID" if errors else "VALID",
                "task_id": contract.get("task_id"),
                "risk_level": contract.get("risk_level"),
                "errors": errors,
            },
            indent=2,
        )
    )
    return 1 if errors else 0


def command_classify(args: argparse.Namespace) -> int:
    contract = _load(Path(args.contract))
    rules = _load(Path(args.rules))
    changed = list(args.path)
    hits = protected_paths(changed, contract["risk_level"], rules)
    verdict = "HUMAN_REVIEW_REQUIRED" if hits else "IN_SCOPE"
    print(
        json.dumps({"verdict": verdict, "changed_paths": changed, "protected_hits": hits}, indent=2)
    )
    return 2 if hits else 0


def command_dry_run(args: argparse.Namespace) -> int:
    contract = _load(Path(args.contract))
    errors = validate_contract(contract)
    if errors:
        print(json.dumps({"status": "BLOCKED", "errors": errors}, indent=2))
        return 1
    state_path = Path(args.state)
    for status in ("CREATED", "VALIDATING", "CHECKER_RUNNING", "PASS"):
        write_state(
            state_path,
            make_state(
                contract["task_id"],
                status,
                next_action="manual Maker/Checker orchestration required"
                if status == "PASS"
                else "continue dry run",
            ),
        )
    final = _load(state_path)
    print(
        json.dumps(
            {"status": final["status"], "task_id": final["task_id"], "state": str(state_path)},
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-contract")
    validate.add_argument("--contract", required=True)
    validate.set_defaults(handler=command_validate)
    classify = subparsers.add_parser("classify-diff")
    classify.add_argument("--contract", required=True)
    classify.add_argument("--rules", default=str(ROOT / ".agents/loop/protected-boundaries.json"))
    classify.add_argument("path", nargs="+")
    classify.set_defaults(handler=command_classify)
    dry_run = subparsers.add_parser("dry-run")
    dry_run.add_argument("--contract", required=True)
    dry_run.add_argument("--state", required=True)
    dry_run.set_defaults(handler=command_dry_run)
    return parser


if __name__ == "__main__":
    parser = build_parser()
    arguments = parser.parse_args()
    raise SystemExit(arguments.handler(arguments))
