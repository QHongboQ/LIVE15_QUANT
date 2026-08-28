"""Acquire one exact, recent DepthFeed H2 probe from read-only H0 Recorder evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from live15_quant.canonical_evidence import (
    H2,
    CoverageScope,
    EvidenceRecord,
    build_canonical_evidence_snapshot,
    training_preflight,
)
from live15_quant.h2_l2_materialization import (
    REAL_PROVIDER_EVIDENCE,
    H2SnapshotEvidence,
    L2EventWindow,
    build_snapshot_sequences,
    canonical_microstructure_availability,
    evaluate_h2_overlap_with_tolerance,
    materialize_snapshot,
    summarize_h2_capabilities,
)
from live15_quant.h2_train_002 import (
    DEPTHFEED_RATE_LIMIT_BLOCKED,
    H0OverlapTarget,
    load_h0_overlap_references,
    run_bounded_depthfeed_request,
    select_recent_h0_overlap_target,
)
from live15_quant.historical_providers import (
    DepthFeedHistoricalOrderbookProvider,
    DepthFeedHttpError,
    HistoricalProviderError,
    validate_depthfeed_free_plan_range,
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recorder-db", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--base-url", default="https://api.depthfeed.com")
    return parser.parse_args()


def _target_report(target: H0OverlapTarget) -> dict[str, object]:
    return {
        "OVERLAP_TARGET_TICKER": target.ticker,
        "EVENT_ID": target.event_id,
        "ASSET": target.asset,
        "SERIES": target.series,
        "WINDOW_START": target.window_start.isoformat(),
        "WINDOW_END": target.window_end.isoformat(),
        "H0_EVIDENCE_RANGE": {
            "start": target.h0_evidence_start.isoformat(),
            "end": target.h0_evidence_end.isoformat(),
            "snapshot_count": target.h0_snapshot_count,
        },
    }


def _attempts(result: object) -> list[dict[str, object]]:
    return [asdict(item) for item in getattr(result, "attempts", ())]


def _error_report(error: Exception) -> dict[str, str]:
    return {"error_class": type(error).__name__, "error": str(error)[:240]}


def _snapshot_payload(snapshot: object) -> dict[str, object]:
    return {
        "ticker": snapshot.ticker,
        "series": snapshot.series,
        "base_asset": snapshot.base_asset,
        "market_type": snapshot.market_type,
        "received_timestamp": snapshot.received_timestamp.isoformat(),
        "yes": [[str(level.price), str(level.size)] for level in snapshot.yes],
        "no": [[str(level.price), str(level.size)] for level in snapshot.no],
        "provider": {
            "provider_id": snapshot.provider.provider_id,
            "tier": snapshot.provider.tier,
            "endpoint_family": snapshot.provider.endpoint_family,
        },
    }


def _artifact_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _exact_market(
    markets: tuple[Mapping[str, Any], ...], target: H0OverlapTarget
) -> dict[str, object] | None:
    for raw in markets:
        if raw.get("ticker") != target.ticker:
            continue
        event_ticker = raw.get("event_ticker")
        market_type = raw.get("market_type", raw.get("type"))
        window_start = raw.get("open_time", raw.get("start_time"))
        window_end = raw.get("close_time", raw.get("end_time"))
        semantics = raw.get("yes_sub_title", raw.get("title"))
        if (
            event_ticker != target.event_id
            or market_type != "15m"
            or not isinstance(window_start, str)
            or not isinstance(window_end, str)
            or not isinstance(semantics, str)
            or not semantics.strip()
            or _provider_timestamp(window_start) != target.window_start
            or _provider_timestamp(window_end) != target.window_end
        ):
            continue
        return {
            "ticker": target.ticker,
            "provider_market_id": raw.get("id", raw.get("market_id")),
            "event_ticker": event_ticker,
            "market_type": market_type,
            "window_start": window_start,
            "window_end": window_end,
            "yes_no_semantics": semantics,
        }
    return None


def _provider_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def main() -> int:
    args = _args()
    now = datetime.now(UTC)
    report: dict[str, object] = {
        "report": "H2_TRAIN_002_ONE_EVENT_FREE_PLAN_PROBE",
        "code_sha": args.code_sha,
        "performed_at": now.isoformat(),
        "PRODUCTION_WRITES": 0,
        "recorder_changed": False,
        "holdout_accessed": False,
        "formal_training_started": False,
        "old_probe_audit": {
            "tool": "tools/run_hist002r_probe.py",
            "finding": "used discover_markets(limit=1) then the first ticker without bounds",
        },
    }
    target = select_recent_h0_overlap_target(args.recorder_db, now=now, lookback_hours=24)
    if target is None:
        target = select_recent_h0_overlap_target(args.recorder_db, now=now, lookback_hours=48)
    if target is None:
        report["classification"] = "H2_H0_OVERLAP_TARGET_NOT_FOUND"
        _write(args.output_json, report)
        return 2
    report["h0_target"] = _target_report(target)
    try:
        historical_range = validate_depthfeed_free_plan_range(
            target.window_start, target.window_end, now=now
        )
    except HistoricalProviderError as error:
        report["classification"] = str(error)
        _write(args.output_json, report)
        return 2
    report["acquisition_manifest"] = {
        "requested_range": {
            "start": historical_range.requested_start.isoformat(),
            "end": historical_range.requested_end.isoformat(),
        },
        "effective_range": historical_range.as_query_params(),
        "provider_plan_lookback_days": 7,
        "page_limit": 1,
        "max_pages": 1,
    }
    interval_seconds = (
        historical_range.effective_end - historical_range.effective_start
    ).total_seconds()
    adapter = DepthFeedHistoricalOrderbookProvider.from_project_secret(
        project_root=args.project_root, base_url=args.base_url
    )
    try:
        mapping_result = run_bounded_depthfeed_request(
            lambda: adapter.discover_markets(
                limit=10, series=target.series, historical_range=historical_range
            ),
            sleep=time.sleep,
            endpoint_family="markets",
            requested_interval_seconds=interval_seconds,
        )
        report["market_mapping_attempts"] = _attempts(mapping_result)
        if mapping_result.value is None:
            report["classification"] = mapping_result.classification
            _write(args.output_json, report)
            return 2
        mapping = _exact_market(mapping_result.value, target)
        if mapping is None:
            report["classification"] = "H2_EXACT_MARKET_MAPPING_NOT_FOUND"
            report["market_mapping_result_count"] = len(mapping_result.value)
            _write(args.output_json, report)
            return 2
        report["depthfeed_market_mapping"] = mapping
        snapshot_result = run_bounded_depthfeed_request(
            lambda: adapter.snapshots(
                target.ticker, historical_range=historical_range, max_pages=1, limit=1
            ),
            sleep=time.sleep,
            requested_interval_seconds=interval_seconds,
        )
        report["snapshot_attempts"] = _attempts(snapshot_result)
        if snapshot_result.value is None:
            report["classification"] = snapshot_result.classification
            _write(args.output_json, report)
            return 2
        snapshots = snapshot_result.value
        report["parsed_snapshot_count"] = len(snapshots)
        report["parsed_snapshot_tickers"] = sorted({item.ticker for item in snapshots})
        report["snapshot_result"] = "ACCEPTED" if snapshots else "EMPTY_RESPONSE"
        if not snapshots:
            report["classification"] = "H2_SNAPSHOT_EMPTY_FOR_VALID_EXACT_RANGE"
            _write(args.output_json, report)
            return 2
        snapshot_payloads = [_snapshot_payload(item) for item in snapshots]
        artifact_hash = _artifact_hash({"snapshots": snapshot_payloads})
        evidence_path = args.output_json.with_name("one_event_snapshots.json")
        evidence_path.write_text(
            json.dumps(
                {
                    "ticker": target.ticker,
                    "requested_range": historical_range.as_query_params(),
                    "artifact_hash": artifact_hash,
                    "snapshots": snapshot_payloads,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        event_window = L2EventWindow(
            target.event_id, target.ticker, target.window_start, target.window_end
        )
        examples = tuple(
            materialize_snapshot(
                H2SnapshotEvidence(
                    snapshot=snapshot,
                    event_window=event_window,
                    source_timestamp=snapshot.received_timestamp,
                    decision_timestamp=snapshot.received_timestamp,
                    source_artifact_hash=artifact_hash,
                    gap_state="NO_GAP",
                    evidence_origin=REAL_PROVIDER_EVIDENCE,
                    experiment_cutoff=now,
                    price_scale=1,
                )
            )
            for snapshot in snapshots
        )
        h0_references = load_h0_overlap_references(args.recorder_db, target)
        overlap = evaluate_h2_overlap_with_tolerance(
            examples, h0_references, timestamp_tolerance=timedelta(seconds=5)
        )
        sequences = build_snapshot_sequences(examples, lookback=2, excluded_event_ids=())
        report["materialization"] = {
            "CODE_PIPELINE_READY": True,
            "real_materialized_snapshot_count": len(examples),
            "snapshot_sequence_count": len(sequences.sequences),
            "sequence_exclusions": [item.reason for item in sequences.exclusions],
            "h0_reference_count": len(h0_references),
            "h0_overlap": {
                "timestamp_tolerance_seconds": 5,
                "status": overlap.status,
                "matched": list(overlap.matched),
                "conflicts": list(overlap.conflicts),
                "reasons": list(overlap.reasons),
            },
            "capabilities": summarize_h2_capabilities(examples, sequences, overlap_result=overlap),
            "artifact_path": str(evidence_path),
        }
        capabilities = report["materialization"]["capabilities"]
        assert isinstance(capabilities, dict)
        canonical = build_canonical_evidence_snapshot(
            experiment_id="h2-train-002-one-event-proof",
            experiment_cutoff=now,
            records=(
                EvidenceRecord(
                    source_id="depthfeed-h2-train-002-one-event",
                    provenance_tier=H2,
                    coverage_scope=CoverageScope.BOUNDED_WINDOW,
                    earliest_timestamp=min(item.decision_timestamp for item in examples),
                    latest_timestamp=max(item.decision_timestamp for item in examples),
                    independent_utc_days=1,
                    independent_events=1,
                    assets=(target.asset,),
                    per_day_counts={
                        examples[0].decision_timestamp.date().isoformat(): len(examples)
                    },
                    per_asset_counts={target.asset: len(examples)},
                    row_count=len(examples),
                    artifact_id=artifact_hash,
                    cutoff=now,
                    sampling_policy="exact H0 ticker and completed event window",
                    capped=True,
                    cap_size=1,
                    full_source=False,
                    data_quality_status=overlap.status,
                    gap_quarantine_state="NO_GAP",
                    sequence_availability={"days": 0},
                    microstructure_availability=canonical_microstructure_availability(capabilities),
                    target_availability={},
                    source_independent_utc_days=1,
                    source_independent_events=1,
                    source_assets=(target.asset,),
                    coverage_days=(examples[0].decision_timestamp.date().isoformat(),),
                ),
            ),
        )
        report["family_preflight"] = {
            family: {
                "status": result.status,
                "reasons": list(result.reasons),
            }
            for family in ("MLPLOB", "DeepLOB", "TLOB")
            for result in (training_preflight(canonical, model_family=family),)
        }
        tick_result = run_bounded_depthfeed_request(
            lambda: adapter.ticks(
                target.ticker, historical_range=historical_range, max_pages=1, limit=1
            ),
            sleep=time.sleep,
            endpoint_family="ticks",
            requested_interval_seconds=interval_seconds,
        )
        report["delta_attempts"] = _attempts(tick_result)
        if tick_result.value is not None:
            report["delta_result"] = "ACCEPTED"
            report["parsed_tick_count"] = len(tick_result.value)
        elif (
            isinstance(tick_result.error, DepthFeedHttpError)
            and tick_result.error.status_code == 402
        ):
            report["delta_result"] = "DEPTHFEED_DELTA_ENDPOINT_PLAN_RESTRICTED"
        else:
            report["delta_result"] = tick_result.classification
        report["classification"] = "H2_SNAPSHOT_ACQUIRED_OVERLAP_PENDING"
        _write(args.output_json, report)
        return 0
    except (HistoricalProviderError, OSError, ValueError) as error:
        report.update(_error_report(error))
        report["classification"] = (
            DEPTHFEED_RATE_LIMIT_BLOCKED
            if isinstance(error, DepthFeedHttpError) and error.status_code == 429
            else "H2_PROVIDER_REQUEST_FAILED"
        )
        _write(args.output_json, report)
        return 2
    finally:
        adapter.close()


def _write(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, sort_keys=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
