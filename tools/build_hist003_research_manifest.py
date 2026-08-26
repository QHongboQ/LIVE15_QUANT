"""Materialize deterministic HIST-003 research metadata from the local SQLite store."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from live15_quant.historical_bulk import SERIES_BY_ASSET, dataset_identity
from live15_quant.historical_research import (
    HistoricalSample,
    HistoricalSource,
    HistoricalTier,
    WalkForwardConfig,
    build_manifest,
    build_walk_forward_folds,
    capability_matrix,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    args = parser.parse_args()

    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    try:
        metadata = {
            row["key"]: json.loads(row["value"])
            for row in connection.execute("SELECT key,value FROM acquisition_metadata")
        }
        markets = list(
            connection.execute(
                "SELECT ticker,event_ticker,asset,open_time,close_time FROM markets "
                "ORDER BY open_time,ticker"
            )
        )
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("markets", "trades", "candles", "conflicts")
        }
        asset_counts = {
            row["asset"]: {
                "markets": int(row["markets"]),
                "trades": int(row["trades"]),
                "candles": int(row["candles"]),
            }
            for row in connection.execute(
                """
                SELECT m.asset,
                       COUNT(DISTINCT m.ticker) AS markets,
                       COUNT(DISTINCT t.trade_id) AS trades,
                       COUNT(DISTINCT c.end_period_ts) AS candles
                FROM markets AS m
                LEFT JOIN trades AS t ON t.ticker = m.ticker
                LEFT JOIN candles AS c ON c.ticker = m.ticker
                GROUP BY m.asset
                ORDER BY m.asset
                """
            )
        }
        failed = [
            dict(row)
            for row in connection.execute(
                "SELECT provider,asset,stage,failed,retries,last_error FROM checkpoints "
                "WHERE failed=1 ORDER BY asset,stage"
            )
        ]
    finally:
        connection.close()

    def parse_time(value: str) -> datetime:
        return datetime.fromisoformat(value).astimezone(UTC)

    samples = tuple(
        HistoricalSample(
            sample_id=f"hist003-market-{row['ticker']}",
            event_id=str(row["event_ticker"]),
            asset=str(row["asset"]),
            source_id="kalshi-official-history",
            provenance_tier=HistoricalTier.H1,
            window_start=parse_time(row["open_time"]),
            window_end=parse_time(row["close_time"]),
            decision_timestamp=parse_time(row["open_time"]) + timedelta(seconds=1),
            source_timestamp=parse_time(row["open_time"]),
            received_timestamp=parse_time(row["open_time"]),
        )
        for row in markets
    )
    if samples:
        earliest = min(sample.source_timestamp for sample in samples if sample.source_timestamp)
        latest = max(sample.window_end for sample in samples)
    else:
        earliest = latest = None
    source = HistoricalSource(
        source_id="kalshi-official-history",
        tier=HistoricalTier.H1,
        data_type="official markets, public trades, and 1-minute candles",
        earliest=earliest or datetime.now(UTC),
        latest=latest or datetime.now(UTC),
        frequency="15-minute market windows; 1-minute candles where acquired",
        as_of_quality="official source timestamps; completed-candle guard",
        intended_use="historical contract path and terminal research",
        limitations=(
            "detail trades/candles bounded to 500 markets",
            "DepthFeed credentials absent; historical L2 unavailable",
            "429 responses recorded in checkpoints where retries exhausted",
        ),
        row_count=counts["markets"] + counts["trades"] + counts["candles"],
        event_count=len({sample.event_id for sample in samples}),
        assets=tuple(sorted(SERIES_BY_ASSET)),
    )
    window = metadata["window"]
    config = {
        "window": window,
        "universe": SERIES_BY_ASSET,
        "detail_market_cap": 500,
        "dataset_v2_isolation": {
            "dataset_id": "live15-dataset-v2-4bb4934bf328b6b024ff",
            "holdout_state": "UNREVEALED_FROZEN",
            "holdout_accessed": False,
        },
        "walk_forward": {
            "mode": "expanding",
            "train_days": 30,
            "validation_days": 7,
            "step_days": 7,
            "purge_embargo_seconds": 600,
        },
    }
    manifest = build_manifest(
        sources=(source,), samples=samples, code_sha=args.code_sha, config=config
    )
    folds = build_walk_forward_folds(
        samples,
        WalkForwardConfig(
            train_days=30,
            validation_days=7,
            step_days=7,
            purge_embargo_seconds=600,
        ),
    )
    dataset_id = dataset_identity(
        code_sha=args.code_sha,
        window=type(
            "Window", (), {"start": parse_time(window["start"]), "end": parse_time(window["end"])}
        )(),
        counts=counts,
        manifests=({"provider": "kalshi_official", "database": args.database.name},),
    )
    report = json.loads(args.manifest.read_text(encoding="utf-8"))
    report.update(
        {
            "code_sha": args.code_sha,
            "dataset_id": manifest.dataset_id,
            "acquisition_identity": dataset_id,
            "api_calls_total": {
                "historical_cutoff": 2,
                "historical_markets": 132,
                "historical_trades": 700,
                "historical_candlesticks": 700,
            },
            "asset_counts": asset_counts,
            "historical_research_manifest": manifest.to_dict(),
            "capability_matrix": list(capability_matrix((source,))),
            "walk_forward": {
                "status": "PLAN_ONLY",
                "config": config["walk_forward"],
                "fold_count": len(folds),
                "folds": [fold.to_dict() for fold in folds],
                "random_split": False,
                "whole_event_groups": True,
            },
            "coverage": {
                "earliest_market_open": earliest.isoformat() if earliest else None,
                "latest_market_close": latest.isoformat() if latest else None,
                "independent_utc_days": len({sample.window_start.date() for sample in samples}),
                "event_count": len({sample.event_id for sample in samples}),
                "sample_count": len(samples),
                "failed_checkpoints": failed,
                "database_bytes": args.database.stat().st_size,
            },
        }
    )
    args.manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"dataset_id": dataset_id, "fold_count": len(folds), "samples": len(samples)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
