"""Auditable, SHA-pinned application release packages.

This module deliberately has no service-control capability.  It creates and
verifies immutable source releases, stages them under a versioned root, and
switches only a small JSON pointer.  A separately approved deployment task is
responsible for installing rendered WinSW definitions and restarting services.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

MANIFEST_SCHEMA_VERSION = 1
BUILDER_VERSION = "dep-pkg-001"
RELEASES_DIRECTORY = "releases"
ACTIVE_POINTER = "active-release.json"
PREVIOUS_POINTER = "previous-release.json"
BOOTSTRAP_DIRECTORY = "bootstrap"
BOOTSTRAP_RUNNER = "release_runner.py"
BOOTSTRAP_MANIFEST = "bootstrap-manifest.json"
PROHIBITED_TOP_LEVEL = frozenset(
    {"data", "runtime", "logs", ".secrets", "current", "rollback", ".venv", ".git", ".local-tools"}
)
# A legacy install can already contain the new control plane.  It is not part
# of the historical application snapshot and must remain separately staged.
LEGACY_CAPTURE_EXCLUDED_TOP_LEVEL = PROHIBITED_TOP_LEVEL | {BOOTSTRAP_DIRECTORY}


class ReleaseError(RuntimeError):
    """Raised when release provenance or integrity cannot be proven."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _run_git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode:
        raise ReleaseError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _safe_release_id(commit_sha: str, tree_sha: str) -> str:
    return f"live15-{commit_sha[:12]}-{tree_sha[:12]}"


def _pointer_path(release_root: Path, filename: str) -> Path:
    return release_root / filename


def _write_json_atomically(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(_canonical_json(value) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseError(f"invalid JSON receipt: {path}") from error
    if not isinstance(value, dict):
        raise ReleaseError(f"JSON receipt is not an object: {path}")
    return value


def _release_directory(release_root: Path, release_id: str) -> Path:
    if Path(release_id).name != release_id or not (
        release_id.startswith("live15-") or release_id.startswith("legacy-unproven-")
    ):
        raise ReleaseError("invalid release identifier")
    return release_root / RELEASES_DIRECTORY / release_id


def _manifest_path(release_directory: Path) -> Path:
    return release_directory / "release-manifest.json"


def _file_inventory(app_root: Path) -> list[dict[str, str]]:
    inventory: list[dict[str, str]] = []
    for path in sorted(item for item in app_root.rglob("*") if item.is_file()):
        relative = path.relative_to(app_root).as_posix()
        top_level = relative.split("/", 1)[0]
        if top_level in PROHIBITED_TOP_LEVEL:
            raise ReleaseError(f"mutable path is forbidden in release: {relative}")
        inventory.append({"path": relative, "sha256": _sha256_file(path)})
    if not inventory:
        raise ReleaseError("release archive is empty")
    return inventory


def _manifest_artifact_hash(files: list[dict[str, str]]) -> str:
    return hashlib.sha256(_canonical_json(files).encode("utf-8")).hexdigest()


def _archive_commit(repository: Path, commit_sha: str, destination: Path) -> None:
    with destination.open("wb") as output:
        result = subprocess.run(
            ["git", "-C", str(repository), "archive", "--format=tar", commit_sha],
            stdout=output,
            stderr=subprocess.PIPE,
            check=False,
        )
    if result.returncode:
        raise ReleaseError(result.stderr.decode("utf-8", errors="replace").strip())


def _verify_detached_source(repository: Path, commit_sha: str, workspace: Path) -> None:
    _run_git(repository, "worktree", "add", "--detach", "--force", str(workspace), commit_sha)
    try:
        if _run_git(workspace, "rev-parse", "HEAD") != commit_sha:
            raise ReleaseError("temporary source checkout does not match requested SHA")
        if _run_git(workspace, "status", "--porcelain"):
            raise ReleaseError("temporary source checkout is dirty")
    finally:
        _run_git(repository, "worktree", "remove", "--force", str(workspace))


@dataclass(frozen=True)
class ReleaseIdentity:
    release_id: str
    git_commit_sha: str
    source_tree_identity: str
    manifest_sha256: str


def build_release(
    *, repository: Path, git_sha: str, release_root: Path, created_at: str | None = None
) -> ReleaseIdentity:
    """Build an immutable source release exclusively from *git_sha*.

    The source checkout used for the assertion is a new detached worktree. The
    payload itself comes from ``git archive`` so untracked or dirty files can
    never enter the release.
    """

    repository = repository.resolve()
    release_root = release_root.resolve()
    if _run_git(repository, "status", "--porcelain"):
        raise ReleaseError("build source repository is dirty")
    commit_sha = _run_git(repository, "rev-parse", "--verify", f"{git_sha}^{{commit}}")
    tree_sha = _run_git(repository, "rev-parse", f"{commit_sha}^{{tree}}")
    release_id = _safe_release_id(commit_sha, tree_sha)
    destination = _release_directory(release_root, release_id)
    if destination.exists():
        raise ReleaseError(f"release already exists: {release_id}")

    release_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="live15-release-build-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        _verify_detached_source(repository, commit_sha, temporary_root / "source")
        archive = temporary_root / "source.tar"
        _archive_commit(repository, commit_sha, archive)
        staged_release = release_root / RELEASES_DIRECTORY / f".{release_id}.staging"
        staged_app = staged_release / "app"
        try:
            staged_app.mkdir(parents=True)
            with tarfile.open(archive) as tar:
                tar.extractall(staged_app, filter="data")
            files = _file_inventory(staged_app)
            requirements_lock = staged_app / "requirements.lock"
            if not requirements_lock.is_file():
                raise ReleaseError("requirements.lock is missing from requested source")
            manifest: dict[str, Any] = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "release_id": release_id,
                "git_commit_sha": commit_sha,
                "source_tree_identity": tree_sha,
                "requirements_lock_sha256": _sha256_file(requirements_lock),
                "python_version": platform.python_version(),
                "builder_version": BUILDER_VERSION,
                "created_at": created_at or datetime.now(UTC).isoformat(),
                "artifact_manifest_sha256": _manifest_artifact_hash(files),
                "files": files,
            }
            _manifest_path(staged_release).write_text(
                _canonical_json(manifest) + "\n", encoding="utf-8"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_release, destination)
        except Exception:
            shutil.rmtree(staged_release, ignore_errors=True)
            raise
    return verify_package(release_root=release_root, release_id=release_id)


def capture_legacy_unproven_release(
    *, legacy_app_root: Path, release_root: Path, created_at: str | None = None
) -> ReleaseIdentity:
    """Preserve a pre-pipeline application tree without inventing a Git SHA.

    This is intentionally a local, explicit capture operation for the *first*
    audited deployment.  It copies only immutable application candidates and
    excludes every known mutable/data/secret boundary.  Capturing a real host
    remains a separately approved future deployment action.
    """

    legacy_app_root = legacy_app_root.resolve()
    release_root = release_root.resolve()
    if not legacy_app_root.is_dir():
        raise ReleaseError("legacy application root is missing")
    staging_parent = release_root / RELEASES_DIRECTORY
    staging_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".legacy-capture-", dir=staging_parent
    ) as temporary_directory:
        staging_root = Path(temporary_directory) / "release"
        staged_app = staging_root / "app"
        ignored = shutil.ignore_patterns(*LEGACY_CAPTURE_EXCLUDED_TOP_LEVEL)
        shutil.copytree(legacy_app_root, staged_app, ignore=ignored)
        files = _file_inventory(staged_app)
        artifact_hash = _manifest_artifact_hash(files)
        release_id = f"legacy-unproven-{artifact_hash[:16]}"
        destination = _release_directory(release_root, release_id)
        if destination.exists():
            raise ReleaseError(f"release already exists: {release_id}")
        requirements_lock = staged_app / "requirements.lock"
        manifest: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "release_kind": "LEGACY_UNPROVEN_ROLLBACK_ARTIFACT",
            "release_id": release_id,
            "git_commit_sha": "UNPROVEN",
            "source_tree_identity": "UNPROVEN",
            "legacy_install_path": str(legacy_app_root),
            "requirements_lock_sha256": (
                _sha256_file(requirements_lock) if requirements_lock.is_file() else "UNPROVEN"
            ),
            "python_version": platform.python_version(),
            "builder_version": BUILDER_VERSION,
            "created_at": created_at or datetime.now(UTC).isoformat(),
            "artifact_manifest_sha256": artifact_hash,
            "files": files,
        }
        _manifest_path(staging_root).write_text(_canonical_json(manifest) + "\n", encoding="utf-8")
        os.replace(staging_root, destination)
    return verify_package(release_root=release_root, release_id=release_id)


def verify_package(*, release_root: Path, release_id: str) -> ReleaseIdentity:
    """Fail closed unless a staged release and all its hashes are intact."""

    release_directory = _release_directory(release_root.resolve(), release_id)
    manifest_path = _manifest_path(release_directory)
    manifest = _read_json(manifest_path)
    required = {
        "schema_version",
        "release_id",
        "git_commit_sha",
        "source_tree_identity",
        "requirements_lock_sha256",
        "artifact_manifest_sha256",
        "files",
    }
    if not required.issubset(manifest) or manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ReleaseError("release manifest schema is incomplete or unsupported")
    if manifest["release_id"] != release_id:
        raise ReleaseError("release manifest identity mismatch")
    app_root = release_directory / "app"
    files = manifest["files"]
    if not isinstance(files, list) or not all(isinstance(item, dict) for item in files):
        raise ReleaseError("release manifest file inventory is invalid")
    normalized_files = [{"path": item.get("path"), "sha256": item.get("sha256")} for item in files]
    if _manifest_artifact_hash(normalized_files) != manifest["artifact_manifest_sha256"]:
        raise ReleaseError("artifact manifest hash mismatch")
    actual_inventory = _file_inventory(app_root)
    if actual_inventory != normalized_files:
        raise ReleaseError("release payload hash mismatch")
    requirements_lock = app_root / "requirements.lock"
    if manifest.get("requirements_lock_sha256") == "UNPROVEN":
        if requirements_lock.exists():
            raise ReleaseError("legacy release lockfile provenance mismatch")
    elif (
        not requirements_lock.is_file()
        or _sha256_file(requirements_lock) != manifest["requirements_lock_sha256"]
    ):
        raise ReleaseError("requirements.lock hash mismatch")
    manifest_sha256 = _sha256_file(manifest_path)
    return ReleaseIdentity(
        release_id=release_id,
        git_commit_sha=str(manifest["git_commit_sha"]),
        source_tree_identity=str(manifest["source_tree_identity"]),
        manifest_sha256=manifest_sha256,
    )


def _read_pointer(release_root: Path, filename: str) -> ReleaseIdentity | None:
    path = _pointer_path(release_root, filename)
    if not path.exists():
        return None
    pointer = _read_json(path)
    release_id = pointer.get("release_id")
    manifest_sha256 = pointer.get("manifest_sha256")
    if not isinstance(release_id, str) or not isinstance(manifest_sha256, str):
        raise ReleaseError(f"invalid release pointer: {path}")
    identity = verify_package(release_root=release_root, release_id=release_id)
    if identity.manifest_sha256 != manifest_sha256:
        raise ReleaseError(f"release pointer manifest mismatch: {path}")
    return identity


def active_release(*, release_root: Path) -> ReleaseIdentity | None:
    return _read_pointer(release_root.resolve(), ACTIVE_POINTER)


def activate_release(
    *, release_root: Path, release_id: str, dry_run: bool = False
) -> ReleaseIdentity:
    """Atomically replace the active pointer after full package verification."""

    release_root = release_root.resolve()
    candidate = verify_package(release_root=release_root, release_id=release_id)
    current = active_release(release_root=release_root)
    if dry_run:
        return candidate
    if current is not None:
        _write_json_atomically(
            _pointer_path(release_root, PREVIOUS_POINTER),
            {"release_id": current.release_id, "manifest_sha256": current.manifest_sha256},
        )
    _write_json_atomically(
        _pointer_path(release_root, ACTIVE_POINTER),
        {"release_id": candidate.release_id, "manifest_sha256": candidate.manifest_sha256},
    )
    return active_release(release_root=release_root) or candidate


def rollback_release(*, release_root: Path, dry_run: bool = False) -> ReleaseIdentity:
    """Restore the independently verified previous release pointer."""

    release_root = release_root.resolve()
    previous = _read_pointer(release_root, PREVIOUS_POINTER)
    if previous is None:
        raise ReleaseError("no rollback release is available")
    return activate_release(
        release_root=release_root, release_id=previous.release_id, dry_run=dry_run
    )


def _bootstrap_paths(release_root: Path) -> tuple[Path, Path]:
    directory = release_root / BOOTSTRAP_DIRECTORY
    return directory / BOOTSTRAP_RUNNER, directory / BOOTSTRAP_MANIFEST


def _resolve_service_base_path(value: str, service_config_path: Path) -> Path:
    """Resolve the WinSW base placeholder or an equivalent explicit path."""

    if value.startswith("%BASE%"):
        value = value.replace("%BASE%", str(service_config_path.parent), 1)
    return Path(value).resolve()


def stage_bootstrap(
    *, release_root: Path, release_id: str, dry_run: bool = False
) -> ReleaseIdentity:
    """Atomically stage a verified stable WinSW bootstrap control plane.

    The selected source release authenticates the runner bytes, but does not
    become the active application identity.  This distinction is what permits
    a first deployment to return to an immutable ``LEGACY_UNPROVEN`` app.
    """

    release_root = release_root.resolve()
    identity = verify_package(release_root=release_root, release_id=release_id)
    source = _release_directory(release_root, release_id) / "app" / "tools"
    source = source / BOOTSTRAP_RUNNER
    if not source.is_file():
        raise ReleaseError("release does not contain the required stable bootstrap runner")
    if dry_run:
        return identity
    destination, receipt_path = _bootstrap_paths(release_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(source, temporary)
        if _sha256_file(temporary) != _sha256_file(source):
            raise ReleaseError("bootstrap copy hash mismatch")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    _write_json_atomically(
        receipt_path,
        {
            "schema_version": 1,
            "bootstrap_source_release_id": identity.release_id,
            "bootstrap_source_manifest_sha256": identity.manifest_sha256,
            "runner_sha256": _sha256_file(destination),
        },
    )
    return identity


def verify_bootstrap(*, release_root: Path) -> ReleaseIdentity:
    """Prove the installed stable runner is byte-bound to its source release.

    It intentionally does not inspect the active application pointer: rollback
    may activate a legacy artifact that predates this control-plane runner.
    """

    release_root = release_root.resolve()
    runner, receipt_path = _bootstrap_paths(release_root)
    receipt = _read_json(receipt_path)
    release_id = receipt.get("bootstrap_source_release_id")
    manifest_sha256 = receipt.get("bootstrap_source_manifest_sha256")
    if not isinstance(release_id, str) or not isinstance(manifest_sha256, str):
        raise ReleaseError("installed bootstrap source identity is incomplete")
    identity = verify_package(release_root=release_root, release_id=release_id)
    if identity.manifest_sha256 != manifest_sha256:
        raise ReleaseError("installed bootstrap source manifest mismatch")
    source = (
        _release_directory(release_root, identity.release_id) / "app" / "tools" / BOOTSTRAP_RUNNER
    )
    if not source.is_file() or not runner.is_file():
        raise ReleaseError("stable bootstrap runner is missing")
    runner_hash = _sha256_file(runner)
    if runner_hash != _sha256_file(source) or receipt.get("runner_sha256") != runner_hash:
        raise ReleaseError("stable bootstrap runner hash mismatch")
    return identity


def verify_runtime_provenance(
    *,
    release_root: Path,
    service_name: str,
    service_pid: int,
    runner_pid: int,
    service_config_path: Path,
    expected_git_sha: str,
) -> ReleaseIdentity:
    """Bind a captured running-process observation to the active release.

    The Windows service PID is captured externally with ``sc queryex``. The
    runner receipt is emitted by the verified stable bootstrap and must show it
    is that service's direct child, imported code from the active release, and
    used exactly the requested SHA. The installed XML is checked so a service
    path cannot quietly bypass the bootstrap.
    """

    component_by_service = {
        "LIVE15Recorder": "recorder",
        "LIVE15ControlCenter": "control-center",
        "LIVE15RuntimeSupervisor": "runtime-supervisor",
    }
    component = component_by_service.get(service_name)
    if component is None or service_pid <= 0 or runner_pid <= 0:
        raise ReleaseError("incomplete runtime service observation")
    bootstrap_identity = verify_bootstrap(release_root=release_root)
    identity = active_release(release_root=release_root)
    if identity is None:
        raise ReleaseError("no active release pointer")
    if identity.git_commit_sha != expected_git_sha:
        raise ReleaseError("active release SHA does not match requested deployment SHA")
    try:
        service_xml = ElementTree.parse(service_config_path).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise ReleaseError("installed service XML cannot be verified") from error
    values = {child.tag: child.text or "" for child in service_xml}
    arguments = values.get("arguments", "")
    command_suffix = f" --component {component}"
    if values.get("id") != service_name or not arguments.endswith(command_suffix):
        raise ReleaseError("installed service XML does not bind to release bootstrap")
    configured_runner = _resolve_service_base_path(
        arguments.removesuffix(command_suffix), service_config_path
    )
    expected_runner, _ = _bootstrap_paths(release_root.resolve())
    if configured_runner != expected_runner.resolve():
        raise ReleaseError("installed service XML points at a different bootstrap runner")
    configured_working_directory = _resolve_service_base_path(
        values.get("workingdirectory", ""), service_config_path
    )
    if configured_working_directory != release_root.resolve():
        raise ReleaseError("installed service XML working directory is outside production root")
    receipt = _read_json(release_root.resolve() / "runtime" / f"release-runtime-{component}.json")
    required = {
        "component": component,
        "pid": runner_pid,
        "parent_pid": service_pid,
        "deployment_release_id": identity.release_id,
        "deployment_git_sha": identity.git_commit_sha,
        "deployment_manifest_sha256": identity.manifest_sha256,
        "bootstrap_source_release_id": bootstrap_identity.release_id,
        "bootstrap_source_manifest_sha256": bootstrap_identity.manifest_sha256,
        "bootstrap_runner_sha256": _sha256_file(_bootstrap_paths(release_root.resolve())[0]),
    }
    if any(receipt.get(key) != value for key, value in required.items()):
        raise ReleaseError("runner receipt does not bind to active service and release")
    app_root = (_release_directory(release_root.resolve(), identity.release_id) / "app").resolve()
    for label, raw_path in (
        ("working directory", receipt.get("working_directory")),
        ("module root", receipt.get("module_root")),
        ("interpreter", receipt.get("interpreter_path")),
    ):
        if not isinstance(raw_path, str):
            raise ReleaseError(f"runner receipt {label} is missing")
        observed = Path(raw_path)
        try:
            if label != "interpreter":
                observed.resolve().relative_to(app_root)
        except ValueError as error:
            raise ReleaseError(f"runtime {label} is outside active release") from error
    if not Path(str(receipt["interpreter_path"])).is_file():
        raise ReleaseError("runtime interpreter path is missing")
    return identity


def _identity_json(identity: ReleaseIdentity) -> str:
    return _canonical_json(identity.__dict__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live15-release", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--repo", required=True, type=Path)
    build.add_argument("--git-sha", required=True)
    build.add_argument("--release-root", required=True, type=Path)
    build.add_argument("--created-at")
    legacy = commands.add_parser("capture-legacy-unproven")
    legacy.add_argument("--legacy-app-root", required=True, type=Path)
    legacy.add_argument("--release-root", required=True, type=Path)
    legacy.add_argument("--created-at")
    for name in ("verify-package", "activate", "stage-bootstrap"):
        command = commands.add_parser(name)
        command.add_argument("--release-root", required=True, type=Path)
        command.add_argument("--release-id", required=True)
        if name in {"activate", "stage-bootstrap"}:
            command.add_argument("--dry-run", action="store_true")
    active = commands.add_parser("verify-active")
    active.add_argument("--release-root", required=True, type=Path)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--release-root", required=True, type=Path)
    rollback.add_argument("--dry-run", action="store_true")
    commands.add_parser("verify-bootstrap").add_argument("--release-root", required=True, type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> None:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "build":
        identity = build_release(
            repository=args.repo,
            git_sha=args.git_sha,
            release_root=args.release_root,
            created_at=args.created_at,
        )
    elif args.command == "capture-legacy-unproven":
        identity = capture_legacy_unproven_release(
            legacy_app_root=args.legacy_app_root,
            release_root=args.release_root,
            created_at=args.created_at,
        )
    elif args.command == "verify-package":
        identity = verify_package(release_root=args.release_root, release_id=args.release_id)
    elif args.command == "activate":
        identity = activate_release(
            release_root=args.release_root, release_id=args.release_id, dry_run=args.dry_run
        )
    elif args.command == "stage-bootstrap":
        identity = stage_bootstrap(
            release_root=args.release_root, release_id=args.release_id, dry_run=args.dry_run
        )
    elif args.command == "verify-bootstrap":
        identity = verify_bootstrap(release_root=args.release_root)
    elif args.command == "verify-active":
        identity = active_release(release_root=args.release_root)
        if identity is None:
            raise ReleaseError("no active release pointer")
    else:
        identity = rollback_release(release_root=args.release_root, dry_run=args.dry_run)
    print(_identity_json(identity))


if __name__ == "__main__":
    main()
