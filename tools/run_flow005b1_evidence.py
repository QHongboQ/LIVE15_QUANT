"""Materialize FLOW-005B1 trade-sequence and bounded DepthFeed evidence."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from live15_quant.historical_providers import DepthFeedHistoricalOrderbookProvider
from live15_quant.sequence_evidence import (
    H2_PROVENANCE,
    DepthReadiness,
    SequenceConfig,
    TradeObservation,
    build_trade_sequences,
    classify_depth_readiness,
    classify_path_readiness,
    materialize_sequence_manifest,
    tlob_eligibility,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path(r"D:\LIVE15_QUANT")
HIST003_DB = SOURCE_ROOT / "data/research/hist003/hist003_official.sqlite3"
OUTPUT_ROOT = SOURCE_ROOT / "data/research/flow005b1"
SOURCE_DATASET_ID = "historical-research-f2d529adfb95080971becdaf"
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")


def _code_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.astimezone(UTC)


def load_trade_observations(path: Path) -> tuple[list[TradeObservation], dict[str, Any]]:
    """Read only the approved H1 trades and market identity from the HIST-003 SQLite store."""

    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    query = """
        SELECT m.ticker, m.event_ticker, m.asset, m.open_time, m.close_time,
               t.trade_id, t.created_time, t.raw_json
        FROM trades AS t JOIN markets AS m ON m.ticker=t.ticker
        WHERE m.asset IN ('BTC','ETH','SOL','XRP','DOGE','BNB','HYPE')
        ORDER BY m.ticker, t.created_time, t.trade_id
    """
    rows: list[TradeObservation] = []
    assets: Counter[str] = Counter()
    tickers: set[str] = set()
    days: set[str] = set()
    try:
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
            price = Decimal(str(payload["yes_price"]))
            quantity = Decimal(str(payload["count"]))
            timestamp = _parse_time(str(created))
            rows.append(
                TradeObservation(
                    ticker=str(ticker),
                    event_id=str(event_id),
                    asset=str(asset),
                    event_start=_parse_time(str(open_time)),
                    event_end=_parse_time(str(close_time)),
                    timestamp=timestamp,
                    trade_id=str(trade_id),
                    price=price,
                    quantity=quantity,
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
    return rows, {
        "trade_rows": len(rows),
        "markets_with_trades": len(tickers),
        "assets": sorted(assets),
        "trade_rows_by_asset": dict(sorted(assets.items())),
        "independent_trade_days": len(days),
        "trade_days": sorted(days),
        "source_dataset_id": SOURCE_DATASET_ID,
        "source_provenance": "H1_KALSHI_OFFICIAL_HISTORY",
    }


def _snapshot_record(snapshot: object, asset: str) -> dict[str, object]:
    yes = [{"price": str(level.price), "size": str(level.size)} for level in snapshot.yes]
    no = [{"price": str(level.price), "size": str(level.size)} for level in snapshot.no]
    best_yes = max((Decimal(item["price"]) for item in yes), default=None)
    best_no = max((Decimal(item["price"]) for item in no), default=None)
    yes_depth = sum((Decimal(item["size"]) for item in yes), Decimal("0"))
    no_depth = sum((Decimal(item["size"]) for item in no), Decimal("0"))
    total_depth = yes_depth + no_depth
    return {
        "ticker": snapshot.ticker,
        "asset": asset,
        "received_timestamp": snapshot.received_timestamp.isoformat(),
        "yes": yes,
        "no": no,
        "best_yes_bid": str(best_yes) if best_yes is not None else None,
        "best_no_bid": str(best_no) if best_no is not None else None,
        "yes_depth": str(yes_depth),
        "no_depth": str(no_depth),
        "top_depth_imbalance": str((yes_depth - no_depth) / total_depth) if total_depth else None,
        "provenance": H2_PROVENANCE,
        "quality_class": "HISTORICAL_L2_SNAPSHOT",
    }


def acquire_depthfeed_evidence(output_dir: Path, code_sha: str) -> dict[str, object]:
    """Make at most three bounded snapshot calls; never query or synthesize deltas."""

    end = datetime.now(UTC).replace(microsecond=0)
    start = end - timedelta(days=7)
    report: dict[str, object] = {
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "max_days": 7,
        "cadence": "provider_page_bounded",
        "provider": "depthfeed_kalshi_l2",
        "provenance": H2_PROVENANCE,
        "delta_status": "H2_DELTA_UNAVAILABLE_PLAN_LIMIT",
        "delta_probe": "HTTP_402_KNOWN_FREE_PLAN_LIMIT; no retry",
        "snapshot_count": 0,
        "assets": [],
        "events": 0,
        "independent_days": 0,
        "errors": [],
        "dataset_v2_touched": False,
        "holdout_accessed": False,
        "recorder_changed": False,
        "model_training": False,
    }
    rows: list[dict[str, object]] = []
    os.environ["DEPTHFEED_BASE_URL"] = "https://api.depthfeed.com"
    adapter = None
    try:
        adapter = DepthFeedHistoricalOrderbookProvider.from_project_secret(project_root=SOURCE_ROOT)
        discovered = adapter.discover_markets(limit=50)
        selected: list[tuple[str, str]] = []
        seen_assets: set[str] = set()
        for item in discovered:
            asset = str(item.get("base_asset", "")).upper()
            ticker = item.get("ticker")
            if asset in ASSETS and isinstance(ticker, str) and asset not in seen_assets:
                selected.append((asset, ticker))
                seen_assets.add(asset)
            if len(selected) >= 3:
                break
        report["discovered_markets"] = len(discovered)
        report["selected_assets"] = [asset for asset, _ in selected]
        for asset, ticker in selected:
            snapshots = adapter.snapshots(ticker, max_pages=1, limit=100)
            for snapshot in snapshots:
                if start <= snapshot.received_timestamp <= end:
                    rows.append(_snapshot_record(snapshot, asset))
    except Exception as error:
        report["errors"] = [{"class": type(error).__name__, "message": str(error)[:240]}]
    finally:
        if adapter is not None:
            adapter.close()
    snapshot_path = output_dir / "depthfeed_snapshot_rows.jsonl"
    output_dir.mkdir(parents=True, exist_ok=True)
    with snapshot_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in sorted(
            rows, key=lambda item: (str(item["ticker"]), str(item["received_timestamp"]))
        ):
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    report["snapshot_count"] = len(rows)
    report["assets"] = sorted({str(row["asset"]) for row in rows})
    report["events"] = len({str(row["ticker"]) for row in rows})
    report["independent_days"] = len({str(row["received_timestamp"])[:10] for row in rows})
    report["snapshot_readiness"] = classify_depth_readiness(report)
    report["tlob_readiness"] = tlob_eligibility(
        {**report, "has_continuous_sequence": False, "provenance": H2_PROVENANCE}
    )
    report["baseline_lob_readiness"] = (
        "DEEPLOB_MLPLOB_SNAPSHOT_COMPATIBLE_RESEARCH_ONLY" if rows else "BLOCKED_NO_SNAPSHOTS"
    )
    report["code_sha"] = code_sha
    (output_dir / "depthfeed_snapshot_manifest.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def build_report(
    output_dir: Path, code_sha: str, *, acquire_depthfeed: bool = True
) -> dict[str, object]:
    trades, source = load_trade_observations(HIST003_DB)
    config = SequenceConfig()
    rows, summary = build_trade_sequences(trades, config)
    summary.update(source)
    summary["fold_count"] = 0
    summary["fold_plan"] = {
        "mode": "expanding",
        "train_days": 30,
        "validation_days": 7,
        "step_days": 7,
        "purge_embargo_seconds": 600,
        "available_validation_folds": 0,
        "reason": "only one independent trade UTC day in HIST-003 detail coverage",
    }
    summary["path_readiness"] = classify_path_readiness(summary)
    sequence_manifest = materialize_sequence_manifest(
        rows=rows,
        summary=summary,
        output_dir=output_dir,
        source_dataset_id=SOURCE_DATASET_ID,
        code_sha=code_sha,
        config=config,
    )
    depth_manifest = output_dir / "depthfeed_snapshot_manifest.json"
    if acquire_depthfeed:
        depthfeed = acquire_depthfeed_evidence(output_dir, code_sha)
    elif depth_manifest.is_file():
        depthfeed = json.loads(depth_manifest.read_text(encoding="utf-8"))
    else:
        raise FileNotFoundError("cannot reuse missing DepthFeed manifest")
    report = {
        "report": "FLOW-005B1_EVIDENCE",
        "code_sha": code_sha,
        "historical_research_dataset_id": SOURCE_DATASET_ID,
        "sequence_manifest_id": sequence_manifest["manifest_id"],
        "sequence": summary,
        "depthfeed": depthfeed,
        "path_model_training_unlocked": summary["path_readiness"]
        == "SEQUENCE_READY_FOR_BOUNDED_MODEL_TRAINING",
        "micro_model_training_unlocked": depthfeed["snapshot_readiness"] == DepthReadiness.READY,
        "dataset_v2_touched": False,
        "holdout_accessed": False,
        "recorder_changed": False,
        "model_training": False,
    }
    (output_dir / "flow005b1_evidence_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_ROOT)
    parser.add_argument(
        "--reuse-depthfeed",
        action="store_true",
        help="reuse the already-acquired bounded DepthFeed result without another network call",
    )
    args = parser.parse_args()
    report = build_report(args.output_dir, _code_sha(), acquire_depthfeed=not args.reuse_depthfeed)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
