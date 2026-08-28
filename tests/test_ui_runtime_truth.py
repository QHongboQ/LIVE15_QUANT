from __future__ import annotations

import json
from pathlib import Path

from tools.ui_runtime_truth_self_test import run_checks


def test_ui_data_contract_is_machine_readable_and_live_only() -> None:
    path = Path(__file__).parents[1] / "docs" / "ui" / "ui_data_contract.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    assert contract["failure_semantics"].startswith("clear_failed_projection")
    assert contract["entries"]
    assert {entry["status"] for entry in contract["entries"]} == {"LIVE"}
    for entry in contract["entries"]:
        assert entry["endpoint"].startswith("/")
        assert entry["source"]
        assert entry["authoritative"]


def test_ui_renders_intentional_auxiliary_states_neutrally() -> None:
    root = Path(__file__).parents[1]
    app = (root / "src" / "live15_quant" / "web" / "app.js").read_text(encoding="utf-8")
    stylesheet = (root / "src" / "live15_quant" / "web" / "app.css").read_text(encoding="utf-8")

    assert 'replaceAll("_", " ").toUpperCase()' in app
    assert ".state-on_demand, .state-paused_by_design" in stylesheet
    assert "function runtimeComponentMetric(name, component)" in app
    assert "badge(component.status, stateLabel(component.status))" in app
    assert "runtimeGrid.append(runtimeComponentMetric(name, component))" in app


def test_truth_probe_fails_closed_on_unreachable_api(monkeypatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("tools.ui_runtime_truth_self_test.urllib.request.urlopen", unavailable)
    report = run_checks(Path.cwd(), "http://127.0.0.1:8765")
    assert report["overall"] == "FAIL"
    assert all(
        item["status"] == "FAIL"
        for item in report["checks"]
        if item["name"].startswith("endpoint:")
    )


def test_truth_probe_accepts_typed_endpoint_responses(monkeypatch) -> None:
    payloads = {
        "/api/health": {
            "status": "healthy",
            "recorder_state": "running",
            "heartbeat_status": "available",
            "observed_at": "2026-01-01T00:00:00Z",
            "fatal_task": None,
            "fatal_error_type": None,
        },
        "/api/system": {"service": "LIVE15 Control Center", "bind_host": "127.0.0.1"},
        "/api/account?profile=production_primary": {"status": "UNAVAILABLE"},
        "/api/markets": [],
        "/api/archive": {"failed_chunks": 0},
        "/api/storage": {"state": "normal"},
    }

    class Response:
        def __init__(self, path: str):
            self.status = 200
            self._body = b"<html>" if path == "/" else json.dumps(payloads[path]).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return self._body

    def available(request, **_kwargs):
        from urllib.parse import urlsplit

        return Response(
            urlsplit(request.full_url).path
            + (f"?{urlsplit(request.full_url).query}" if urlsplit(request.full_url).query else "")
        )

    monkeypatch.setattr("tools.ui_runtime_truth_self_test.urllib.request.urlopen", available)
    report = run_checks(Path.cwd(), "http://127.0.0.1:8765")
    assert report["overall"] == "WARN"  # account is intentionally unavailable in this fixture
    assert not any(item["status"] == "FAIL" for item in report["checks"])
