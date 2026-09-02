from __future__ import annotations

import re
import sys
from pathlib import Path

from live15_quant import ci_plan
from live15_quant.ci_plan import TestGroup, ValidationTier, evaluate_gate, plan_validation


def _workflow_step_body(step_name: str) -> str:
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    step = re.search(
        rf"(?ms)^      - name: {re.escape(step_name)}\r?\n.*?^        run: \|\r?\n"
        rf"(?P<body>.*?)(?=^      - name:|\Z)",
        workflow,
    )
    assert step, f"CI workflow is missing the {step_name!r} step"
    return step.group("body")


def test_tracker_only_change_uses_governance_fast_path() -> None:
    plan = plan_validation(("BUG_REGISTRY.md", "docs/agents/domain.md"), event="pull_request")

    assert plan.tier is ValidationTier.METADATA
    assert plan.groups == (TestGroup.GOVERNANCE,)
    assert not plan.full_suite


def test_localized_control_center_change_selects_its_contract_group() -> None:
    plan = plan_validation(("src/live15_quant/control_center_service.py",), event="pull_request")

    assert plan.tier is ValidationTier.SUBSYSTEM
    assert plan.groups == (TestGroup.CONTROL_CENTER,)
    assert not plan.full_suite


def test_release_runtime_change_forces_full_suite() -> None:
    plan = plan_validation(("src/live15_quant/release_pipeline.py",), event="pull_request")

    assert plan.tier is ValidationTier.HIGH_RISK
    assert plan.full_suite


def test_shared_core_change_forces_full_suite() -> None:
    plan = plan_validation(("src/live15_quant/records.py",), event="pull_request")

    assert plan.tier is ValidationTier.FULL
    assert plan.full_suite


def test_lockfile_and_workflow_changes_force_full_suite() -> None:
    for path in ("requirements.lock", ".github/workflows/ci.yml"):
        assert plan_validation((path,), event="pull_request").full_suite


def test_shared_fixture_and_unknown_path_force_full_suite() -> None:
    for path in ("tests/conftest.py", "unmapped/new_component.py"):
        assert plan_validation((path,), event="pull_request").full_suite


def test_test_only_change_uses_its_explicit_group() -> None:
    plan = plan_validation(("tests/test_control_center.py",), event="pull_request")

    assert plan.groups == (TestGroup.CONTROL_CENTER,)
    assert not plan.full_suite


def test_main_push_and_workflow_dispatch_force_full_suite() -> None:
    assert plan_validation(
        ("docs/agents/domain.md",), event="push", ref="refs/heads/main"
    ).full_suite
    assert plan_validation(("docs/agents/domain.md",), event="workflow_dispatch").full_suite


def test_empty_change_set_fails_closed_to_full_suite() -> None:
    assert plan_validation((), event="pull_request").full_suite


def test_final_gate_rejects_missing_or_failed_planned_job() -> None:
    plan = plan_validation(("src/live15_quant/control_center_service.py",), event="pull_request")

    assert not evaluate_gate(plan, {"static": "success"})
    assert not evaluate_gate(plan, {"static": "success", "control-center": "failure"})
    assert evaluate_gate(plan, {"static": "success", "control-center": "success"})


def test_cli_writes_github_outputs_for_a_governance_plan(tmp_path: Path, monkeypatch) -> None:
    changed_paths = tmp_path / "paths.txt"
    changed_paths.write_text("docs/agents/domain.md\n")
    output = tmp_path / "github-output.txt"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ci_plan",
            "--event",
            "pull_request",
            "--changed-paths-file",
            str(changed_paths),
            "--github-output",
            str(output),
        ],
    )

    ci_plan.main()

    assert output.read_text(encoding="utf-8") == (
        "tier=TIER_0_METADATA\ngroups=governance\nfull_suite=false\n"
    )


def test_workflow_uses_pr_and_main_push_without_feature_push_duplication() -> None:
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )

    assert "pull_request:" in workflow
    assert "branches: [main]" in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow
    assert "CI Gate" in workflow


def test_frontend_release_build_is_pinned_and_fails_closed() -> None:
    body = _workflow_step_body("Build deterministic release frontend")

    assert "$pnpmVersion = '11.19.0'" in body
    assert 'corepack install --global "pnpm@$pnpmVersion"' in body
    assert 'Write-Host "pnpm version = $actualPnpmVersion"' in body
    assert "function Invoke-Native" in body
    assert "if ($LASTEXITCODE -ne 0)" in body
    assert "pnpm --dir frontend" not in body

    required_commands = (
        "pnpm install --frozen-lockfile",
        "pnpm run typecheck",
        "pnpm run lint",
        "pnpm run build:release",
        "git diff --exit-code -- src/live15_quant/terminal",
    )
    command_positions = [body.index(command) for command in required_commands]
    assert command_positions == sorted(command_positions)


def test_research_model_workflow_expands_model_files_and_fails_closed() -> None:
    body = _workflow_step_body("Research and model tests")

    assert "Get-ChildItem -LiteralPath tests -Filter 'test_model*.py' -File" in body
    assert "Sort-Object Name" in body
    assert "if ($modelTests.Count -eq 0)" in body
    assert "throw 'No model tests matched tests/test_model*.py'" in body
    assert "& pytest @tests" in body
    assert "if ($LASTEXITCODE -ne 0)" in body
    assert "exit $LASTEXITCODE" in body
