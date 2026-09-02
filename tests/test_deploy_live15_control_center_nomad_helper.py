from pathlib import Path

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
