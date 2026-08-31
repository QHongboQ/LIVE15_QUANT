from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from live15_quant.kalshi_gateway.shadow import ShadowTelemetryStore
from live15_quant.managed_kalshi_sdk_shadow import (
    KalshiSdkShadowRunner,
    _lifecycle_owner,
    _nomad_break_handler,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
JOBSPEC = REPOSITORY_ROOT / "deploy" / "nomad" / "live15-kalshi-sdk-ws-shadow.nomad.hcl"


def _runner(
    tmp_path: Path, *, control_path: Path | None
) -> tuple[KalshiSdkShadowRunner, ShadowTelemetryStore]:
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
        control_path=control_path,
    )
    return runner, store


def test_lifecycle_owner_is_explicit_and_fail_closed() -> None:
    assert _lifecycle_owner({}) == "runtime_supervisor"
    assert _lifecycle_owner({"LIVE15_KALSHI_SDK_SHADOW_LIFECYCLE_OWNER": "nomad"}) == "nomad"
    with pytest.raises(ValueError, match="lifecycle owner"):
        _lifecycle_owner({"LIVE15_KALSHI_SDK_SHADOW_LIFECYCLE_OWNER": "both"})


def test_nomad_ctrl_break_routes_to_existing_stop_event(tmp_path: Path) -> None:
    runner, store = _runner(tmp_path, control_path=None)

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


@pytest.mark.asyncio
async def test_nomad_mode_does_not_obey_legacy_supervisor_stop_receipt(tmp_path: Path) -> None:
    legacy_control = tmp_path / "runtime-supervisor-control.json"
    legacy_control.write_text('{"desired":"stopped"}', encoding="utf-8")
    runner, store = _runner(tmp_path, control_path=None)
    try:
        heartbeat = asyncio.create_task(runner._heartbeat())
        await asyncio.sleep(0.05)
        assert not runner.stop_event.is_set()
        runner.request_stop("NOMAD_CTRL_BREAK")
        await asyncio.wait_for(heartbeat, timeout=1)
        assert runner.stop_reason == "NOMAD_CTRL_BREAK"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_legacy_mode_still_obeys_supervisor_stop_receipt(tmp_path: Path) -> None:
    control = tmp_path / "runtime-supervisor-control.json"
    control.write_text('{"desired":"stopped"}', encoding="utf-8")
    runner, store = _runner(tmp_path, control_path=control)
    try:
        await asyncio.wait_for(runner._heartbeat(), timeout=1)
        assert runner.stop_event.is_set()
        assert runner.stop_reason == "SUPERVISOR_STOP_REQUESTED"
    finally:
        store.close()


def test_nomad_jobspec_owns_only_shadow_process_lifecycle() -> None:
    jobspec = JOBSPEC.read_text(encoding="utf-8")

    assert 'job "live15-kalshi-sdk-ws-shadow"' in jobspec
    assert 'type        = "service"' in jobspec
    assert 'attribute = "${attr.os.name}"' in jobspec
    assert 'value     = "windows"' in jobspec
    assert 'driver       = "raw_exec"' in jobspec
    assert "LIVE15_KALSHI_SDK_SHADOW_LIFECYCLE_OWNER" in jobspec
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
