from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from live15_quant.release_pipeline import activate_release, build_release

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_runner", ROOT / "tools/release_runner.py")
assert SPEC and SPEC.loader
release_runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_runner)


def _git(repository: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repository), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_runner_resolves_only_active_verified_release(tmp_path: Path) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "test")
    (repository / "src/live15_quant").mkdir(parents=True)
    (repository / "src/live15_quant/__init__.py").write_text("VALUE = 'release'\n")
    (repository / "requirements.lock").write_text("example==1.0\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "release")
    identity = build_release(
        repository=repository,
        git_sha=_git(repository, "rev-parse", "HEAD"),
        release_root=tmp_path / "production",
    )
    activate_release(release_root=tmp_path / "production", release_id=identity.release_id)
    app, manifest, manifest_hash = release_runner.resolve_active_release(tmp_path / "production")
    assert app.name == "app"
    assert manifest["git_commit_sha"] == identity.git_commit_sha
    assert manifest_hash == identity.manifest_sha256

    pointer = tmp_path / "production" / "active-release.json"
    pointer.write_text('{"release_id":"' + identity.release_id + '","manifest_sha256":"bad"}')
    with pytest.raises(release_runner.ReleaseRunnerError, match="manifest hash mismatch"):
        release_runner.resolve_active_release(tmp_path / "production")


def test_runner_rejects_a_payload_that_no_longer_matches_the_manifest(tmp_path: Path) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "test")
    (repository / "src/live15_quant").mkdir(parents=True)
    payload = repository / "src/live15_quant/__init__.py"
    payload.write_text("VALUE = 'release'\n")
    (repository / "requirements.lock").write_text("example==1.0\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "release")
    production = tmp_path / "production"
    identity = build_release(
        repository=repository,
        git_sha=_git(repository, "rev-parse", "HEAD"),
        release_root=production,
    )
    activate_release(release_root=production, release_id=identity.release_id)
    (production / "releases" / identity.release_id / "app/src/live15_quant/__init__.py").write_text(
        "corrupt\n"
    )
    with pytest.raises(release_runner.ReleaseRunnerError, match="payload hash mismatch"):
        release_runner.resolve_active_release(production)


def test_runner_does_not_leak_its_component_arguments_to_the_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WinSW invokes the runner with ``--component``; component CLIs must not see it."""

    app = tmp_path / "app"
    (app / "src").mkdir(parents=True)
    seen_argv: list[str] = []

    monkeypatch.setattr(release_runner, "_bootstrap_identity", lambda _: {})
    monkeypatch.setattr(
        release_runner,
        "resolve_active_release",
        lambda _: (app, {"release_id": "release"}, "manifest"),
    )
    monkeypatch.setattr(release_runner, "_write_runtime_receipt", lambda *args: None)

    class Component:
        @staticmethod
        def recorder_main() -> None:
            seen_argv.extend(sys.argv)

    monkeypatch.setattr(release_runner.importlib, "import_module", lambda _: Component)
    monkeypatch.setattr(sys, "argv", ["release_runner.py", "--component", "recorder"])

    release_runner.run_component("recorder", tmp_path)

    assert seen_argv == ["release_runner.py"]
