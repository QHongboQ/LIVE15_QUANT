from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

import pytest

from live15_quant.deployment_sidecar import SidecarRenderError, render_candidate_winsw_sidecar

_KEY_ID = "LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH"
_PRIVATE_KEY = "LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH"


def _write_sidecar(
    path: Path,
    *,
    service_id: str = "LIVE15Recorder",
    component: str = "recorder",
    key_id: str = f"%{_KEY_ID}%",
    private_key: str = f"%{_PRIVATE_KEY}%",
) -> None:
    arguments = "%BASE%\\..\\..\\bootstrap\\release_runner.py --component " + component
    path.write_text(
        "<service>"
        f"<id>{service_id}</id>"
        "<executable>%BASE%\\..\\..\\.venv\\Scripts\\python.exe</executable>"
        f"<arguments>{arguments}</arguments>"
        f'<env name="{_KEY_ID}" value="{key_id}" />'
        f'<env name="{_PRIVATE_KEY}" value="{private_key}" />'
        "</service>",
        encoding="utf-8",
    )


def _environment(path: Path) -> dict[str, str]:
    root = ElementTree.parse(path).getroot()
    return {node.attrib["name"]: node.attrib["value"] for node in root.findall("env")}


def test_render_candidate_sidecar_preserves_existing_external_credential_references(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate.xml"
    installed = tmp_path / "installed.xml"
    _write_sidecar(candidate)
    _write_sidecar(
        installed,
        key_id=r"D:\LIVE15_CREDENTIALS\kalshi-id.txt",
        private_key=r"D:\LIVE15_CREDENTIALS\kalshi-private.key",
    )

    rendered = render_candidate_winsw_sidecar(
        candidate_template=candidate,
        installed_sidecar=installed,
        expected_service_id="LIVE15Recorder",
        expected_component="recorder",
    )
    output = tmp_path / "rendered.xml"
    output.write_bytes(rendered.xml)

    assert _environment(output) == _environment(installed)
    assert rendered.sha256
    assert rendered.preserved_environment_names == (_KEY_ID, _PRIVATE_KEY)
    assert f"%{_KEY_ID}%" not in output.read_text(encoding="utf-8")
    assert f"%{_PRIVATE_KEY}%" not in output.read_text(encoding="utf-8")


def test_render_candidate_sidecar_rejects_unresolved_installed_credential_reference(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate.xml"
    installed = tmp_path / "installed.xml"
    _write_sidecar(candidate)
    _write_sidecar(installed)

    with pytest.raises(SidecarRenderError, match="must be an absolute path"):
        render_candidate_winsw_sidecar(
            candidate_template=candidate,
            installed_sidecar=installed,
            expected_service_id="LIVE15Recorder",
            expected_component="recorder",
        )


def test_render_candidate_sidecar_rejects_absolute_path_with_unresolved_placeholder(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate.xml"
    installed = tmp_path / "installed.xml"
    _write_sidecar(candidate)
    _write_sidecar(
        installed,
        key_id=r"D:\LIVE15_CREDENTIALS\%UNRESOLVED%\kalshi-id.txt",
        private_key=r"D:\LIVE15_CREDENTIALS\kalshi-private.key",
    )

    with pytest.raises(SidecarRenderError, match="must be an absolute path"):
        render_candidate_winsw_sidecar(
            candidate_template=candidate,
            installed_sidecar=installed,
            expected_service_id="LIVE15Recorder",
            expected_component="recorder",
        )


def test_render_candidate_sidecar_rejects_wrong_candidate_component(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.xml"
    installed = tmp_path / "installed.xml"
    _write_sidecar(candidate, component="control-center")
    _write_sidecar(
        installed,
        key_id=r"D:\LIVE15_CREDENTIALS\kalshi-id.txt",
        private_key=r"D:\LIVE15_CREDENTIALS\kalshi-private.key",
    )

    with pytest.raises(SidecarRenderError, match="component identity"):
        render_candidate_winsw_sidecar(
            candidate_template=candidate,
            installed_sidecar=installed,
            expected_service_id="LIVE15Recorder",
            expected_component="recorder",
        )
