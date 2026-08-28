from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from live15_quant.release_pipeline import (
    ACTIVE_POINTER,
    PREVIOUS_POINTER,
    ReleaseError,
    activate_release,
    active_release,
    build_release,
    capture_legacy_unproven_release,
    rollback_release,
    stage_bootstrap,
    verify_bootstrap,
    verify_package,
    verify_runtime_provenance,
)


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "test")
    (repository / "src/live15_quant").mkdir(parents=True)
    (repository / "src/live15_quant/__init__.py").write_text("VALUE = 'A'\n")
    (repository / "requirements.lock").write_text("example==1.0\n")
    (repository / "pyproject.toml").write_text("[build-system]\nrequires=[]\n")
    (repository / "tools").mkdir()
    (repository / "tools/release_runner.py").write_text("# stable bootstrap\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial")
    return repository, _git(repository, "rev-parse", "HEAD")


def _next_commit(repository: Path, value: str) -> str:
    (repository / "src/live15_quant/__init__.py").write_text(f"VALUE = {value!r}\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", f"release {value}")
    return _git(repository, "rev-parse", "HEAD")


def test_build_records_exact_sha_and_deterministic_manifest(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    release_root = tmp_path / "releases-one"
    identity = build_release(
        repository=repository,
        git_sha=commit,
        release_root=release_root,
        created_at="2026-08-28T00:00:00+00:00",
    )
    manifest = json.loads(
        (release_root / "releases" / identity.release_id / "release-manifest.json").read_text()
    )
    assert identity.git_commit_sha == commit
    assert manifest["git_commit_sha"] == commit
    assert manifest["requirements_lock_sha256"]
    assert manifest["artifact_manifest_sha256"]

    second_root = tmp_path / "releases-two"
    other = build_release(
        repository=repository,
        git_sha=commit,
        release_root=second_root,
        created_at="2026-08-28T00:00:00+00:00",
    )
    assert other.release_id == identity.release_id
    assert (second_root / "releases" / other.release_id / "release-manifest.json").read_text() == (
        release_root / "releases" / identity.release_id / "release-manifest.json"
    ).read_text()


def test_dirty_or_wrong_sha_is_rejected(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    release_root = tmp_path / "releases"
    (repository / "untracked.txt").write_text("must not package\n")
    with pytest.raises(ReleaseError, match="dirty"):
        build_release(repository=repository, git_sha=commit, release_root=release_root)
    (repository / "untracked.txt").unlink()
    with pytest.raises(ReleaseError):
        build_release(repository=repository, git_sha="0" * 40, release_root=release_root)


def test_mutable_data_and_secrets_are_rejected(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    (repository / "data").mkdir()
    (repository / "data/live15.sqlite3").write_text("not release content")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "bad mutable data")
    with pytest.raises(ReleaseError, match="mutable path"):
        build_release(
            repository=repository,
            git_sha=_git(repository, "rev-parse", "HEAD"),
            release_root=tmp_path / "releases",
        )


def test_corrupt_or_missing_package_fails_closed_without_changing_active(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    release_root = tmp_path / "releases"
    first = build_release(repository=repository, git_sha=commit, release_root=release_root)
    assert activate_release(release_root=release_root, release_id=first.release_id) == first
    second_commit = _next_commit(repository, "B")
    second = build_release(repository=repository, git_sha=second_commit, release_root=release_root)
    pointer_before = (release_root / ACTIVE_POINTER).read_text()
    payload = release_root / "releases" / second.release_id / "app/src/live15_quant/__init__.py"
    payload.write_text("corrupt\n")
    with pytest.raises(ReleaseError, match="payload hash mismatch"):
        activate_release(release_root=release_root, release_id=second.release_id)
    assert active_release(release_root=release_root) == first
    assert (release_root / ACTIVE_POINTER).read_text() == pointer_before
    with pytest.raises(ReleaseError):
        verify_package(release_root=release_root, release_id="live15-missing")


def test_stage_activate_rollback_and_dry_run_are_pointer_atomic(tmp_path: Path) -> None:
    repository, first_commit = _repository(tmp_path)
    release_root = tmp_path / "releases"
    first = build_release(repository=repository, git_sha=first_commit, release_root=release_root)
    second_commit = _next_commit(repository, "B")
    second = build_release(repository=repository, git_sha=second_commit, release_root=release_root)
    assert active_release(release_root=release_root) is None
    assert activate_release(release_root=release_root, release_id=first.release_id) == first
    pointer_before = (release_root / ACTIVE_POINTER).read_text()
    assert (
        activate_release(release_root=release_root, release_id=second.release_id, dry_run=True)
        == second
    )
    assert (release_root / ACTIVE_POINTER).read_text() == pointer_before
    assert activate_release(release_root=release_root, release_id=second.release_id) == second
    assert (release_root / PREVIOUS_POINTER).is_file()
    assert rollback_release(release_root=release_root) == first
    assert active_release(release_root=release_root) == first


def test_runtime_provenance_requires_active_release_paths(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    release_root = tmp_path / "releases"
    identity = build_release(repository=repository, git_sha=commit, release_root=release_root)
    activate_release(release_root=release_root, release_id=identity.release_id)
    stage_bootstrap(release_root=release_root, release_id=identity.release_id)
    assert verify_bootstrap(release_root=release_root) == identity
    app = release_root / "releases" / identity.release_id / "app"
    interpreter = tmp_path / "python.exe"
    interpreter.write_text("placeholder")
    runtime = release_root / "runtime"
    runtime.mkdir()
    (runtime / "release-runtime-recorder.json").write_text(
        json.dumps(
            {
                "component": "recorder",
                "pid": 99,
                "parent_pid": 1,
                "interpreter_path": str(interpreter),
                "working_directory": str(app),
                "module_root": str(app / "src/live15_quant"),
                "deployment_release_id": identity.release_id,
                "deployment_git_sha": identity.git_commit_sha,
                "deployment_manifest_sha256": identity.manifest_sha256,
            }
        )
    )
    service_xml = release_root / ".local-tools/winsw/LIVE15Recorder.xml"
    service_xml.parent.mkdir(parents=True)
    service_xml.write_text(
        "<service><id>LIVE15Recorder</id>"
        "<arguments>%BASE%\\..\\..\\bootstrap\\release_runner.py --component recorder</arguments>"
        "<workingdirectory>%BASE%\\..\\..</workingdirectory>"
        "</service>"
    )
    assert (
        verify_runtime_provenance(
            release_root=release_root,
            service_name="LIVE15Recorder",
            service_pid=1,
            runner_pid=99,
            service_config_path=service_xml,
            expected_git_sha=identity.git_commit_sha,
        )
        == identity
    )
    service_xml.write_text(
        "<service><id>LIVE15Recorder</id><arguments>direct</arguments></service>"
    )
    with pytest.raises(ReleaseError, match="does not bind"):
        verify_runtime_provenance(
            release_root=release_root,
            service_name="LIVE15Recorder",
            service_pid=1,
            runner_pid=99,
            service_config_path=service_xml,
            expected_git_sha=identity.git_commit_sha,
        )
    service_xml.write_text(
        "<service><id>LIVE15Recorder</id>"
        "<arguments>D:\\wrong\\bootstrap\\release_runner.py --component recorder</arguments>"
        "<workingdirectory>%BASE%\\..\\..</workingdirectory>"
        "</service>"
    )
    with pytest.raises(ReleaseError, match="different bootstrap"):
        verify_runtime_provenance(
            release_root=release_root,
            service_name="LIVE15Recorder",
            service_pid=1,
            runner_pid=99,
            service_config_path=service_xml,
            expected_git_sha=identity.git_commit_sha,
        )
    service_xml.write_text(
        "<service><id>LIVE15Recorder</id>"
        "<arguments>%BASE%\\..\\..\\bootstrap\\release_runner.py --component recorder</arguments>"
        "<workingdirectory>%BASE%\\..\\..</workingdirectory>"
        "</service>"
    )
    with pytest.raises(ReleaseError, match="runner receipt"):
        verify_runtime_provenance(
            release_root=release_root,
            service_name="LIVE15Recorder",
            service_pid=1,
            runner_pid=100,
            service_config_path=service_xml,
            expected_git_sha=identity.git_commit_sha,
        )


def test_legacy_rollback_capture_never_invents_a_git_sha_or_copies_mutable_data(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy"
    (legacy / "src/live15_quant").mkdir(parents=True)
    (legacy / "src/live15_quant/__init__.py").write_text("legacy\n")
    (legacy / "requirements.lock").write_text("example==1.0\n")
    (legacy / "data").mkdir()
    (legacy / "data/live15.sqlite3").write_text("mutable")
    (legacy / ".secrets").mkdir()
    (legacy / ".secrets/key.txt").write_text("secret")
    release_root = tmp_path / "releases"
    identity = capture_legacy_unproven_release(
        legacy_app_root=legacy,
        release_root=release_root,
        created_at="2026-08-28T00:00:00+00:00",
    )
    manifest = json.loads(
        (release_root / "releases" / identity.release_id / "release-manifest.json").read_text()
    )
    assert manifest["release_kind"] == "LEGACY_UNPROVEN_ROLLBACK_ARTIFACT"
    assert manifest["git_commit_sha"] == "UNPROVEN"
    app = release_root / "releases" / identity.release_id / "app"
    assert not (app / "data").exists()
    assert not (app / ".secrets").exists()
