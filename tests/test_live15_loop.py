from __future__ import annotations

import json
from pathlib import Path

from tools.live15_loop import make_state, protected_paths, validate_contract, write_state


def _contract() -> dict[str, object]:
    return {
        "task_id": "TEST-001",
        "title": "test",
        "task_type": "test",
        "objective": "test",
        "allowed_scope": ["tools/"],
        "forbidden_scope": ["src/live15_quant/"],
        "acceptance_criteria": ["pass"],
        "validation_commands": ["pytest"],
        "risk_level": "L1",
        "change_budget": {
            "max_files": 5,
            "max_added_lines": 200,
            "max_deleted_lines": 200,
            "hard": True,
        },
        "max_iterations": 3,
        "human_approval_requirements": [],
        "expected_output": "pass",
        "rollback_expectation": "none",
    }


def test_contract_validation_and_protected_boundary() -> None:
    assert validate_contract(_contract()) == []
    rules = {
        "rules": [{"name": "recorder", "patterns": ["src/live15_quant/*"], "minimum_risk": "L3"}]
    }
    assert protected_paths(["src/live15_quant/recorder.py"], "L1", rules)
    assert protected_paths(["src/live15_quant/recorder.py"], "L3", rules) == []


def test_state_write_is_machine_readable(tmp_path: Path) -> None:
    target = tmp_path / "runs" / "state.json"
    write_state(target, make_state("TEST-001", "PASS", next_action="review"))
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["task_id"] == "TEST-001"
    assert payload["status"] == "PASS"
