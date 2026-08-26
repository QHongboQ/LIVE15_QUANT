from __future__ import annotations

import json

from live15_quant.ci_automation import (
    CIStateStore,
    FailureClass,
    FailureContext,
    FailureWatcher,
    WorkflowRun,
    classify_failure,
    failure_fingerprint,
    record_repair_attempt,
    repair_attempt_allowed,
    repair_branch_name,
)


def _context(**overrides: object) -> FailureContext:
    values: dict[str, object] = {
        "run_id": 123,
        "head_sha": "a" * 40,
        "branch": "agent/example",
        "job_id": 456,
        "job_name": "quality",
        "failed_step": "Format check",
        "log_excerpt": "ruff format --check . failed",
        "paths": ("tools/example.py",),
    }
    values.update(overrides)
    return FailureContext(**values)


def test_classifier_routes_supported_and_protected_failures() -> None:
    assert classify_failure(_context()).failure_class is FailureClass.RUFF_FORMAT
    assert (
        classify_failure(
            _context(failed_step="Lint", log_excerpt="ruff check . failed")
        ).failure_class
        is FailureClass.RUFF_SAFE_LINT
    )
    assert (
        classify_failure(_context(failed_step="Test", log_excerpt="pytest failed")).failure_class
        is FailureClass.AGENT_REQUIRED
    )
    assert (
        classify_failure(_context(paths=("src/live15_quant/dataset.py",))).failure_class
        is FailureClass.HUMAN_REQUIRED
    )
    assert (
        classify_failure(
            _context(failed_step="Mystery", log_excerpt="unexpected failure")
        ).failure_class
        is FailureClass.UNKNOWN
    )


def test_failure_fingerprint_is_stable_and_excludes_log_noise() -> None:
    first = failure_fingerprint(_context(log_excerpt="line 1\nline 2"))
    second = failure_fingerprint(_context(log_excerpt="line 3\nline 4"))
    assert first == second
    assert len(first) == 64


def test_workflow_run_identity_and_duplicate_suppression() -> None:
    run = WorkflowRun(
        run_id=99,
        workflow="CI",
        status="completed",
        conclusion="failure",
        head_sha="b" * 40,
        branch="main",
        event="push",
    )
    state = {"processed_run_ids": [99], "processed_fingerprints": []}
    assert run.is_failed
    assert run.run_id in state["processed_run_ids"]


def test_state_store_round_trips_json_without_secrets(tmp_path) -> None:
    path = tmp_path / "ci-auto.json"
    store = CIStateStore(path)
    state = {"status": "FAILURE_DETECTED", "repair_attempts": 1, "token": "must-not-be-written"}
    store.save(state)
    loaded = store.load()
    assert loaded["status"] == "FAILURE_DETECTED"
    raw = path.read_text(encoding="utf-8")
    assert "must-not-be-written" not in raw
    assert json.loads(raw)["repair_attempts"] == 1


def test_branch_name_is_bounded_and_deterministic() -> None:
    name = repair_branch_name("Ruff format", "A" * 64)
    assert name == "agent/ci-auto/ruff-format-" + "a" * 12
    assert len(name) <= 80


def test_repair_attempts_stop_at_two() -> None:
    state: dict[str, object] = {}
    fingerprint = "f" * 64
    assert repair_attempt_allowed(state, fingerprint)
    record_repair_attempt(state, fingerprint)
    assert repair_attempt_allowed(state, fingerprint)
    record_repair_attempt(state, fingerprint)
    assert not repair_attempt_allowed(state, fingerprint)
    assert state["status"] == "CI_AUTOFIX_EXHAUSTED"


def test_watcher_persists_one_failure_and_suppresses_repeat(tmp_path) -> None:
    class Client:
        def list_workflow_runs(self, workflow: str, limit: int = 20):
            return [WorkflowRun(7, workflow, "completed", "failure", "c" * 40, "main", "push")]

        def failure_context(self, run: WorkflowRun) -> FailureContext:
            return _context(run_id=run.run_id, head_sha=run.head_sha)

    watcher = FailureWatcher(Client(), CIStateStore(tmp_path / "state.json"))
    first = watcher.check_once()
    second = watcher.check_once()
    assert first["status"] == "SAFE_AUTOFIX"
    assert second["status"] == "IDLE"
    assert second["processed_run_ids"] == [7]
    assert first["failure_fingerprint"] == second["failure_fingerprint"]
