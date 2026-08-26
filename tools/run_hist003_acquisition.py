"""Run the bounded, resumable HIST-003 official-history acquisition."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from live15_quant.historical_bulk import (
    HIST003_DEFAULT_DETAIL_MARKET_CAP,
    SERIES_BY_ASSET,
    HistoricalBulkStore,
    acquire_official,
    dataset_identity,
    estimate_plan,
    resolve_window,
)
from live15_quant.historical_providers import (
    DEPTHFEED_NOT_CONFIGURED,
    KalshiOfficialHistoricalProvider,
    depthfeed_key_status,
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--detail-market-cap", type=int, default=HIST003_DEFAULT_DETAIL_MARKET_CAP)
    parser.add_argument("--safe-headroom-bytes", type=int, default=5 * 1024**3)
    return parser.parse_args()


def _preflight(
    provider: KalshiOfficialHistoricalProvider,
    window,
) -> tuple[int, dict[str, int]]:
    """Count only the approved universe inside the exact window."""

    total = 0
    pages: dict[str, int] = {}
    for asset, series in SERIES_BY_ASSET.items():
        count = 0
        page_count = 0
        for records, _cursor, page_number in provider.market_pages(
            series_ticker=series, max_pages=100, limit=1000
        ):
            page_count = page_number
            count += sum(
                record.close_time > window.start and record.open_time < window.end
                for record in records
            )
            if records and min(record.close_time for record in records) <= window.start:
                break
        total += count
        pages[asset] = page_count
    return total, pages


def _cutoff_dict(cutoff: object) -> dict[str, object]:
    return {
        "market_settled_ts": cutoff.market_settled_timestamp.isoformat(),
        "trades_created_ts": cutoff.trades_created_timestamp.isoformat(),
        "orders_updated_ts": cutoff.orders_updated_timestamp.isoformat(),
        "provider": cutoff.provider.provider_id,
    }


def main() -> int:
    args = _args()
    if args.detail_market_cap <= 0:
        raise SystemExit("detail-market-cap must be positive")
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = output_dir / "hist003_official.sqlite3"
    plan_path = output_dir / "hist003_plan.json"
    manifest_path = output_dir / "hist003_manifest.json"

    with KalshiOfficialHistoricalProvider() as provider:
        cutoff = provider.cutoff()
        window = resolve_window(cutoff.market_settled_timestamp, days=90)
        market_count, page_counts = _preflight(provider, window)

        disk = shutil.disk_usage(output_dir)
        depth_status = depthfeed_key_status()
        plan = estimate_plan(
            market_count=market_count,
            window=window,
            detail_market_cap=min(args.detail_market_cap, max(1, market_count)),
            free_bytes=disk.free,
            depthfeed_status=depth_status,
        )
        plan_report = {
            "report": "HIST-003_PREFLIGHT",
            "code_sha": args.code_sha,
            "cutoff": _cutoff_dict(cutoff),
            "window": plan.to_dict(),
            "universe": dict(SERIES_BY_ASSET),
            "metadata_page_counts": page_counts,
            "disk": {"total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free},
            "dataset_v2_touched": False,
            "holdout_accessed": False,
            "recorder_changed": False,
        }
        plan_path.write_text(
            json.dumps(plan_report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if not plan.storage_headroom_ok:
            plan_report["status"] = "HIST003_STORAGE_HEADROOM_BLOCKED"
            plan_path.write_text(
                json.dumps(plan_report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(json.dumps(plan_report, sort_keys=True))
            return 2

        store = HistoricalBulkStore(db_path)
        try:
            acquisition = acquire_official(
                provider=provider,
                store=store,
                window=window,
                detail_market_cap=plan.detail_market_cap,
            )
            counts = store.counts()
            store.metadata("code_sha", args.code_sha)
            store.metadata(
                "window", {"start": window.start.isoformat(), "end": window.end.isoformat()}
            )
            store.metadata("cutoff", _cutoff_dict(cutoff))
            store.metadata("universe", SERIES_BY_ASSET)
            store.metadata("depthfeed_status", depth_status)
            store.connection.commit()
        finally:
            store.close()

    dataset_id = dataset_identity(
        code_sha=args.code_sha,
        window=window,
        counts=counts,
        manifests=(
            {
                "provider": "kalshi_official",
                "database": db_path.name,
                "api_calls": acquisition["api_calls"],
            },
        ),
    )
    finished = datetime.now(UTC)
    report = {
        "report": "HIST-003",
        "status": "H2_PENDING_DEPTHFEED_CREDENTIALS"
        if depth_status == DEPTHFEED_NOT_CONFIGURED
        else "OFFICIAL_KALSHI_ACQUISITION_COMPLETE",
        "code_sha": args.code_sha,
        "dataset_id": dataset_id,
        "dataset_name": "HistoricalResearchDataset",
        "cutoff": _cutoff_dict(cutoff),
        "window": {"start": window.start.isoformat(), "end": window.end.isoformat(), "days": 90},
        "universe": dict(SERIES_BY_ASSET),
        "provider": "kalshi_official",
        "depthfeed_status": depth_status,
        "acquisition": acquisition,
        "counts": counts,
        "database": db_path.name,
        "finished_at": finished.isoformat(),
        "dataset_v2_touched": False,
        "holdout_accessed": False,
        "recorder_changed": False,
        "model_training": False,
        "fold_plan": {
            "status": "PLAN_ONLY",
            "mode": "expanding",
            "purge_embargo_seconds": 600,
            "note": (
                "Fold PLAN requires decision-row materialization; no models trained in HIST-003."
            ),
        },
        "eligibility": {
            "structured_path_terminal": "ELIGIBLE_H1_OFFICIAL",
            "microstructure": "ELIGIBLE_ONLY_H0_OR_H2",
            "event_delta": "ELIGIBLE_ONLY_WHERE_H2_TICKS_EXIST",
            "sequence_models": "INSUFFICIENT_SEQUENCE_EVIDENCE",
        },
        "leakage_rules": [
            "completed candle only",
            "source timestamp <= decision timestamp",
            "no future-nearest, interpolation, forward-fill, or zero substitution",
            "Dataset v2 and holdout isolated",
        ],
    }
    manifest_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
