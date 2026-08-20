from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from live15_quant.config import Settings
from live15_quant.models import FreshnessState
from live15_quant.providers.pyth import PYTH_FEEDS, PythHermesClient, discovery_metadata


@pytest.mark.smoke
def test_official_pyth_registry_verifies_all_configured_underlying_feeds() -> None:
    if os.environ.get("LIVE15_RUN_SMOKE") != "1":
        pytest.skip("set LIVE15_RUN_SMOKE=1 to access Pyth's public feed registry")
    metadata = discovery_metadata()
    assert len(metadata) == len(PYTH_FEEDS) == 5
    assert {item["feed_id"] for item in metadata} == {feed_id for _, feed_id in PYTH_FEEDS.values()}


@pytest.mark.smoke
def test_authenticated_single_sse_connection_receives_all_five_feeds() -> None:
    if os.environ.get("LIVE15_RUN_SMOKE") != "1":
        pytest.skip("set LIVE15_RUN_SMOKE=1 to access Pyth Hermes")
    key_path = os.environ.get("LIVE15_PYTH_API_KEY_PATH")
    if not key_path:
        pytest.skip("configure the external LIVE15_PYTH_API_KEY_PATH")
    client = PythHermesClient(
        Settings(
            pyth_api_key_path=Path(key_path),
            pyth_stream_read_timeout_seconds=20,
        )
    )
    observed = {}
    errors: list[BaseException] = []
    finished = threading.Event()

    def consume() -> None:
        try:
            for batch in client.stream_batches():
                for item in batch.observations:
                    observed[item.asset] = item
                if set(observed) == set(PYTH_FEEDS):
                    return
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=consume, name="pyth-auth-smoke", daemon=True)
    thread.start()
    try:
        completed = finished.wait(30)
    finally:
        client.close()
        thread.join(timeout=5)

    assert completed, "authenticated Pyth SSE did not deliver all feeds within 30 seconds"
    assert not thread.is_alive()
    if errors:
        raise errors[0]
    assert set(observed) == set(PYTH_FEEDS)
    now = datetime.now(UTC)
    for asset, item in observed.items():
        assert item.feed_id == PYTH_FEEDS[asset][1]
        assert item.price > 0
        assert item.confidence is not None and item.confidence >= 0
        assert item.source_timestamp <= item.received_timestamp <= now
        assert item.freshness is FreshnessState.FRESH
