from __future__ import annotations

import importlib.util
import json
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


def test_runtime_receipt_records_venv_launcher_and_base_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "release/app"
    app.mkdir(parents=True)
    launcher = tmp_path / "venv/Scripts/python.exe"
    base = tmp_path / "base/python.exe"
    launcher.parent.mkdir(parents=True)
    base.parent.mkdir(parents=True)
    launcher.write_bytes(b"launcher")
    base.write_bytes(b"base")
    monkeypatch.setattr(sys, "executable", str(launcher))
    monkeypatch.setattr(sys, "_base_executable", str(base), raising=False)

    release_runner._write_runtime_receipt(
        tmp_path,
        "recorder",
        app,
        {"release_id": "candidate", "git_commit_sha": "a" * 40},
        "manifest",
        {
            "bootstrap_source_release_id": "candidate",
            "bootstrap_source_manifest_sha256": "bootstrap-manifest",
            "runner_sha256": "runner-hash",
        },
    )

    receipt = json.loads((tmp_path / "runtime/release-runtime-recorder.json").read_text())
    assert receipt["interpreter_path"] == str(launcher)
    assert receipt["base_executable"] == str(base)


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
    # Service code must retain the mutable Production root as its cwd; only
    # imports are allowed to originate from the immutable release payload.
    monkeypatch.chdir(tmp_path)

    release_runner.run_component("recorder", tmp_path)

    assert seen_argv == ["release_runner.py"]
    assert Path.cwd() == tmp_path


@pytest.mark.parametrize(
    ("component", "function_name"),
    [("recorder", "recorder_main"), ("control-center", "main"), ("runtime-supervisor", "main")],
)
def test_component_relative_runtime_writes_stay_outside_immutable_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str, function_name: str
) -> None:
    """All components keep relative mutable paths below the Production root."""

    production = tmp_path / "production"
    app = production / "releases/release/app"
    (app / "src/live15_quant").mkdir(parents=True)
    before = sorted(path.relative_to(app).as_posix() for path in app.rglob("*") if path.is_file())

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
            Path("data/recorder.marker").parent.mkdir(parents=True, exist_ok=True)
            Path("data/recorder.marker").write_text("runtime\n", encoding="utf-8")

        @staticmethod
        def main() -> None:
            Path(f"data/{component}.marker").parent.mkdir(parents=True, exist_ok=True)
            Path(f"data/{component}.marker").write_text("runtime\n", encoding="utf-8")

    monkeypatch.setattr(release_runner.importlib, "import_module", lambda _: Component)
    monkeypatch.chdir(tmp_path)

    release_runner.run_component(component, production)

    after = sorted(path.relative_to(app).as_posix() for path in app.rglob("*") if path.is_file())
    assert after == before
    assert list((production / "data").glob("*.marker"))
