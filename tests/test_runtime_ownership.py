from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from live15_quant.runtime_ownership import ComponentHealth, ServiceHealthObservation

ROOT = Path(__file__).parents[1]


def test_machine_readable_registry_has_one_owner_and_recovery_authority_per_component() -> None:
    payload = json.loads((ROOT / "deploy/windows/runtime-ownership.json").read_text())
    assert payload["principle"] == "ONE COMPONENT ONE OWNER ONE HEALTH TRUTH ONE RECOVERY AUTHORITY"
    components = payload["components"]
    assert len({item["component"] for item in components}) == len(components)
    for item in components:
        assert item["owner_type"]
        assert item["owner_id"]
        assert item["health_source"]
        assert item["restart_authority"]


def test_current_windows_service_overrides_stale_supervisor_telemetry() -> None:
    now = datetime(2026, 8, 28, tzinfo=UTC)
    resolved = ServiceHealthObservation(
        service_running=True,
        heartbeat_at=now - timedelta(seconds=91),
        checked_at=now,
        stale_after_seconds=90,
    ).resolve()

    assert resolved is ComponentHealth.STALE_TELEMETRY


def test_stopped_service_is_not_inferred_from_historical_status_file() -> None:
    now = datetime(2026, 8, 28, tzinfo=UTC)
    resolved = ServiceHealthObservation(
        service_running=False,
        heartbeat_at=now,
        checked_at=now,
        stale_after_seconds=90,
    ).resolve()

    assert resolved is ComponentHealth.STOPPED
