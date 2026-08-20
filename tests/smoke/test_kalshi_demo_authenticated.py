from __future__ import annotations

import os
from pathlib import Path

import pytest

from live15_quant.config import load_settings
from live15_quant.providers.kalshi_demo import KalshiDemoCredentials, KalshiDemoReadOnlyClient

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        os.getenv("LIVE15_RUN_KALSHI_DEMO_AUDIT") != "1",
        reason="set LIVE15_RUN_KALSHI_DEMO_AUDIT=1 with Demo-only credentials",
    ),
]


def test_authenticated_demo_connectivity_is_read_only() -> None:
    settings = load_settings()
    if settings.kalshi_demo_api_key_id is None or settings.kalshi_demo_private_key_path is None:
        pytest.fail("Kalshi Demo audit requested but Demo credential references are missing")
    credentials = KalshiDemoCredentials(
        settings.kalshi_demo_api_key_id,
        Path(settings.kalshi_demo_private_key_path),
    )

    with KalshiDemoReadOnlyClient(settings, credentials) as client:
        result = client.audit()

    assert result.environment == "demo"
    assert result.authenticated is True
    assert result.balance_dollars is not None
    assert result.positions_readable and result.orders_readable and result.fills_readable
    assert result.capabilities.client_write_operations is False
