from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from live15_quant.kalshi_gateway.shadow import ShadowTelemetryStore
from live15_quant.managed_kalshi_sdk_shadow import (
    KalshiSdkShadowRunner,
    _nomad_break_handler,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
JOBSPEC = REPOSITORY_ROOT / "deploy" / "nomad" / "live15-kalshi-sdk-ws-shadow.nomad.hcl"


def _runner(tmp_path: Path) -> tuple[KalshiSdkShadowRunner, ShadowTelemetryStore]:
    store = ShadowTelemetryStore(tmp_path / "shadow.sqlite3")
    runner = KalshiSdkShadowRunner(
        settings=SimpleNamespace(),
        store=store,
        old_projection_path=tmp_path / "old.json",
        status={
            "pid": 1234,
            "started_at": "2026-08-31T00:00:00+00:00",
            "expected_mode": "SDK_WS_SHADOW_NO_RECORDER_WRITES",
        },
        status_path=tmp_path / "status.json",
    )
    return runner, store


def test_nomad_ctrl_break_routes_to_existing_stop_event(tmp_path: Path) -> None:
    runner, store = _runner(tmp_path)

    class ImmediateLoop:
        @staticmethod
        def call_soon_threadsafe(callback, *args) -> None:
            callback(*args)

    try:
        handler = _nomad_break_handler(runner, ImmediateLoop())
        handler(0, None)
        assert runner.stop_event.is_set()
        assert runner.stop_reason == "NOMAD_CTRL_BREAK"
    finally:
        store.close()


def test_nomad_jobspec_owns_only_shadow_process_lifecycle() -> None:
    jobspec = JOBSPEC.read_text(encoding="utf-8")

    assert 'job "live15-kalshi-sdk-ws-shadow"' in jobspec
    assert 'type        = "service"' in jobspec
    assert 'attribute = "${attr.kernel.name}"' in jobspec
    assert 'attribute = "${attr.os.name}"' not in jobspec
    assert 'value     = "windows"' in jobspec
    assert 'driver       = "raw_exec"' in jobspec
    assert "LIVE15_KALSHI_SDK_SHADOW_LIFECYCLE_OWNER" in jobspec
    assert 'LIVE15_KALSHI_SDK_SHADOW_LIFECYCLE_OWNER        = "nomad"' in jobspec
    assert 'LIVE15_KALSHI_RUNTIME_ENVIRONMENT                = "PRODUCTION"' in jobspec
    assert 'LIVE15_ENABLE_KALSHI_PRODUCTION_WEBSOCKET        = "true"' in jobspec
    assert 'KALSHI_DEMO                                      = "false"' in jobspec
    assert 'KALSHI_BASE_URL                                  = ""' in jobspec
    assert 'KALSHI_WS_BASE_URL                               = ""' in jobspec
    assert 'LIVE15_KALSHI_DEMO_API_KEY_ID                    = ""' in jobspec
    assert 'LIVE15_KALSHI_DEMO_API_KEY_ID_FILE               = ""' in jobspec
    assert 'LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH              = ""' in jobspec
    assert "from live15_quant.managed_kalshi_sdk_shadow import main; main()" in jobspec
    assert "attempts = 3" in jobspec
    assert 'interval = "5m"' in jobspec
    assert 'delay    = "15s"' in jobspec
    assert 'mode     = "fail"' in jobspec
    assert "attempts  = 0" in jobspec
    assert "unlimited = false" in jobspec
    assert "runtime-supervisor-control.json" not in jobspec
    assert "paper_forward" not in jobspec
    assert "current_trainable" not in jobspec
    assert "recorder_main" not in jobspec
    assert "Production writes" not in jobspec
