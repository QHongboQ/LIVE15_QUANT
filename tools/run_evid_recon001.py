"""Reconcile H0/H1/H2 evidence without training or mutating runtime stores."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from live15_quant.evidence_reconciliation import (
    H0_PROVENANCE,
    H1_PROVENANCE,
    H2_PROVENANCE,
    select_stratified_markets,
)
from live15_quant.historical_bulk import HistoricalBulkStore
from live15_quant.historical_providers import (
    DepthFeedHistoricalOrderbookProvider,
    HistoricalMarketRecord,
    KalshiOfficialHistoricalProvider,
    ProviderProvenance,
)
from live15_quant.sequence_evidence import (
    SequenceConfig,
    TradeObservation,
    build_trade_sequences,
    classify_path_readiness,
    materialize_sequence_manifest,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path(r"D:\LIVE15_QUANT")
HIST003_DB = SOURCE_ROOT / "data/research/hist003/hist003_official.sqlite3"
H0_DB = SOURCE_ROOT / "data/live15.sqlite3"
TRAINABLE_DB = SOURCE_ROOT / "data/current_trainable.sqlite3"
ARCHIVE_DB = SOURCE_ROOT / "data/ws_archive_manifest.sqlite3"
OUTPUT_ROOT = SOURCE_ROOT / "data/research/evid_recon001"
CRYPTO_ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
SOURCE_DATASET_ID = "historical-research-f2d529adfb95080971becdaf"


def _code_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _load_market_metadata(path: Path) -> list[dict[str, object]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [
            {
                "ticker": ticker,
                "event_ticker": event_ticker,
                "asset": asset,
                "series": series,
                "open_time": open_time,
                "close_time": close_time,
                "status": status,
                "result": result,
                "raw_json": raw_json,
            }
            for (
                ticker,
                event_ticker,
                asset,
                series,
                open_time,
                close_time,
                status,
                result,
                raw_json,
            ) in connection.execute(
                "SELECT ticker,event_ticker,asset,series,open_time,close_time,status,result,"
                "raw_json "
                "FROM markets WHERE asset IN (?,?,?,?,?,?,?)",
                CRYPTO_ASSETS,
            )
        ]
    finally:
        connection.close()


def _load_trade_observations(path: Path) -> tuple[list[TradeObservation], dict[str, object]]:
    """Load only the newly materialized, read-only H1 trade subset."""

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    observations: list[TradeObservation] = []
    assets: Counter[str] = Counter()
    tickers: set[str] = set()
    days: set[str] = set()
    try:
        query = (
            "SELECT m.ticker,m.event_ticker,m.asset,m.open_time,m.close_time,"
            "t.trade_id,t.created_time,t.raw_json FROM trades t "
            "JOIN markets m ON m.ticker=t.ticker "
            "ORDER BY m.ticker,t.created_time,t.trade_id"
        )
        for (
            ticker,
            event_id,
            asset,
            open_time,
            close_time,
            trade_id,
            created,
            raw,
        ) in connection.execute(query):
            payload = json.loads(raw)
            timestamp = _parse_time(str(created))
            observations.append(
                TradeObservation(
                    ticker=str(ticker),
                    event_id=str(event_id),
                    asset=str(asset),
                    event_start=_parse_time(str(open_time)),
                    event_end=_parse_time(str(close_time)),
                    timestamp=timestamp,
                    trade_id=str(trade_id),
                    price=Decimal(str(payload["yes_price"])),
                    quantity=Decimal(str(payload["count"])),
                    taker_side=str(payload.get("taker_side"))
                    if payload.get("taker_side")
                    else None,
                )
            )
            assets[str(asset)] += 1
            tickers.add(str(ticker))
            days.add(timestamp.date().isoformat())
    finally:
        connection.close()
    return observations, {
        "trade_rows": len(observations),
        "markets_with_trades": len(tickers),
        "trade_rows_by_asset": dict(sorted(assets.items())),
        "independent_trade_days": len(days),
        "trade_days": sorted(days),
        "source_dataset_id": SOURCE_DATASET_ID,
        "source_provenance": H1_PROVENANCE,
    }


def _market_record(item: dict[str, object]) -> HistoricalMarketRecord:
    provenance = ProviderProvenance(
        "kalshi_official", H1_PROVENANCE, "historical_markets", datetime.now(UTC)
    )
    return HistoricalMarketRecord(
        str(item["ticker"]),
        str(item["event_ticker"]),
        str(item["series"]) if item["series"] else None,
        _parse_time(str(item["open_time"])),
        _parse_time(str(item["close_time"])),
        str(item["status"]),
        str(item["result"]) if item["result"] else None,
        provenance,
        json.loads(str(item["raw_json"])),
    )


def _acquire_h1(selection: dict[str, object], output_dir: Path) -> dict[str, object]:
    selected = selection["selected_markets"]
    db_path = output_dir / "h1_stratified.sqlite3"
    store = HistoricalBulkStore(db_path)
    api_calls = Counter[str]()
    failures: list[dict[str, str]] = []
    completed = 0
    try:
        with KalshiOfficialHistoricalProvider() as provider:
            for raw in selected:
                item = dict(raw)
                asset = str(item["asset"])
                ticker = str(item["ticker"])
                record = _market_record(item)
                store.insert_market(asset, str(item["series"]), record)
                stage = f"detail:{ticker}"
                state = store.checkpoint_state("kalshi_official", asset, stage)
                if state is not None and state[1]:
                    completed += 1
                    continue
                attempt = 0
                while True:
                    try:
                        trades = provider.trades(
                            ticker=ticker,
                            min_ts=int(record.open_time.timestamp()),
                            max_ts=int(record.close_time.timestamp()),
                            max_pages=100,
                            limit=1000,
                        )
                        api_calls["historical_trades"] += 1
                        for trade in trades:
                            store.insert_trade(trade)
                        candles = provider.candlesticks(
                            ticker,
                            start=record.open_time,
                            end=record.close_time,
                            period_interval=1,
                        )
                        api_calls["historical_candlesticks"] += 1
                        for candle in candles:
                            store.insert_candle(ticker, candle)
                        store.checkpoint(
                            "kalshi_official",
                            asset,
                            stage,
                            cursor=None,
                            completed=True,
                            retries=attempt,
                        )
                        completed += 1
                        break
                    except Exception as error:
                        message = str(error)
                        if "429" in message and attempt < 1:
                            attempt += 1
                            time.sleep(5)
                            continue
                        failures.append(
                            {
                                "ticker": ticker,
                                "class": type(error).__name__,
                                "message": message[:240],
                            }
                        )
                        store.checkpoint(
                            "kalshi_official",
                            asset,
                            stage,
                            cursor=None,
                            completed=False,
                            failed=True,
                            retries=attempt,
                            last_error=message[:240],
                        )
                        break
                time.sleep(0.15)
            store.metadata("selection_policy", selection)
            store.metadata("source_dataset_id", SOURCE_DATASET_ID)
            store.metadata("provenance", H1_PROVENANCE)
            store.connection.commit()
            counts = store.counts()
    finally:
        store.close()
    return {
        "database": str(db_path),
        "selected_markets": len(selected),
        "completed_markets": completed,
        "trade_rows": int(counts["trades"]),
        "candle_rows": int(counts["candles"]),
        "conflict_rows": int(counts["conflicts"]),
        "api_calls": dict(sorted(api_calls.items())),
        "failures": failures,
        "provenance": H1_PROVENANCE,
        "resumable": True,
    }


def _load_h0_summary() -> dict[str, object]:
    connection = sqlite3.connect(f"file:{H0_DB}?mode=ro", uri=True)
    trainable = sqlite3.connect(f"file:{TRAINABLE_DB}?mode=ro", uri=True)
    archive = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
    try:
        quote_first_last = (
            connection.execute(
                "SELECT received_timestamp FROM kalshi_prediction_quotes ORDER BY id ASC LIMIT 1"
            ).fetchone()[0],
            connection.execute(
                "SELECT received_timestamp FROM kalshi_prediction_quotes ORDER BY id DESC LIMIT 1"
            ).fetchone()[0],
        )
        underlying_first_last = (
            connection.execute(
                "SELECT received_timestamp FROM underlying_observations ORDER BY id ASC LIMIT 1"
            ).fetchone()[0],
            connection.execute(
                "SELECT received_timestamp FROM underlying_observations ORDER BY id DESC LIMIT 1"
            ).fetchone()[0],
        )
        trainable_events_by_day = trainable.execute(
            "SELECT substr(settlement_timestamp,1,10),COUNT(*) FROM current_trainable_events "
            "GROUP BY 1 ORDER BY 1"
        ).fetchall()
        trainable_events_by_asset = trainable.execute(
            "SELECT asset,COUNT(*) FROM current_trainable_events GROUP BY asset ORDER BY asset"
        ).fetchall()
        quote_counts = connection.execute(
            "SELECT COUNT(*) FROM kalshi_prediction_quotes"
        ).fetchone()[0]
        underlying_counts = connection.execute(
            "SELECT COUNT(*) FROM underlying_observations"
        ).fetchone()[0]
        gaps = connection.execute(
            "SELECT COUNT(*),SUM(CASE WHEN recovered=1 THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN recovered=0 THEN 1 ELSE 0 END) FROM data_gaps"
        ).fetchone()
        checkpoints = connection.execute(
            "SELECT COUNT(*),MIN(received_timestamp),MAX(received_timestamp),"
            "COUNT(DISTINCT substr(received_timestamp,1,10)) FROM kalshi_ws_book_checkpoints"
        ).fetchone()
        orderbook_events = connection.execute(
            "SELECT MAX(id) FROM kalshi_ws_orderbook_events"
        ).fetchone()[0]
        archive_days = archive.execute(
            "SELECT substr(first_received_timestamp,1,10),COUNT(*),SUM(event_count) "
            "FROM ws_retention_chunks GROUP BY 1 ORDER BY 1"
        ).fetchall()
        return {
            "provenance": H0_PROVENANCE,
            "source_tables": [
                "data/live15.sqlite3:kalshi_prediction_quotes",
                "data/live15.sqlite3:underlying_observations",
                "data/live15.sqlite3:kalshi_ws_book_checkpoints",
                "data/live15.sqlite3:kalshi_ws_orderbook_events",
                "data/live15.sqlite3:data_gaps",
                "data/current_trainable.sqlite3:current_trainable_events",
                "data/ws_archive_manifest.sqlite3:ws_retention_chunks",
            ],
            "earliest_recorded_timestamp": min(quote_first_last[0], underlying_first_last[0]),
            "latest_recorded_timestamp": max(quote_first_last[1], underlying_first_last[1]),
            "independent_utc_days": len({row[0] for row in trainable_events_by_day}),
            "independent_events": sum(int(row[1]) for row in trainable_events_by_day),
            "assets": sorted(str(row[0]) for row in trainable_events_by_asset),
            "per_day_event_counts": {
                str(day): int(count) for day, count in trainable_events_by_day
            },
            "per_asset_event_counts": {
                str(asset): int(count) for asset, count in trainable_events_by_asset
            },
            "quote_rows": int(quote_counts),
            "underlying_rows": int(underlying_counts),
            "l2_checkpoints": {
                "rows": int(checkpoints[0]),
                "earliest": checkpoints[1],
                "latest": checkpoints[2],
                "independent_days": int(checkpoints[3]),
            },
            "atomic_orderbook_event_rows_retained": int(orderbook_events),
            "archive_chunks_by_day": {
                str(day): {"chunks": int(chunks), "events": int(events)}
                for day, chunks, events in archive_days
            },
            "gaps": {
                "rows": int(gaps[0]),
                "recovered": int(gaps[1] or 0),
                "unrecovered": int(gaps[2] or 0),
            },
            "gap_quarantine": "active health reports 16 gaps; no inference or repair performed",
            "l2_snapshot_available": True,
            "atomic_delta_available": True,
            "h0_only_path_sequence_materialized": False,
        }
    finally:
        connection.close()
        trainable.close()
        archive.close()


def _depthfeed_snapshot_probe(output_dir: Path) -> dict[str, object]:
    end = datetime.now(UTC).replace(microsecond=0)
    start = end - timedelta(days=7)
    report: dict[str, object] = {
        "provenance": H2_PROVENANCE,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "max_days": 7,
        "snapshot_count": 0,
        "assets": [],
        "independent_days": 0,
        "errors": [],
        "delta_status": "H2_DELTA_UNAVAILABLE_PLAN_LIMIT",
        "delta_probe": "HTTP_402_KNOWN_FREE_PLAN_LIMIT; no retry",
        "checkpoint_resume": True,
    }
    rows: list[dict[str, object]] = []
    os.environ["DEPTHFEED_BASE_URL"] = "https://api.depthfeed.com"
    adapter = None
    try:
        adapter = DepthFeedHistoricalOrderbookProvider.from_project_secret(project_root=SOURCE_ROOT)
        discovered = adapter.discover_markets(limit=50)
        report["discovered_markets"] = len(discovered)
        selected: list[tuple[str, str]] = []
        seen: set[str] = set()
        for priority in CRYPTO_ASSETS:
            for item in discovered:
                asset = str(item.get("base_asset", "")).upper()
                ticker = item.get("ticker")
                if asset == priority and isinstance(ticker, str) and asset not in seen:
                    selected.append((asset, ticker))
                    seen.add(asset)
                    break
            if len(selected) >= 3:
                break
        report["selected_assets"] = [asset for asset, _ticker in selected]
        for asset, ticker in selected:
            checkpoint = output_dir / f"depthfeed-{asset}.json"
            if checkpoint.is_file():
                rows.extend(json.loads(checkpoint.read_text(encoding="utf-8")).get("rows", []))
                continue
            attempt = 0
            while True:
                try:
                    snapshots = adapter.snapshots(ticker, max_pages=1, limit=100)
                    asset_rows = [
                        {
                            "ticker": snapshot.ticker,
                            "asset": asset,
                            "received_timestamp": snapshot.received_timestamp.isoformat(),
                            "provenance": H2_PROVENANCE,
                            "quality_class": "HISTORICAL_L2_SNAPSHOT",
                        }
                        for snapshot in snapshots
                        if start <= snapshot.received_timestamp <= end
                    ]
                    checkpoint.write_text(
                        json.dumps({"asset": asset, "rows": asset_rows}, sort_keys=True),
                        encoding="utf-8",
                    )
                    rows.extend(asset_rows)
                    break
                except Exception as error:
                    if "429" in str(error) and attempt < 1:
                        attempt += 1
                        time.sleep(5)
                        continue
                    report["errors"].append(
                        {"asset": asset, "class": type(error).__name__, "message": str(error)[:240]}
                    )
                    break
            time.sleep(2)
    except Exception as error:
        report["errors"].append({"class": type(error).__name__, "message": str(error)[:240]})
    finally:
        if adapter is not None:
            adapter.close()
    report["snapshot_count"] = len(rows)
    report["assets"] = sorted({str(row["asset"]) for row in rows})
    report["independent_days"] = len({str(row["received_timestamp"])[:10] for row in rows})
    report["snapshot_readiness"] = (
        "MICROSTRUCTURE_SNAPSHOT_READY" if rows else "MICROSTRUCTURE_SNAPSHOT_BLOCKED"
    )
    report["tlob_readiness"] = "TLOB_BLOCKED" if not rows else "TLOB_RESEARCH_ONLY"
    report["baseline_lob_readiness"] = (
        "BLOCKED_NO_SNAPSHOTS" if not rows else "SNAPSHOT_ONLY_RESEARCH"
    )
    (output_dir / "depthfeed_snapshot_manifest.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--target-days", type=int, default=7)
    parser.add_argument("--events-per-asset-day", type=int, default=3)
    parser.add_argument("--skip-depthfeed", action="store_true")
    parser.add_argument("--reuse-depthfeed", action="store_true")
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    markets = _load_market_metadata(HIST003_DB)
    selection = select_stratified_markets(
        markets,
        target_days=args.target_days,
        events_per_asset_day=args.events_per_asset_day,
        assets=CRYPTO_ASSETS,
    )
    selection_dict = selection.to_dict()
    (output_dir / "h1_selection_manifest.json").write_text(
        json.dumps(selection_dict, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    h1 = _acquire_h1(selection_dict, output_dir)
    trades, h1_source = _load_trade_observations(output_dir / "h1_stratified.sqlite3")
    rows, sequence = build_trade_sequences(trades, SequenceConfig())
    sequence.update(h1_source)
    sequence["sequence_calendar_days"] = int(sequence.get("independent_days", 0))
    days = int(sequence.get("independent_trade_days", 0))
    sequence["independent_days"] = days
    train_days, validation_days = (3, 1)
    folds = max(0, days - train_days - validation_days + 1)
    sequence["fold_plan"] = {
        "mode": "expanding",
        "train_days": train_days,
        "validation_days": validation_days,
        "step_days": 1,
        "purge_embargo_seconds": 600,
        "available_validation_folds": folds,
    }
    sequence["fold_count"] = folds
    sequence["path_readiness"] = classify_path_readiness(sequence)
    sequence_manifest = materialize_sequence_manifest(
        rows=rows,
        summary=sequence,
        output_dir=output_dir / "sequence",
        source_dataset_id=SOURCE_DATASET_ID,
        code_sha=_code_sha(),
        config=SequenceConfig(),
    )
    depth_manifest = output_dir / "depthfeed_snapshot_manifest.json"
    if args.skip_depthfeed:
        h2 = {"status": "SKIPPED"}
    elif args.reuse_depthfeed and depth_manifest.is_file():
        h2 = json.loads(depth_manifest.read_text(encoding="utf-8"))
    else:
        h2 = _depthfeed_snapshot_probe(output_dir)
    h0 = _load_h0_summary()
    report = {
        "report": "EVID-RECON-001",
        "code_sha": _code_sha(),
        "h0": h0,
        "h1": {
            "selection": selection_dict,
            "acquisition": h1,
            "sequence": sequence,
            "sequence_manifest_id": sequence_manifest["manifest_id"],
        },
        "h2": h2,
        "combined": {
            "path_ready_days": int(sequence.get("independent_days", 0)),
            "walk_forward_folds": folds,
            "path_readiness": sequence["path_readiness"],
            "snapshot_readiness": h2.get("snapshot_readiness", "SKIPPED"),
            "delta_readiness": "H0_ATOMIC_DELTAS_AVAILABLE; H2_TICKS_UNAVAILABLE_PLAN_LIMIT",
        },
        "dataset_v2_touched": False,
        "holdout_accessed": False,
        "recorder_changed": False,
        "model_training": False,
    }
    (output_dir / "evid_recon001_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
