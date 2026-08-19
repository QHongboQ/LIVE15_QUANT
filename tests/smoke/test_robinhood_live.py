from __future__ import annotations

import os
from datetime import timedelta

import pytest

from live15_quant.config import Settings
from live15_quant.models import Asset, DataRole, SupportLevel
from live15_quant.providers.robinhood_15min import Robinhood15MinuteProvider

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        os.getenv("LIVE15_RUN_SMOKE") != "1",
        reason="set LIVE15_RUN_SMOKE=1 to access public external services",
    ),
]


def test_robinhood_public_15minute_discovery_live() -> None:
    contracts = Robinhood15MinuteProvider(Settings()).discover()

    assert set(contract.asset for contract in contracts) == set(Asset)
    assert all(
        contract.end_time - contract.start_time == timedelta(minutes=15) for contract in contracts
    )
    assert all(contract.source_url.startswith("https://robinhood.com/") for contract in contracts)
    assert all(contract.quote.is_executable is False for contract in contracts)
    assert all(contract.quote.role is DataRole.CONTRACT_MARKET_QUOTE for contract in contracts)
    assert all(contract.quote.no_probability is None for contract in contracts)
    assert all(contract.venue is None for contract in contracts)
    assert all(contract.settlement.asset is contract.asset for contract in contracts)
    assert all(
        contract.quote.availability
        is (
            SupportLevel.UNSUPPORTED
            if contract.quote.yes_probability is None
            else SupportLevel.PARTIAL
        )
        for contract in contracts
    )
