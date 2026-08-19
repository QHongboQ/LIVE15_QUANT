from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    ("module_name", "entrypoint_name"),
    [
        ("btc_price_test", "rest_main"),
        ("btc_stream", "btc_stream_main"),
        ("market_stream", "stream_main"),
    ],
)
def test_compatibility_entry_is_import_safe(module_name: str, entrypoint_name: str) -> None:
    module = importlib.import_module(module_name)

    assert callable(getattr(module, entrypoint_name))
