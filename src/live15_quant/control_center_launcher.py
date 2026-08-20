"""One-click Windows launcher for the localhost Control Center."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import uvicorn

from live15_quant.config import Settings, load_settings
from live15_quant.control_center import LOCAL_HOST, create_app
from live15_quant.logging_config import configure_logging


def _control_center_running(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/system", timeout=1.0) as response:
            payload = json.loads(response.read(64 * 1024))
        return (
            isinstance(payload, dict)
            and payload.get("service") == "LIVE15 Control Center"
            and payload.get("api_mode") == "read_only_data_with_bounded_recorder_control"
            and payload.get("bind_host") == LOCAL_HOST
        )
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return False


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind((LOCAL_HOST, port))
        except OSError:
            return False
    return True


def _open_when_ready(url: str, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _control_center_running(url):
            webbrowser.open(url)
            return
        time.sleep(0.1)


def launch(settings: Settings | None = None) -> int:
    configured = settings or load_settings()
    port = configured.ui_port
    if not 1 <= port <= 65535:
        raise ValueError("LIVE15 UI port must be in 1..65535")
    url = f"http://{LOCAL_HOST}:{port}"
    if _control_center_running(url):
        webbrowser.open(url)
        return 0
    if not _port_available(port):
        print(f"ERROR: port {port} is occupied by a non-LIVE15 program.", flush=True)
        return 2
    configure_logging(configured.log_level)
    threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
    print(f"LIVE15 Control Center: {url}", flush=True)
    uvicorn.run(create_app(configured), host=LOCAL_HOST, port=port, log_config=None)
    return 0


def main() -> None:
    raise SystemExit(launch())


if __name__ == "__main__":
    main()
