from __future__ import annotations

import errno
import hashlib
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from live15_quant import release_pipeline
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

ROOT = Path(__file__).resolve().parents[1]


def test_current_source_archive_excludes_local_tooling() -> None:
    """A release archive must not carry mutable checkout tooling."""
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout

    with tarfile.open(fileobj=io.BytesIO(archive)) as payload:
        members = {member.name.rstrip("/") for member in payload.getmembers()}
    assert ".local-tools" not in members
    assert all(not member.startswith(".local-tools/") for member in members)


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
    shutil.copyfile(ROOT / "tools/release_runner.py", repository / "tools/release_runner.py")
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


def test_verify_package_rejects_runtime_created_data_inside_payload(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    release_root = tmp_path / "production"
    identity = build_release(repository=repository, git_sha=commit, release_root=release_root)
    payload_data = release_root / "releases" / identity.release_id / "app/data"
    payload_data.mkdir()
    (payload_data / "live15.sqlite3").write_bytes(b"runtime mutation")

    with pytest.raises(ReleaseError, match="mutable path is forbidden in release: data"):
        verify_package(release_root=release_root, release_id=identity.release_id)


def test_build_omits_export_ignored_local_tools_directory(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    local_tools = repository / ".local-tools"
    local_tools.mkdir()
    (local_tools / "Start LIVE15.cmd").write_text("@echo off\n")
    (repository / ".gitattributes").write_text(
        ".local-tools export-ignore\n.local-tools/** export-ignore\n"
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "ignore local tools in release archive")

    identity = build_release(
        repository=repository,
        git_sha=_git(repository, "rev-parse", "HEAD"),
        release_root=tmp_path / "releases",
    )

    assert not (
        tmp_path / "releases" / "releases" / identity.release_id / "app/.local-tools"
    ).exists()


@pytest.mark.parametrize("boundary", [".local-tools", "data"])
def test_verify_package_rejects_empty_prohibited_top_level_directory(
    tmp_path: Path, boundary: str
) -> None:
    repository, commit = _repository(tmp_path)
    release_root = tmp_path / "releases"
    identity = build_release(repository=repository, git_sha=commit, release_root=release_root)
    (release_root / "releases" / identity.release_id / "app" / boundary).mkdir()

    with pytest.raises(ReleaseError, match="mutable path"):
        verify_package(release_root=release_root, release_id=identity.release_id)


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
    bootstrap_runner = release_root / "bootstrap/release_runner.py"
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
                "base_executable": str(interpreter),
                "working_directory": str(release_root),
                "module_root": str(app / "src/live15_quant"),
                "deployment_release_id": identity.release_id,
                "deployment_git_sha": identity.git_commit_sha,
                "deployment_manifest_sha256": identity.manifest_sha256,
                "bootstrap_source_release_id": identity.release_id,
                "bootstrap_source_manifest_sha256": identity.manifest_sha256,
                "bootstrap_runner_sha256": hashlib.sha256(
                    bootstrap_runner.read_bytes()
                ).hexdigest(),
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
            runner_parent_pid=1,
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
            runner_parent_pid=1,
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
            runner_parent_pid=1,
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
            runner_parent_pid=1,
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
    excluded_directories = {
        ".git",
        ".local-tools",
        ".secrets",
        ".venv",
        ".worktrees",
        "bootstrap",
        "current",
        "data",
        "logs",
        "releases",
        "rollback",
        "runtime",
    }
    for directory in excluded_directories:
        (legacy / directory).mkdir()
        (legacy / directory / "must-not-capture.txt").write_text(directory)
    (legacy / ACTIVE_POINTER).write_text("{}")
    (legacy / PREVIOUS_POINTER).write_text("{}")
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
    assert manifest["source_tree_identity"] == "UNPROVEN"
    app = release_root / "releases" / identity.release_id / "app"
    assert all(not (app / directory).exists() for directory in excluded_directories)
    assert not (app / ACTIVE_POINTER).exists()
    assert not (app / PREVIOUS_POINTER).exists()
    assert verify_package(release_root=release_root, release_id=identity.release_id) == identity


def test_legacy_capture_excludes_worktrees_before_copytree_can_descend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    legacy = tmp_path / "legacy"
    (legacy / "src/live15_quant").mkdir(parents=True)
    (legacy / "src/live15_quant/__init__.py").write_text("legacy\n")
    (legacy / "requirements.lock").write_text("example==1.0\n")
    worktrees = legacy / ".worktrees"
    (worktrees / "frozen-holdout").mkdir(parents=True)
    (worktrees / "frozen-holdout/forbidden.bin").write_bytes(b"synthetic-only")
    original_scandir = os.scandir

    def fail_if_worktree_is_scanned(path: str | os.PathLike[str]) -> os.ScandirIterator[str]:
        scanned = Path(path).resolve()
        try:
            scanned.relative_to(worktrees.resolve())
        except ValueError:
            return original_scandir(path)
        raise AssertionError("legacy capture must exclude .worktrees before traversal")

    monkeypatch.setattr(os, "scandir", fail_if_worktree_is_scanned)
    identity = capture_legacy_unproven_release(
        legacy_app_root=legacy,
        release_root=tmp_path / "production",
        created_at="2026-08-28T00:00:00+00:00",
    )
    package = tmp_path / "production/releases" / identity.release_id / "app"
    assert not (package / ".worktrees").exists()
    assert (
        verify_package(release_root=tmp_path / "production", release_id=identity.release_id)
        == identity
    )


def test_legacy_capture_is_safe_when_application_and_release_root_are_identical(
    tmp_path: Path,
) -> None:
    legacy_and_release_root = tmp_path / "legacy-install"
    (legacy_and_release_root / "src/live15_quant").mkdir(parents=True)
    (legacy_and_release_root / "src/live15_quant/__init__.py").write_text("legacy\n")
    (legacy_and_release_root / "requirements.lock").write_text("example==1.0\n")

    identity = capture_legacy_unproven_release(
        legacy_app_root=legacy_and_release_root,
        release_root=legacy_and_release_root,
        created_at="2026-08-28T00:00:00+00:00",
    )

    app = legacy_and_release_root / "releases" / identity.release_id / "app"
    assert not (app / "releases").exists()
    assert (
        verify_package(release_root=legacy_and_release_root, release_id=identity.release_id)
        == identity
    )


def test_legacy_capture_reports_an_inaccessible_unexcluded_directory_before_descent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    legacy = tmp_path / "legacy"
    (legacy / "src/live15_quant").mkdir(parents=True)
    (legacy / "src/live15_quant/__init__.py").write_text("legacy\n")
    (legacy / "requirements.lock").write_text("example==1.0\n")
    inaccessible = legacy / ".checker-pytest-candidate"
    inaccessible.mkdir()
    original_scandir = os.scandir
    scanned_inaccessible: list[Path] = []

    def deny_inaccessible(path: str | os.PathLike[str]) -> os.ScandirIterator[str]:
        candidate = Path(path).resolve()
        if candidate == inaccessible.resolve():
            scanned_inaccessible.append(candidate)
            raise PermissionError(errno.EACCES, "Access is denied", str(path))
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", deny_inaccessible)
    try:
        with pytest.raises(
            ReleaseError,
            match=(
                r"cannot inspect unexcluded directory: "
                r".*\.checker-pytest-candidate.*PermissionError"
            ),
        ):
            capture_legacy_unproven_release(
                legacy_app_root=legacy,
                release_root=tmp_path / "production",
                created_at="2026-08-28T00:00:00+00:00",
            )
    finally:
        monkeypatch.undo()

    assert scanned_inaccessible == [inaccessible.resolve()]


def test_legacy_capture_refuses_a_mocked_reparse_point_before_descent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    legacy = tmp_path / "legacy"
    (legacy / "src/live15_quant").mkdir(parents=True)
    (legacy / "src/live15_quant/__init__.py").write_text("legacy\n")
    (legacy / "requirements.lock").write_text("example==1.0\n")
    reparse_point = legacy / "synthetic-junction"
    reparse_point.mkdir()
    original_scandir = os.scandir

    def fail_if_reparse_point_is_scanned(path: str | os.PathLike[str]) -> os.ScandirIterator[str]:
        candidate = Path(path).resolve()
        try:
            candidate.relative_to(reparse_point.resolve())
        except ValueError:
            return original_scandir(path)
        raise AssertionError("legacy capture must reject a reparse point before descent")

    def classify_mocked_reparse_point(path: Path) -> str:
        if path.resolve() == reparse_point.resolve():
            return "reparse-point"
        return "directory" if path.is_dir() else "regular-file"

    monkeypatch.setattr(os, "scandir", fail_if_reparse_point_is_scanned)
    monkeypatch.setattr(
        release_pipeline,
        "_legacy_capture_entry_type",
        classify_mocked_reparse_point,
        raising=False,
    )
    try:
        with pytest.raises(
            ReleaseError, match=r"refuses reparse-point entry: .*synthetic-junction"
        ):
            capture_legacy_unproven_release(
                legacy_app_root=legacy,
                release_root=tmp_path / "production",
                created_at="2026-08-28T00:00:00+00:00",
            )
    finally:
        monkeypatch.undo()


def test_first_deploy_can_rollback_to_immutable_legacy_without_a_runner(tmp_path: Path) -> None:
    """The stable bootstrap is control-plane content, not legacy app content."""

    legacy = tmp_path / "legacy"
    (legacy / "src/live15_quant").mkdir(parents=True)
    (legacy / "src/live15_quant/__init__.py").write_text("VALUE = 'legacy'\n")
    (legacy / "src/live15_quant/cli.py").write_text("def recorder_main():\n    return None\n")
    (legacy / "requirements.lock").write_text("example==1.0\n")
    release_root = tmp_path / "production"
    legacy_identity = capture_legacy_unproven_release(
        legacy_app_root=legacy,
        release_root=release_root,
        created_at="2026-08-28T00:00:00+00:00",
    )
    legacy_app = release_root / "releases" / legacy_identity.release_id / "app"
    legacy_inventory_before = sorted(
        path.relative_to(legacy_app).as_posix() for path in legacy_app.rglob("*") if path.is_file()
    )
    assert not (legacy_app / "tools/release_runner.py").exists()

    repository, commit = _repository(tmp_path)
    modern_identity = build_release(
        repository=repository,
        git_sha=commit,
        release_root=release_root,
        created_at="2026-08-28T00:00:00+00:00",
    )
    assert activate_release(release_root=release_root, release_id=legacy_identity.release_id)
    stage_bootstrap(release_root=release_root, release_id=modern_identity.release_id)
    assert activate_release(release_root=release_root, release_id=modern_identity.release_id)
    assert rollback_release(release_root=release_root) == legacy_identity
    assert verify_bootstrap(release_root=release_root)
    assert active_release(release_root=release_root) == legacy_identity
    assert legacy_identity.git_commit_sha == "UNPROVEN"
    assert (
        sorted(
            path.relative_to(legacy_app).as_posix()
            for path in legacy_app.rglob("*")
            if path.is_file()
        )
        == legacy_inventory_before
    )

    parent_cwd = Path.cwd()
    parent_sys_path = list(sys.path)
    parent_dont_write_bytecode = sys.dont_write_bytecode
    parent_modules = {
        name: importlib.import_module(name)
        for name in (
            "live15_quant.models",
            "live15_quant.ws_retention",
            "live15_quant.research_data_authority",
            "live15_quant.runtime_supervisor",
        )
    }
    child_environment = {
        key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ
    }
    child_environment.update(
        {"TEMP": str(tmp_path), "TMP": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    )
    stable_runner = release_root / "bootstrap/release_runner.py"
    subprocess.run(
        [
            sys.executable,
            str(stable_runner),
            "--component",
            "recorder",
            "--production-root",
            str(release_root),
        ],
        check=True,
        cwd=tmp_path,
        env=child_environment,
        capture_output=True,
        text=True,
    )
    assert Path.cwd() == parent_cwd
    assert sys.path == parent_sys_path
    assert sys.dont_write_bytecode == parent_dont_write_bytecode
    assert {name: importlib.import_module(name) for name in parent_modules} == parent_modules
    legacy_receipt = json.loads(
        (release_root / "runtime/release-runtime-recorder.json").read_text(encoding="utf-8")
    )
    assert legacy_receipt["deployment_git_sha"] == "UNPROVEN"
    assert legacy_receipt["deployment_release_id"] == legacy_identity.release_id
    assert legacy_receipt["bootstrap_source_release_id"] == modern_identity.release_id
    assert legacy_receipt["bootstrap_runner_sha256"]

    assert activate_release(release_root=release_root, release_id=modern_identity.release_id)
    assert verify_bootstrap(release_root=release_root)


def test_legacy_runner_subprocess_leaves_project_modules_importable() -> None:
    for name in (
        "live15_quant.models",
        "live15_quant.ws_retention",
        "live15_quant.research_data_authority",
        "live15_quant.runtime_supervisor",
    ):
        assert importlib.import_module(name)


def test_bootstrap_corruption_fails_closed_without_changing_active_application(
    tmp_path: Path,
) -> None:
    repository, commit = _repository(tmp_path)
    release_root = tmp_path / "production"
    identity = build_release(repository=repository, git_sha=commit, release_root=release_root)
    stage_bootstrap(release_root=release_root, release_id=identity.release_id)
    activate_release(release_root=release_root, release_id=identity.release_id)
    pointer_before = (release_root / ACTIVE_POINTER).read_text(encoding="utf-8")
    (release_root / "bootstrap/release_runner.py").write_text("corrupt\n", encoding="utf-8")

    with pytest.raises(ReleaseError, match="bootstrap runner hash mismatch"):
        verify_bootstrap(release_root=release_root)

    assert active_release(release_root=release_root) == identity
    assert (release_root / ACTIVE_POINTER).read_text(encoding="utf-8") == pointer_before
