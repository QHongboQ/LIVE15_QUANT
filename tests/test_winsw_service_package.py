from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).parents[1]
PINNED_SHA256 = "05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA"


def test_winsw_metadata_is_pinned_to_stable_official_release() -> None:
    metadata = json.loads((ROOT / "deploy/windows/winsw-v2.12.0.json").read_text())
    assert metadata["version"] == "v2.12.0"
    assert metadata["asset"] == "WinSW-x64.exe"
    assert metadata["license"] == "MIT"
    assert metadata["url"].startswith("https://github.com/winsw/winsw/releases/download/v2.12.0/")
    assert metadata["sha256"] == PINNED_SHA256


def test_bootstrap_fails_closed_on_checksum_mismatch() -> None:
    script = (ROOT / "tools/bootstrap_winsw.ps1").read_text()
    assert "WinSW SHA256 mismatch" in script
    assert "Get-FileHash" in script
    assert "Move-Item" in script


def test_bootstrap_script_index_blob_is_lf_normalized() -> None:
    """Keep the CRLF worktree policy from making a clean Windows checkout dirty."""
    result = subprocess.run(
        ["git", "show", ":tools/bootstrap_winsw.ps1"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )

    assert b"\r" not in result.stdout


def test_service_xml_is_direct_python_with_bounded_failure_policy() -> None:
    service = ElementTree.parse(ROOT / "deploy/windows/live15-control-center.xml").getroot()
    values = {child.tag: (child.text or "") for child in service}
    assert values["id"] == "LIVE15ControlCenter"
    assert values["executable"].endswith(".venv\\Scripts\\python.exe")
    assert values["arguments"].endswith("bootstrap\\release_runner.py --component control-center")
    env = {node.attrib["name"]: node.attrib["value"] for node in service.findall("env")}
    assert env == {
        "PYTHONUNBUFFERED": "1",
        "PYTHONUTF8": "1",
        "LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH": "%LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH%",
        "LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH": "%LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH%",
    }
    assert "cmd.exe" not in ElementTree.tostring(service, encoding="unicode")
    assert values["resetfailure"] == "5 min"
    assert values["startmode"] == "Automatic"
    assert values["stoptimeout"] == "15 sec"
    assert service.find("log").attrib["mode"] == "roll"
    actions = [node.attrib["action"] for node in service.findall("onfailure")]
    assert actions == ["restart", "restart", "restart", "none"]
    assert [node.attrib["delay"] for node in service.findall("onfailure")[:-1]] == [
        "10 sec",
        "30 sec",
        "60 sec",
    ]


def test_recorder_and_supervisor_are_independent_automatic_winsw_services() -> None:
    recorder = ElementTree.parse(ROOT / "deploy/windows/live15-recorder.xml").getroot()
    supervisor = ElementTree.parse(ROOT / "deploy/windows/live15-runtime-supervisor.xml").getroot()
    recorder_values = {child.tag: (child.text or "") for child in recorder}
    supervisor_values = {child.tag: (child.text or "") for child in supervisor}

    assert recorder_values["id"] == "LIVE15Recorder"
    assert recorder_values["startmode"] == "Automatic"
    assert supervisor_values["id"] == "LIVE15RuntimeSupervisor"
    assert supervisor_values["arguments"].endswith(
        "bootstrap\\release_runner.py --component runtime-supervisor"
    )
    assert supervisor_values["startmode"] == "Automatic"
    assert [node.attrib["action"] for node in supervisor.findall("onfailure")] == [
        "restart",
        "restart",
        "restart",
        "none",
    ]


def test_supervisor_install_scripts_stage_only_supervisor_service() -> None:
    install = (ROOT / "tools/install_runtime_supervisor_service.ps1").read_text(encoding="utf-8")
    uninstall = (ROOT / "tools/uninstall_runtime_supervisor_service.ps1").read_text(
        encoding="utf-8"
    )
    assert "LIVE15RuntimeSupervisor" in install
    assert "LIVE15Recorder" not in install
    assert "LIVE15ControlCenter" not in install
    assert "LIVE15RuntimeSupervisor" in uninstall


def test_install_scripts_render_only_control_center_and_escape_paths() -> None:
    install = (ROOT / "tools" / "install_control_center_service.ps1").read_text(encoding="utf-8")
    uninstall = (ROOT / "tools" / "uninstall_control_center_service.ps1").read_text(
        encoding="utf-8"
    )
    assert "LIVE15ControlCenter" in install
    assert "LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH" in install
    assert "LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH" in install
    assert "SecurityElement]::Escape" in install
    assert "LIVE15Recorder" not in install
    assert "LIVE15Recorder" not in uninstall
    assert "LIVE15ControlCenter" in uninstall


def test_control_center_config_only_references_credential_paths_not_secret_contents() -> None:
    xml = (ROOT / "deploy/windows/live15-control-center.xml").read_text(encoding="utf-8")
    assert "%LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH%" in xml
    assert "%LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH%" in xml
    assert "BEGIN" not in xml
    assert "PRIVATE KEY" not in xml


def test_bootstrap_rejects_bad_download_without_promoting_it() -> None:
    with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
        project = Path(temp_dir) / "project"
        (project / "deploy/windows").mkdir(parents=True)
        (project / "tools").mkdir(parents=True)
        bad = b"not WinSW"
        (project / "download.bin").write_bytes(bad)
        destination = project / ".local-tools/winsw/WinSW-x64.exe"
        destination.parent.mkdir(parents=True)
        existing = b"existing local WinSW executable"
        destination.write_bytes(existing)
        metadata = json.loads((ROOT / "deploy/windows/winsw-v2.12.0.json").read_text())
        metadata["url"] = "http://127.0.0.1:0/download.bin"
        (project / "deploy/windows/winsw-v2.12.0.json").write_text(json.dumps(metadata))

        class Handler(SimpleHTTPRequestHandler):
            requests = 0

            def do_GET(self) -> None:
                type(self).requests += 1
                super().do_GET()

            def log_message(self, *_args: object) -> None:
                pass

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            partial(Handler, directory=str(project)),
        )
        metadata["url"] = f"http://127.0.0.1:{server.server_port}/download.bin"
        (project / "deploy/windows/winsw-v2.12.0.json").write_text(json.dumps(metadata))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = os.environ.copy()
            env["PSModulePath"] = (
                r"C:\Program Files\WindowsPowerShell\Modules;"
                r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\Modules"
            )
            result = subprocess.run(
                [
                    r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    (
                        "Import-Module Microsoft.PowerShell.Utility; "
                        f"& '{ROOT / 'tools/bootstrap_winsw.ps1'}' -ProjectRoot '{project}'"
                    ),
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
        assert result.returncode != 0
        assert Handler.requests == 1, result.stderr
        assert destination.read_bytes() == existing
        assert hashlib.sha256(bad).hexdigest().upper() != PINNED_SHA256


def test_package_has_no_legacy_watchdog_or_recorder_lifecycle_dependency() -> None:
    xml = (ROOT / "deploy/windows/live15-control-center.xml").read_text()
    assert "managed_control_center" not in xml
    assert "RuntimePidLease" not in xml
    assert "runtime-supervisor" not in xml
    assert "Recorder" not in xml
