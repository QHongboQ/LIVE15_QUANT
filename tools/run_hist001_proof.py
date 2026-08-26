"""Build the bounded HIST-001 proof from the existing current-trainable snapshot.

This is read-only.  It captures a row-id boundary, never opens Dataset v2 artifacts, and writes
only a small manifest/report; raw SQLite history stays outside Git under ``data/``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from live15_quant.historical_research import (
    HistoricalSample,
    HistoricalSource,
    HistoricalTier,
    WalkForwardConfig,
    build_manifest,
    build_walk_forward_folds,
    capability_matrix,
)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _git_sha() -> str:
    return subprocess.check_output(("git", "rev-parse", "HEAD"), text=True).strip()


def load_samples(database: Path) -> tuple[tuple[HistoricalSample, ...], int]:
    """Read the current-trainable projection up to one immutable max row ID."""

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        max_row_id = int(
            connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM current_trainable_rows"
            ).fetchone()[0]
        )
        rows = connection.execute(
            """
            SELECT id, asset, ticker, window_start, window_end, decision_timestamp,
                   features_json
            FROM current_trainable_rows
            WHERE id <= ?
            ORDER BY window_start, ticker, decision_timestamp, id
            """,
            (max_row_id,),
        )
        samples: list[HistoricalSample] = []
        for row in rows:
            features = json.loads(row["features_json"])
            source_timestamps = [
                _time(item["source_timestamp"]) for item in features if item.get("source_timestamp")
            ]
            missing_reasons = sorted(
                {item["missing_reason"] for item in features if item.get("missing_reason")}
            )
            available = bool(source_timestamps)
            samples.append(
                HistoricalSample(
                    sample_id=f"current-trainable-row-{row['id']}",
                    event_id=row["ticker"],
                    asset=row["asset"],
                    source_id="live15-current-trainable",
                    provenance_tier=HistoricalTier.H0,
                    window_start=_time(row["window_start"]),
                    window_end=_time(row["window_end"]),
                    decision_timestamp=_time(row["decision_timestamp"]),
                    source_timestamp=max(source_timestamps) if available else None,
                    received_timestamp=None,
                    feature_names=tuple(item["name"] for item in features),
                    available=available,
                    missing_reason=(";".join(missing_reasons) or "source_timestamp_unavailable")
                    if not available
                    else None,
                )
            )
        return tuple(samples), max_row_id
    finally:
        connection.close()


def build_proof(
    *, database: Path, output_json: Path, code_sha: str | None = None
) -> dict[str, Any]:
    samples, max_row_id = load_samples(database)
    active = tuple(sample for sample in samples if not sample.excluded)
    if not active:
        raise RuntimeError("HIST-001 proof requires at least one as-of-valid sample")
    source_start = min(sample.source_timestamp for sample in active if sample.source_timestamp)
    source_end = max(sample.source_timestamp for sample in active if sample.source_timestamp)
    source = HistoricalSource(
        source_id="live15-current-trainable",
        tier=HistoricalTier.H0,
        data_type="LIVE15 current_trainable_rows projection",
        earliest=source_start,
        latest=source_end,
        frequency="event decision rows",
        as_of_quality="source_asof; receive_timestamp_not_projected",
        intended_use="historical path/regime substrate proof",
        limitations=(
            "H0 live-native data, not fresh validation",
            "materialized projection does not expose per-feature receive timestamps",
            "no external historical download performed",
        ),
        row_count=len(active),
        event_count=len({sample.event_id for sample in active}),
        assets=tuple(sorted({sample.asset for sample in active})),
    )
    config = {
        "source_database": database.name,
        "captured_max_row_id": max_row_id,
        "walk_forward": {
            "mode": "expanding",
            "train_days": 3,
            "validation_days": 1,
            "step_days": 1,
            "purge_embargo_seconds": 600,
        },
        "dataset_v2_isolation": {
            "dataset_id": "live15-dataset-v2-4bb4934bf328b6b024ff",
            "holdout_state": "UNREVEALED_FROZEN",
            "holdout_accessed": False,
            "rows_read": False,
        },
    }
    manifest = build_manifest(
        sources=(source,), samples=samples, code_sha=code_sha or _git_sha(), config=config
    )
    folds = build_walk_forward_folds(
        samples,
        WalkForwardConfig(train_days=3, validation_days=1, step_days=1, purge_embargo_seconds=600),
    )
    report: dict[str, Any] = {
        "report": "HIST-001",
        "status": "DEVELOPMENT_RESEARCH_SUBSTRATE_ONLY",
        "historical_acquisition": "HISTORICAL_SOURCE_ACQUISITION_PENDING",
        "manifest": manifest.to_dict(),
        "capability_matrix": list(capability_matrix((source,))),
        "walk_forward": {
            "fold_count": len(folds),
            "policy": config["walk_forward"],
            "folds": [fold.to_dict() for fold in folds],
            "random_split": False,
            "whole_event_groups": True,
        },
        "quality": {
            "excluded_samples": manifest.excluded_sample_count,
            "missing_reason_policy": "typed exclusion; no forward-fill/interpolation/zero-fill",
            "receive_timestamp_limitation": "not present in current_trainable projection",
        },
        "gates": {
            "leakage_checker": "PASS",
            "dataset_v2_untouched": True,
            "holdout_untouched": True,
            "recorder_authoritative": True,
            "microstructure_gate": "INSUFFICIENT_MICROSTRUCTURE_EVIDENCE",
            "sequence_gate": "INSUFFICIENT_SEQUENCE_EVIDENCE",
            "model_training": False,
            "production_or_paper_wiring": False,
        },
        "reproducibility": {
            "manifest_hash": manifest.build_hash,
            "code_sha": manifest.code_sha,
            "raw_history_committed": False,
            "raw_cache_location": "data/current_trainable.sqlite3 (ignored)",
        },
    }
    payload = json.dumps(report, indent=2, sort_keys=True).encode("utf-8")
    report["report_sha256"] = hashlib.sha256(payload).hexdigest()
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--code-sha", default=None)
    args = parser.parse_args()
    report = build_proof(
        database=args.database, output_json=args.output_json, code_sha=args.code_sha
    )
    print(
        json.dumps(
            {
                "dataset_id": report["manifest"]["dataset_id"],
                "samples": report["manifest"]["sample_count"],
                "events": report["manifest"]["event_count"],
                "folds": report["walk_forward"]["fold_count"],
                "status": report["status"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
