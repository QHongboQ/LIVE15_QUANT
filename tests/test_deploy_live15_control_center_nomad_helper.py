import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools" / "deploy_live15_control_center_nomad.ps1"


def _source() -> str:
    return HELPER.read_text(encoding="utf-8")


def test_control_center_nomad_helper_is_plan_only_and_localhost_scoped() -> None:
    source = _source()

    assert "[switch]$Apply" in source
    assert "http://127.0.0.1:4646" in source
    assert "if (-not $Apply)" in source
    assert "PLAN_ONLY = PASS" in source
    assert "Candidate jobspec must target only live15-control-center." in source
    assert "live15-recorder" in source


def test_control_center_nomad_helper_discovers_and_validates_live_credential_paths() -> None:
    source = _source()

    assert "/v1/job/$ControlCenterJobId" in source
    assert "LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH" in source
    assert "LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH" in source
    assert "Resolve-ReadableLeafPath" in source
    assert "[System.IO.FileAccess]::Read" in source
    assert "NOMAD_VAR_kalshi_production_api_key_id_path" in source
    assert "NOMAD_VAR_kalshi_production_private_key_path" in source
    assert "[Environment]::SetEnvironmentVariable" in source
    assert source.index("$previousEnvironment = @{}") < source.index("try {")


def test_control_center_nomad_helper_treats_valid_plan_exit_codes_correctly() -> None:
    source = _source()

    assert "$planExitCode -notin @(0, 1)" in source
    assert "PLAN_ONLY = PASS (no changes)" in source
    assert "PLAN_ONLY = PASS (changes present)" in source
    assert "Nomad job plan failed (exit=$planExitCode)." in source


def test_control_center_nomad_helper_rechecks_index_and_prevents_false_pass() -> None:
    source = _source()

    assert source.count("Get-LiveControlCenterJob -Address $NomadAddress") == 2
    assert "JobModifyIndex changed after planning; re-plan is required." in source
    assert '"-check-index=$($plannedJob.JobModifyIndex)"' in source
    assert "Nomad job run failed (exit=$LASTEXITCODE)." in source
    assert source.index("Nomad job run failed (exit=$LASTEXITCODE).") < source.index(
        "CONTROLCENTER_ROLLOUT = PASS"
    )
    assert source.count("CONTROLCENTER_ROLLOUT = PASS") == 1


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.fail("PowerShell is required to verify the deployment helper process contract")
    return executable


def _helper_process(
    tmp_path: Path,
    *,
    plan_exit: int,
    validate_exit: int = 0,
    run_exit: int = 0,
    apply: bool = False,
) -> subprocess.CompletedProcess[str]:
    candidate = tmp_path / "candidate.nomad.hcl"
    candidate.write_text('job "live15-control-center" {}\n', encoding="utf-8")
    key_id = tmp_path / "key-id.txt"
    private_key = tmp_path / "private-key.pem"
    key_id.write_text("test-key-id\n", encoding="utf-8")
    private_key.write_text("test-private-key\n", encoding="utf-8")
    fake_nomad = tmp_path / "fake-nomad.cmd"
    fake_nomad.write_text(
        "@echo off\n"
        'if /I "%2"=="validate" exit /b %LIVE15_TEST_VALIDATE_EXIT%\n'
        'if /I "%2"=="plan" exit /b %LIVE15_TEST_PLAN_EXIT%\n'
        'if /I "%2"=="run" exit /b %LIVE15_TEST_RUN_EXIT%\n'
        "exit /b 9\n",
        encoding="utf-8",
    )
    wrapper = tmp_path / "invoke-helper.ps1"
    wrapper.write_text(
        "param([string]$Helper, [string]$Candidate, [string]$Nomad, [string]$KeyId, "
        "[string]$PrivateKey, [string]$Apply)\n"
        "function Invoke-RestMethod {\n"
        "  [pscustomobject]@{ ID='live15-control-center'; JobModifyIndex=[int64]42; TaskGroups=@(\n"
        "    [pscustomobject]@{ Name='control-center'; Tasks=@(\n"
        "      [pscustomobject]@{ Name='control-center'; Env=[pscustomobject]@{\n"
        "        LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH=$KeyId\n"
        "        LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH=$PrivateKey\n"
        "      }}\n"
        "    )}\n"
        "  )}\n"
        "}\n"
        "if ($Apply -eq 'true') {\n"
        "  & $Helper -CandidatePath $Candidate -NomadPath $Nomad -Apply\n"
        "} else {\n"
        "  & $Helper -CandidatePath $Candidate -NomadPath $Nomad\n"
        "}\n"
        "exit $LASTEXITCODE\n",
        encoding="utf-8",
    )
    environment = os.environ | {
        "LIVE15_TEST_PLAN_EXIT": str(plan_exit),
        "LIVE15_TEST_VALIDATE_EXIT": str(validate_exit),
        "LIVE15_TEST_RUN_EXIT": str(run_exit),
    }
    return subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(wrapper),
            "-Helper",
            str(HELPER),
            "-Candidate",
            str(candidate),
            "-Nomad",
            str(fake_nomad),
            "-KeyId",
            str(key_id),
            "-PrivateKey",
            str(private_key),
            "-Apply",
            "true" if apply else "false",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


@pytest.mark.parametrize("plan_exit, expected_message", [(0, "no changes"), (1, "changes present")])
def test_control_center_nomad_helper_plan_only_returns_process_success(
    tmp_path: Path, plan_exit: int, expected_message: str
) -> None:
    result = _helper_process(tmp_path, plan_exit=plan_exit)

    assert result.returncode == 0, result.stderr
    assert f"PLAN_ONLY = PASS ({expected_message})" in result.stdout


@pytest.mark.parametrize(
    ("plan_exit", "validate_exit", "run_exit", "apply"),
    [(2, 0, 0, False), (0, 2, 0, False), (0, 0, 2, True)],
)
def test_control_center_nomad_helper_failure_paths_remain_nonzero(
    tmp_path: Path, plan_exit: int, validate_exit: int, run_exit: int, apply: bool
) -> None:
    result = _helper_process(
        tmp_path, plan_exit=plan_exit, validate_exit=validate_exit, run_exit=run_exit, apply=apply
    )

    assert result.returncode != 0
