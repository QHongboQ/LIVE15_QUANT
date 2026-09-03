import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools" / "deploy_live15_recorder_nomad.ps1"
JOBSPEC = ROOT / "deploy" / "nomad" / "live15-recorder.nomad.hcl"


def _source() -> str:
    return HELPER.read_text(encoding="utf-8")


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.fail("PowerShell is required to verify the Recorder deployment helper")
    return executable


def _library_helper(tmp_path: Path) -> Path:
    marker = "$nomadVariableNames = @("
    source = _source()
    assert marker in source
    helper = tmp_path / "deploy-helper-library.ps1"
    helper.write_text(source.replace(marker, f"return\n\n{marker}", 1), encoding="utf-8")
    return helper


def test_recorder_deploy_helper_is_localhost_scoped_and_requires_exact_reviewed_sha() -> None:
    source = _source()

    assert "[ValidatePattern('^[0-9a-fA-F]{40}$')]" in source
    assert "http://127.0.0.1:4646" in source
    assert "merge-base --is-ancestor $GitSha origin/main" in source
    assert "Repository must be clean." in source
    assert "$ExpectedRuntimeSha256" in source
    assert "Canonical runtime SHA-256 mismatch." in source


def test_preview_is_non_mutating_and_apply_is_explicit() -> None:
    source = _source()

    assert "[switch]$Preview" in source
    assert "if ($Apply -and $Preview)" in source
    assert "if (-not $Apply)" in source
    assert "mode='PREVIEW'" in source
    assert "mutation='NONE'" in source
    assert source.index("if (-not $Apply)") < source.index("job run")
    assert "$previous.ReleaseId" in source
    assert "$next.ReleaseId" in source


def test_get_identity_returns_one_object_when_package_verification_writes_stdout(
    tmp_path: Path,
) -> None:
    helper = _library_helper(tmp_path)
    release_root = tmp_path / "releases"
    release_id = "live15-test"
    release = release_root / "releases" / release_id
    (release / "app").mkdir(parents=True)
    (release / "release-manifest.json").write_text(
        json.dumps({"release_id": release_id, "git_commit_sha": "a" * 40}), encoding="utf-8"
    )
    runtime = tmp_path / "noisy-runtime.cmd"
    runtime.write_text("@echo verify-package normal stdout\n@exit /b 0\n", encoding="utf-8")
    wrapper = tmp_path / "assert-identity.ps1"
    wrapper.write_text(
        "param([string]$Helper, [string]$Repository, [string]$ReleaseRoot, [string]$Runtime)\n"
        ". $Helper -Repository $Repository -ReleaseRoot $ReleaseRoot -RuntimePython $Runtime\n"
        f"$identities = @(Get-Identity '{release_id}')\n"
        'if ($identities.Count -ne 1) { throw "identity count=$($identities.Count)" }\n'
        "$identity = $identities[0]\n"
        "if ($null -eq $identity.ReleaseId -or $null -eq $identity.AppRoot -or "
        "$null -eq $identity.Manifest -or $null -eq $identity.ManifestSha256) "
        "{ throw 'identity shape invalid' }\n"
        "$identity | ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(wrapper),
            "-Helper",
            str(helper),
            "-Repository",
            str(ROOT),
            "-ReleaseRoot",
            str(release_root),
            "-Runtime",
            str(runtime),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "verify-package normal stdout" in result.stdout
    identity = json.loads(result.stdout.strip().splitlines()[-1])
    assert identity["ReleaseId"] == release_id
    assert identity["AppRoot"].endswith("releases\\live15-test\\app")


def test_jobspec_resolution_uses_repository_default_and_preserves_explicit_path(
    tmp_path: Path,
) -> None:
    helper = _library_helper(tmp_path)
    explicit = tmp_path / "explicit.nomad.hcl"
    explicit.write_text('job "live15-recorder" {}\n', encoding="utf-8")
    wrapper = tmp_path / "assert-jobspec.ps1"
    wrapper.write_text(
        "param([string]$Helper, [string]$Repository, [string]$Explicit)\n"
        ". $Helper -Repository $Repository\n"
        "[pscustomobject]@{ default = Resolve-Jobspec $null; "
        "explicit = Resolve-Jobspec $Explicit } | "
        "ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(wrapper),
            "-Helper",
            str(helper),
            "-Repository",
            str(ROOT),
            "-Explicit",
            str(explicit),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    resolved = json.loads(result.stdout)
    assert Path(resolved["default"]) == JOBSPEC
    assert Path(resolved["explicit"]) == explicit


def test_helper_preserves_live_recorder_configuration_and_archive_stays_disabled() -> None:
    source = _source()
    jobspec = JOBSPEC.read_text(encoding="utf-8")

    for variable in (
        "LIVE15_RECORDER_DATA_PATH",
        "LIVE15_RECORDER_HEALTH_PATH",
        "LIVE15_RECORDER_CONTROL_PATH",
        "LIVE15_RECORDER_PID_PATH",
        "LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH",
        "LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH",
        "LIVE15_PYTH_API_KEY_PATH",
    ):
        assert variable in source
        assert variable in jobspec
    assert 'LIVE15_ENABLE_WS_ARCHIVE                   = "false"' in jobspec
    assert 'LIVE15_ENABLE_ADAPTIVE_WS_RETENTION        = "false"' in jobspec


def test_helper_has_single_writer_and_failed_deploy_safety_gates() -> None:
    source = _source()

    assert "function Assert-OneRecorderWriter" in source
    assert "/v1/job/$JobId/allocations" in source
    assert source.count("Assert-OneRecorderWriter") >= 3
    assert "JobModifyIndex -ne $live.JobModifyIndex" in source
    assert '"-check-index=$($live.JobModifyIndex)"' in source
    assert "Recorder did not return to synchronized/fresh/no-drop-regression health" in source
    assert "kalshi_ws_queue_dropped" in source


def test_receipt_rollback_is_exact_and_package_verification_precedes_use() -> None:
    source = _source()

    assert "Rollback requires an existing deployment ReceiptPath." in source
    assert "GitSha is required unless Rollback is selected." in source
    assert "Rollback receipt does not describe the live Recorder release." in source
    assert "$receipt.new_release_id -ne $previous.ReleaseId" in source
    assert "Get-Identity ([string]$receipt.previous_release_id)" in source
    assert "Invoke-ReleasePipeline @('verify-package'" in source
    assert "previous_recorder_app_root" in source
    assert "new_recorder_app_root" in source


def test_jobspec_remains_the_existing_single_recorder_nomad_job() -> None:
    jobspec = JOBSPEC.read_text(encoding="utf-8")

    assert 'job "live15-recorder"' in jobspec
    assert 'group "recorder"' in jobspec
    assert 'task "recorder"' in jobspec
    assert "count" not in jobspec


def test_helper_parses_in_powershell() -> None:
    command = (
        "$tokens = $null; "
        "$errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{HELPER}', [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
