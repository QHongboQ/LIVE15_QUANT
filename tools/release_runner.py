"""Stable WinSW bootstrap for a verified LIVE15 release pointer.

This file intentionally uses only the Python standard library.  It is an
installation bootstrap, not a supervisor: WinSW remains the owner of each
service and the selected release owns the application module it imports.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
from pathlib import Path

COMPONENTS = {
    "recorder": ("live15_quant.cli", "recorder_main"),
    "control-center": ("live15_quant.control_center", "main"),
    "runtime-supervisor": ("live15_quant.runtime_supervisor", "main"),
}


class ReleaseRunnerError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseRunnerError(f"invalid release receipt: {path}") from error
    if not isinstance(value, dict):
        raise ReleaseRunnerError(f"release receipt is not an object: {path}")
    return value


def _verify_payload(app: Path, manifest: dict[str, object]) -> None:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ReleaseRunnerError("active release file inventory is missing")
    expected: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            raise ReleaseRunnerError("active release file inventory is invalid")
        relative, digest = item.get("path"), item.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise ReleaseRunnerError("active release file inventory is invalid")
        candidate = (app / relative).resolve()
        try:
            candidate.relative_to(app.resolve())
        except ValueError as error:
            raise ReleaseRunnerError("active release contains unsafe file path") from error
        if not candidate.is_file() or _sha256(candidate) != digest:
            raise ReleaseRunnerError("active release payload hash mismatch")
        expected[relative] = digest
    actual = {path.relative_to(app).as_posix() for path in app.rglob("*") if path.is_file()}
    if actual != set(expected):
        raise ReleaseRunnerError("active release payload inventory mismatch")


def _bootstrap_identity(production_root: Path) -> dict[str, object]:
    """Verify the stable control-plane runner independently of the app pointer."""

    runner = Path(__file__).resolve()
    expected_runner = (production_root / "bootstrap" / "release_runner.py").resolve()
    if runner != expected_runner:
        raise ReleaseRunnerError("runner is outside the stable bootstrap location")
    receipt = _read_json(production_root / "bootstrap" / "bootstrap-manifest.json")
    source_release_id = receipt.get("bootstrap_source_release_id")
    source_manifest_sha256 = receipt.get("bootstrap_source_manifest_sha256")
    if (
        not isinstance(source_release_id, str)
        or Path(source_release_id).name != source_release_id
        or not isinstance(source_manifest_sha256, str)
        or not isinstance(receipt.get("runner_sha256"), str)
    ):
        raise ReleaseRunnerError("stable bootstrap identity is incomplete")
    required = {
        "schema_version": 1,
        "bootstrap_source_release_id": source_release_id,
        "bootstrap_source_manifest_sha256": source_manifest_sha256,
        "runner_sha256": _sha256(runner),
    }
    if any(receipt.get(key) != value for key, value in required.items()):
        raise ReleaseRunnerError("stable bootstrap identity mismatch")
    source = production_root / "releases" / source_release_id
    manifest = source / "release-manifest.json"
    source_runner = source / "app" / "tools" / "release_runner.py"
    if (
        not manifest.is_file()
        or _sha256(manifest) != source_manifest_sha256
        or not source_runner.is_file()
        or _sha256(source_runner) != required["runner_sha256"]
    ):
        raise ReleaseRunnerError("stable bootstrap source verification failed")
    return receipt


def resolve_active_release(production_root: Path) -> tuple[Path, dict[str, object], str]:
    pointer = _read_json(production_root / "active-release.json")
    release_id = pointer.get("release_id")
    pointer_hash = pointer.get("manifest_sha256")
    if not isinstance(release_id, str) or not isinstance(pointer_hash, str):
        raise ReleaseRunnerError("active release pointer is incomplete")
    if Path(release_id).name != release_id:
        raise ReleaseRunnerError("active release identifier is unsafe")
    release_directory = production_root / "releases" / release_id
    manifest_path = release_directory / "release-manifest.json"
    if _sha256(manifest_path) != pointer_hash:
        raise ReleaseRunnerError("active release manifest hash mismatch")
    manifest = _read_json(manifest_path)
    if manifest.get("release_id") != release_id or not isinstance(
        manifest.get("git_commit_sha"), str
    ):
        raise ReleaseRunnerError("active release manifest identity mismatch")
    app = release_directory / "app"
    if not (app / "src" / "live15_quant").is_dir():
        raise ReleaseRunnerError("active release application package is missing")
    _verify_payload(app, manifest)
    return app, manifest, pointer_hash


def _write_runtime_receipt(
    production_root: Path,
    component: str,
    app: Path,
    manifest: dict[str, object],
    manifest_hash: str,
    bootstrap: dict[str, object],
) -> None:
    runtime = production_root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    receipt = {
        "component": component,
        "pid": os.getpid(),
        "parent_pid": os.getppid(),
        "interpreter_path": sys.executable,
        "working_directory": str(app),
        "module_root": str(app / "src" / "live15_quant"),
        "deployment_release_id": manifest["release_id"],
        "deployment_git_sha": manifest["git_commit_sha"],
        "deployment_manifest_sha256": manifest_hash,
        "bootstrap_source_release_id": bootstrap["bootstrap_source_release_id"],
        "bootstrap_source_manifest_sha256": bootstrap["bootstrap_source_manifest_sha256"],
        "bootstrap_runner_sha256": bootstrap["runner_sha256"],
    }
    target = runtime / f"release-runtime-{component}.json"
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def run_component(component: str, production_root: Path | None = None) -> None:
    if component not in COMPONENTS:
        raise ReleaseRunnerError(f"unknown component: {component}")
    root = (production_root or Path(__file__).resolve().parents[1]).resolve()
    bootstrap = _bootstrap_identity(root)
    app, manifest, manifest_hash = resolve_active_release(root)
    os.chdir(app)
    # Release payloads are immutable inventories.  Application imports must
    # not create ``__pycache__`` entries that would invalidate that inventory.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(app / "src"))
    _write_runtime_receipt(root, component, app, manifest, manifest_hash, bootstrap)
    module_name, function_name = COMPONENTS[component]
    # WinSW invokes this bootstrap with ``--component``. Component entry points
    # own their own CLI contracts, including Recorder's explicit no-arguments
    # guard, so the bootstrap arguments must not leak through global sys.argv.
    inherited_argv = sys.argv
    try:
        sys.argv = [sys.argv[0]]
        getattr(importlib.import_module(module_name), function_name)()
    finally:
        sys.argv = inherited_argv


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="live15-release-runner")
    parser.add_argument("--component", choices=sorted(COMPONENTS), required=True)
    parser.add_argument("--production-root", type=Path)
    args = parser.parse_args(argv)
    run_component(args.component, args.production_root)


if __name__ == "__main__":
    main()
