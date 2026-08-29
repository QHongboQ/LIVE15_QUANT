"""Fail-closed paths for test and tool temporary artifacts."""

from pathlib import Path


class RootHygieneError(ValueError):
    """Raised when a temporary-artifact path leaves the approved boundary."""


def resolve_pytest_cache_dir(project_root: Path, configured: str | Path) -> Path:
    """Resolve a pytest cache path while forbidding project-root residue.

    Relative paths must stay below ``runtime/tmp``. Absolute paths may be
    outside the project, but may not resolve to the project root or another
    unapproved location inside it.
    """

    root = project_root.expanduser().resolve()
    candidate = Path(configured).expanduser()
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    approved = (root / "runtime" / "tmp").resolve()

    if candidate.is_absolute():
        invalid = resolved == root or (
            resolved.is_relative_to(root) and not resolved.is_relative_to(approved)
        )
    else:
        invalid = not resolved.is_relative_to(approved)
    if invalid:
        raise RootHygieneError(
            f"temporary artifact path must be external or under {approved}: {resolved}"
        )
    return resolved
