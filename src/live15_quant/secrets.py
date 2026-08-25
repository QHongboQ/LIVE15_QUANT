"""Minimal secret-reference resolution for local and future deployments.

This module resolves paths only; it never stores, logs, or returns secret contents.
The current provider is a project-local file under ``.secrets``. WSL/container mounts,
environment-injected files, and managed cloud providers remain future adapters behind
the same path/reference boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECT_SECRET_DIRECTORY = ".secrets"


@dataclass(frozen=True, slots=True)
class SecretReference:
    """A non-secret reference to a credential location."""

    name: str
    path: Path


def _normalise_name(name: str) -> str:
    value = name.strip()
    if not value or Path(value).name != value or value in {".", ".."}:
        raise ValueError("secret name must be one file name")
    return value


def project_secret_path(name: str, *, project_root: Path | None = None) -> Path:
    """Return the ignored project-local path for one named secret."""

    root = (project_root or Path.cwd()).resolve()
    return root / PROJECT_SECRET_DIRECTORY / _normalise_name(name)


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)
    except OSError:
        return left.expanduser().absolute() == right.expanduser().absolute()


def _readable_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def resolve_secret_path(
    explicit_path: Path | None,
    *,
    name: str,
    project_root: Path | None = None,
    legacy_paths: tuple[Path, ...] = (),
) -> Path | None:
    """Resolve a non-secret path reference using the local migration hierarchy.

    An explicit path wins. The one compatibility exception is a configured legacy
    default: once its project-local replacement exists, the replacement is selected
    so a stale inherited environment variable cannot defeat the sandbox-compatible
    local store. Other explicit paths are never silently replaced.
    """

    local = project_secret_path(name, project_root=project_root)
    if explicit_path is not None:
        if any(_same_path(explicit_path, legacy) for legacy in legacy_paths) and (
            _readable_file(local)
        ):
            return local
        return explicit_path
    if _readable_file(local):
        return local
    for legacy in legacy_paths:
        if _readable_file(legacy):
            return legacy
    return None


def is_project_secret_path(path: Path, *, project_root: Path | None = None) -> bool:
    """Return whether ``path`` is inside this project's ignored secret directory."""

    root = (project_root or Path.cwd()).resolve()
    try:
        return (
            path.expanduser()
            .resolve(strict=False)
            .is_relative_to((root / PROJECT_SECRET_DIRECTORY).resolve(strict=False))
        )
    except OSError:
        return False
