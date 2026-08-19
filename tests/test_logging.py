from __future__ import annotations

import json
import logging

from live15_quant.logging_config import JsonFormatter


def test_json_formatter_includes_structured_context() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="live15.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="connected",
        args=(),
        exc_info=None,
    )
    record.event = "provider_connected"
    record.symbol = "BTC-USD"

    payload = json.loads(formatter.format(record))

    assert payload["message"] == "connected"
    assert payload["event"] == "provider_connected"
    assert payload["symbol"] == "BTC-USD"
    assert payload["level"] == "INFO"
