"""Run one bounded, read-only probe for the verified HIST-002R providers."""

from __future__ import annotations

import argparse
import json
import os
from datetime import timedelta
from pathlib import Path

from live15_quant.historical_providers import (
    DEPTHFEED_INTEGRATION_READY_KEY_REQUIRED,
    DepthFeedHistoricalOrderbookProvider,
    KalshiOfficialHistoricalProvider,
    depthfeed_key_status,
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    return parser.parse_args()


def main() -> int:
    args = _args()
    report: dict[str, object] = {
        "report": "HIST-002R_BOUNDED_PROVIDER_PROBE",
        "code_sha": args.code_sha,
        "bulk_download_performed": False,
        "dataset_v2_touched": False,
        "holdout_accessed": False,
        "recorder_changed": False,
        "official": {},
        "depthfeed": {"key_status": depthfeed_key_status()},
    }

    official: dict[str, object] = {}
    try:
        with KalshiOfficialHistoricalProvider() as provider:
            cutoff = provider.cutoff()
            markets = provider.markets(series_ticker="KXBTC15M", max_pages=1, limit=1)
            official.update(
                {
                    "cutoff": True,
                    "markets": len(markets),
                    "cutoff_provider": cutoff.provider.provider_id,
                }
            )
            if markets:
                market = markets[0]
                trades = provider.trades(ticker=market.ticker, max_pages=1, limit=1)
                candles = provider.candlesticks(
                    market.ticker,
                    start=market.open_time,
                    end=min(market.close_time, market.open_time + timedelta(minutes=15)),
                    period_interval=1,
                )
                official.update({"trades": len(trades), "candlesticks": len(candles)})
    except Exception as error:  # bounded probe reports provider failure without affecting runtime
        official["error_class"] = type(error).__name__
        official["error"] = str(error)[:240]
    report["official"] = official

    if report["depthfeed"]["key_status"] != "DEPTHFEED_NOT_CONFIGURED":
        depthfeed: dict[str, object] = {
            "key_status": DEPTHFEED_INTEGRATION_READY_KEY_REQUIRED,
            "base_url_configured": bool(os.environ.get("DEPTHFEED_BASE_URL", "").strip()),
        }
        if depthfeed["base_url_configured"]:
            try:
                adapter = DepthFeedHistoricalOrderbookProvider.from_project_secret()
                markets = adapter.discover_markets(limit=1)
                depthfeed["markets"] = len(markets)
                if markets and isinstance(markets[0].get("ticker"), str):
                    snapshots = adapter.snapshots(str(markets[0]["ticker"]), max_pages=1, limit=1)
                    depthfeed["snapshots"] = len(snapshots)
                adapter.close()
            except Exception as error:  # optional provider failure is isolated
                depthfeed["error_class"] = type(error).__name__
                depthfeed["error"] = str(error)[:240]
        report["depthfeed"] = depthfeed

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
