"""Bounded, isolated Production SDK Recorder-host smoke launcher.

This script intentionally uses the same environment-backed Production
credential resolution as the managed runtime.  It never writes to the formal
Recorder database and never invokes any execution endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from live15_quant.config import Settings, load_settings
from live15_quant.kalshi_gateway.client import KalshiGatewayError, production_credentials
from live15_quant.kalshi_gateway.production_recorder_host import SdkProductionRecorderHost
from live15_quant.kalshi_gateway.smoke_result import write_smoke_result_atomic
from live15_quant.models import Asset
from live15_quant.storage import RecorderStore

RESULT_PATH = (
    Path(__file__).resolve().parents[1] / "runtime" / "sdk-production-host-smoke-result.json"
)


def _current_universe(settings: Settings) -> dict[Asset, str]:
    """Read the Recorder-owned current universe without touching its store."""

    try:
        payload = json.loads(settings.recorder_health_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Recorder health/current-market projection is unavailable") from error
    current = payload.get("current_markets")
    if not isinstance(current, dict):
        raise RuntimeError("Recorder health/current-market projection is invalid")
    universe = {
        asset: ticker
        for asset in Asset
        if isinstance((ticker := current.get(asset.value)), str) and ticker
    }
    if len(universe) != len(Asset):
        raise RuntimeError(f"Recorder current universe is incomplete: {len(universe)}/{len(Asset)}")
    return universe


def _safe_error(error: BaseException) -> dict[str, str]:
    message = str(error)
    if isinstance(error, KalshiGatewayError) and "credential" in message.lower():
        return {"error_type": type(error).__name__, "message": message}
    return {"error_type": type(error).__name__, "message": message[:300]}


def _error_text(error: BaseException | None) -> str | None:
    if error is None:
        return None
    safe = _safe_error(error)
    return f"{safe['error_type']}: {safe['message']}"


def _result_template(started_at: datetime, duration_seconds: float) -> dict[str, object]:
    return {
        "started_at": started_at.isoformat(timespec="microseconds"),
        "finished_at": None,
        "duration_seconds": duration_seconds,
        "status": "FAILED",
        "synchronized_count": 0,
        "expected_market_count": 0,
        "unrecovered_gap_count": 0,
        "reconnect_count": 0,
        "rows_before": 0,
        "rows_after": 0,
        "rows_added": 0,
        "checkpoint_before": None,
        "checkpoint_after": None,
        "checkpoint_advanced": False,
        "final_flush_count": 0,
        "graceful_shutdown": False,
        "last_error": None,
    }


async def _run(
    duration_seconds: float,
    settings: Settings,
    *,
    started_at: datetime,
    result_path: Path,
) -> dict[str, object]:
    # Validate the formal config before creating any socket/host task.  This
    # reports missing configuration keys without ever reading key material into
    # this launcher output.
    result = _result_template(started_at, duration_seconds)
    store: RecorderStore | None = None
    host: SdkProductionRecorderHost | None = None
    task: asyncio.Task[None] | None = None
    stop: asyncio.Event | None = None
    error: BaseException | None = None
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    committed = 0
    states: list[str] = []
    try:
        production_credentials(settings)
        universe = _current_universe(settings)
        result["expected_market_count"] = len(universe)
        temporary_directory = tempfile.TemporaryDirectory(prefix="live15-sdk-host-smoke-")
        directory = Path(temporary_directory.name)
        store = RecorderStore(directory / "isolated-recorder.sqlite3")
        result["rows_before"] = store.count("kalshi_ws_orderbook_events")

        def on_committed(events: tuple[Any, ...]) -> None:
            nonlocal committed
            committed += len(events)

        host = SdkProductionRecorderHost(
            settings=settings,
            store=store,
            universe=lambda: universe,
            on_committed=on_committed,
            on_transport_state=lambda state, _at: states.append(state),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(host.run(stop), name="sdk-production-host-smoke")
        await asyncio.sleep(duration_seconds)
    except BaseException as caught:
        error = caught
    finally:
        if stop is not None:
            stop.set()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=30.0)
            except BaseException as caught:
                if error is None:
                    error = caught
        if store is not None:
            summary = None if host is None else host.final_summary
            rows_after = store.count("kalshi_ws_orderbook_events")
            result["rows_after"] = rows_after
            result["rows_added"] = rows_after - int(result["rows_before"])
            if summary is not None:
                result.update(
                    {
                        "synchronized_count": summary.synchronized_count,
                        "unrecovered_gap_count": summary.gap_count,
                        "reconnect_count": summary.reconnect_count,
                        "checkpoint_after": summary.checkpoint_sequence,
                        "checkpoint_advanced": summary.checkpoint_sequence is not None,
                        "final_flush_count": summary.flushed_events,
                    }
                )
            result["graceful_shutdown"] = error is None and summary is not None
        finished_at = datetime.now(UTC)
        result["finished_at"] = finished_at.isoformat(timespec="microseconds")
        result["duration_seconds"] = round((finished_at - started_at).total_seconds(), 3)
        acceptance_failure: str | None = None
        if result["graceful_shutdown"]:
            if result["synchronized_count"] != result["expected_market_count"]:
                acceptance_failure = "synchronization did not reach the expected market count"
            elif result["unrecovered_gap_count"] != 0:
                acceptance_failure = "unrecovered sequence gaps remain"
            elif result["rows_added"] <= 0:
                acceptance_failure = "isolated RecorderStore did not grow"
            elif not result["checkpoint_advanced"]:
                acceptance_failure = "consumer checkpoint did not advance"
        result["last_error"] = _error_text(error) or acceptance_failure
        result["status"] = "PASSED" if result["last_error"] is None else "FAILED"
        try:
            write_smoke_result_atomic(result_path, result)
        finally:
            if store is not None:
                store.close()
            if temporary_directory is not None:
                temporary_directory.cleanup()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_sdk_production_host_smoke")
    parser.add_argument("--duration-seconds", type=float, default=75.0)
    arguments = parser.parse_args(argv)
    if not 60.0 <= arguments.duration_seconds <= 120.0:
        parser.error("--duration-seconds must be within 60..120")
    started_at = datetime.now(UTC)
    try:
        result = asyncio.run(
            _run(
                arguments.duration_seconds,
                load_settings(),
                started_at=started_at,
                result_path=RESULT_PATH,
            )
        )
    except BaseException as error:
        result = _result_template(started_at, arguments.duration_seconds)
        result["finished_at"] = datetime.now(UTC).isoformat(timespec="microseconds")
        result["last_error"] = _error_text(error)
        write_smoke_result_atomic(RESULT_PATH, result)
    print("SDK_HOST_SMOKE_SUMMARY=" + json.dumps(result, default=str, sort_keys=True), flush=True)
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
