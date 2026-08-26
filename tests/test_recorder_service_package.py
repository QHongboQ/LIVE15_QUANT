from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).parents[1]
PINNED_SHA256 = "05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA"


def _service() -> ElementTree.Element:
    return ElementTree.parse(ROOT / "deploy/windows/live15-recorder.xml").getroot()


def test_recorder_service_uses_direct_authoritative_entrypoint() -> None:
    service = _service()
    values = {child.tag: (child.text or "") for child in service}
    assert values["id"] == "LIVE15Recorder"
    assert values["name"] == "LIVE15 Recorder"
    assert values["executable"].endswith(".venv\\Scripts\\python.exe")
    assert values["arguments"] == '-c "from live15_quant.cli import recorder_main; recorder_main()"'
    assert "managed_recorder" not in values["arguments"]
    assert "RecorderProcessController" not in values["arguments"]
    assert values["workingdirectory"] == r"%BASE%\..\.."


def test_recorder_service_has_native_bounded_recovery_and_safe_environment() -> None:
    service = _service()
    values = {child.tag: (child.text or "") for child in service}
    env = {node.attrib["name"]: node.attrib["value"] for node in service.findall("env")}
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["PYTHONUTF8"] == "1"
    assert env["LIVE15_ENABLE_KALSHI_PRODUCTION_WEBSOCKET"] == "true"
    assert env["LIVE15_KALSHI_RECORDER_PROVIDER"] == "sdk"
    assert env["LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH"] == (
        "%LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH%"
    )
    assert env["LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH"] == (
        "%LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH%"
    )
    assert values["startmode"] == "Automatic"
    assert values["stoptimeout"] == "15 sec"
    assert service.find("log").attrib["mode"] == "roll"
    assert values["resetfailure"] == "5 min"
    failures = service.findall("onfailure")
    assert [node.attrib["action"] for node in failures] == [
        "restart",
        "restart",
        "restart",
        "none",
    ]
    assert [node.attrib["delay"] for node in failures[:-1]] == ["10 sec", "30 sec", "60 sec"]


def test_recorder_service_reuses_winsw_pin_without_binary_or_recorder_lifecycle_manager() -> None:
    metadata = (ROOT / "deploy/windows/winsw-v2.12.0.json").read_text(encoding="utf-8")
    assert '"version":"v2.12.0"' in metadata
    assert f'"sha256":"{PINNED_SHA256}"' in metadata
    assert not list((ROOT / "deploy/windows").glob("*.exe"))
    xml = (ROOT / "deploy/windows/live15-recorder.xml").read_text(encoding="utf-8")
    assert "8765" not in xml
    assert "LIVE15ControlCenter" not in xml
    assert "run_recorder_forever" not in xml
    assert "RuntimePidLease" not in xml
    assert "JobObject" not in xml


def test_recorder_install_tools_target_only_recorder_service_and_validate_credentials() -> None:
    install = (ROOT / "tools/install_recorder_service.ps1").read_text(encoding="utf-8")
    uninstall = (ROOT / "tools/uninstall_recorder_service.ps1").read_text(encoding="utf-8")
    assert "LIVE15Recorder" in install
    assert "LIVE15ControlCenter" not in install
    assert "Test-Path" in install
    assert "LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH" in install
    assert "LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH" in install
    assert "LIVE15Recorder" in uninstall
    assert "LIVE15ControlCenter" not in uninstall
    assert "uninstall" in uninstall.lower()
