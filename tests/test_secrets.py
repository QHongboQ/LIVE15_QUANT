from pathlib import Path

from live15_quant.secrets import (
    SecretReference,
    is_project_secret_path,
    project_secret_path,
    resolve_secret_path,
)


def test_project_secret_reference_is_path_only() -> None:
    root = Path("D:/project")
    reference = SecretReference(
        "pyth-api-key.txt", project_secret_path("pyth-api-key.txt", project_root=root)
    )
    assert reference.name == "pyth-api-key.txt"
    assert reference.path == root / ".secrets" / "pyth-api-key.txt"
    assert is_project_secret_path(reference.path, project_root=root)


def test_explicit_nonlegacy_path_wins() -> None:
    root = Path("D:/project")
    explicit = Path("C:/managed/pyth.key")
    assert resolve_secret_path(explicit, name="pyth-api-key.txt", project_root=root) == explicit


def test_local_path_replaces_stale_legacy_reference(tmp_path: Path) -> None:
    local = project_secret_path("pyth-api-key.txt", project_root=tmp_path)
    local.parent.mkdir()
    local.write_bytes(b"opaque")
    legacy = tmp_path / "legacy" / "pyth-api-key.txt"
    assert (
        resolve_secret_path(
            legacy,
            name="pyth-api-key.txt",
            project_root=tmp_path,
            legacy_paths=(legacy,),
        )
        == local
    )


def test_local_path_is_selected_when_no_explicit_path(tmp_path: Path) -> None:
    local = project_secret_path("pyth-api-key.txt", project_root=tmp_path)
    local.parent.mkdir()
    local.write_bytes(b"opaque")
    assert resolve_secret_path(None, name="pyth-api-key.txt", project_root=tmp_path) == local
