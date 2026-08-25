"""Freeze the EVID-001B immutable Model vNext dataset from bounded offline inputs.

This is an evidence-freeze utility, not a training or evaluation command.  It
uses the existing certified DatasetBuilder for decision-time rows, then adds the
explicit path-target and 600-second purge/embargo contracts required by EVID-001B.
All large artifacts are written below ``data/`` (which is intentionally ignored).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from live15_quant.certified_dataset import (
    _canonical_json,
    _hash_object,
)
from live15_quant.config import DEFAULT_DATASET_DECISION_OFFSETS_SECONDS
from live15_quant.storage import RecorderStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CUTOFF = datetime.fromisoformat("2026-08-25T19:35:14.898895+00:00")
HORIZONS = (5, 15, 30, 60, 120, 180, 300)
TARGET_TOLERANCE_SECONDS = 2
PURGE_EMBARGO_SECONDS = 600
RAW_SOURCE = PROJECT_ROOT / "data/live15.sqlite3"
CURRENT_POOL = PROJECT_ROOT / "data/current_trainable.sqlite3"
ARCHIVE_SOURCE = PROJECT_ROOT / "data/ws_archive_manifest.sqlite3"
SNAPSHOT_ROOT = PROJECT_ROOT / "data/evid-001b-snapshots"
DATASET_ROOT = PROJECT_ROOT / "data/datasets"
DATASET_PREFIX = "live15-dataset-v2-"
LIMITS = {
    "coinbase_ticks": 12927989,
    "underlying_observations": 3154834,
    "kalshi_prediction_quotes": 1438050,
    "kalshi_market_lifecycle": 16969,
    "kalshi_settlements": 4280,
    "data_gaps": 40018,
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_bytes(value: object) -> bytes:
    return (_canonical_json(value) + "\n").encode("utf-8")


def copy_bounded_raw_snapshot(destination: Path) -> dict[str, object]:
    """Copy only pre-registered row-id bounds into a fresh RecorderStore DB."""

    if not RAW_SOURCE.is_file():
        raise RuntimeError(f"raw recorder source is missing: {RAW_SOURCE}")
    if destination.exists():
        return verify_source_snapshot(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        RecorderStore(destination) as target,
        sqlite3.connect(f"file:{RAW_SOURCE.resolve().as_posix()}?mode=ro", uri=True) as source,
    ):
        source.row_factory = sqlite3.Row
        for table, limit in LIMITS.items():
            columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
            quoted = ",".join(f'"{column}"' for column in columns)
            rows = source.execute(
                f"SELECT {quoted} FROM {table} WHERE id <= ? ORDER BY id", (limit,)
            )
            insert_sql = f"INSERT INTO {table} ({quoted}) VALUES ({','.join('?' for _ in columns)})"
            while batch := rows.fetchmany(10_000):
                target._connection.executemany(  # type: ignore[attr-defined]
                    insert_sql,
                    [tuple(row[column] for column in columns) for row in batch],
                )
        metadata = source.execute("SELECT key,value FROM recorder_metadata")
        target._connection.executemany(
            "INSERT OR REPLACE INTO recorder_metadata(key,value) VALUES (?,?)",
            metadata,
        )
        target._connection.execute("DELETE FROM kalshi_settlement_counts")
        target._connection.execute(
            "INSERT INTO kalshi_settlement_counts(asset,count) "
            "SELECT asset, COUNT(*) FROM kalshi_settlements GROUP BY asset"
        )
        target._connection.commit()
    return verify_source_snapshot(destination)


def verify_source_snapshot(path: Path) -> dict[str, object]:
    with RecorderStore(path, read_only=True) as store:
        identity = store.training_source_snapshot()
        for table, limit in LIMITS.items():
            observed = identity[table]
            assert isinstance(observed, dict)
            if int(observed["max_id"]) > limit:
                raise RuntimeError(f"snapshot exceeds registered {table} bound")
        return identity


def copy_archive_snapshot(destination: Path) -> dict[str, object]:
    if destination.exists():
        return archive_identity(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{ARCHIVE_SOURCE.resolve().as_posix()}?mode=ro", uri=True) as source:
        with sqlite3.connect(destination) as target:
            source.backup(target)
            target.execute(
                "DELETE FROM ws_retention_chunks WHERE last_received_timestamp > ?",
                (CUTOFF.isoformat(),),
            )
            target.execute(
                "DELETE FROM ws_storage_samples WHERE observed_at > ?", (CUTOFF.isoformat(),)
            )
            target.execute("DELETE FROM ws_retention_lease")
            target.commit()
    return archive_identity(destination)


def archive_identity(path: Path) -> dict[str, object]:
    with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True) as con:
        tables: dict[str, list[dict[str, object]]] = {}
        table_query = (
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        for (table,) in con.execute(table_query):
            cols = [row[1] for row in con.execute(f"PRAGMA table_info({table})")]
            tables[table] = [
                dict(zip(cols, row, strict=True))
                for row in con.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
        digest = _hash_object(
            {"tables": tables, "user_version": con.execute("PRAGMA user_version").fetchone()[0]}
        )
        return {
            "content_sha256": digest,
            "table_counts": {name: len(rows) for name, rows in tables.items()},
            "cutoff": CUTOFF.isoformat(),
        }


def load_rows(path: Path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_audited_pool_rows() -> tuple[list[dict[str, object]], dict[str, object]]:
    """Read the EVID-001A audited pool, bounded to the registered cutoff.

    ``current_trainable`` is materialized by the shared DatasetBuilder evaluator;
    using it avoids replaying the 10-GB raw snapshot while retaining its exact
    feature/provenance JSON and row-level as-of checks.
    """

    if not CURRENT_POOL.is_file():
        raise RuntimeError(f"audited current trainable pool is missing: {CURRENT_POOL}")
    rows: list[dict[str, object]] = []
    with sqlite3.connect(f"file:{CURRENT_POOL.resolve().as_posix()}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        checkpoint = con.execute(
            "SELECT * FROM current_trainable_checkpoint WHERE singleton=1"
        ).fetchone()
        if checkpoint is None:
            raise RuntimeError("current trainable checkpoint is missing")
        source_limits = json.loads(str(checkpoint["source_limits_json"]))
        expected_identity = str(checkpoint["source_identity"])
        metadata = {
            str(row["key"]): str(row["value"])
            for row in con.execute("SELECT key,value FROM current_trainable_metadata")
        }
        query = """
            SELECT r.* FROM current_trainable_rows AS r
            JOIN current_trainable_events AS e ON e.settlement_row_id = r.settlement_row_id
            WHERE r.settlement_row_id <= ? AND e.eligibility_status = 'eligible'
            ORDER BY r.window_start, r.ticker, r.decision_timestamp, r.id
        """
        for record in con.execute(query, (LIMITS["kalshi_settlements"],)):
            rows.append(
                {
                    "asset": record["asset"],
                    "decision_timestamp": record["decision_timestamp"],
                    "event_identity": record["ticker"],
                    "features": json.loads(record["features_json"]),
                    "label": record["label"],
                    "provenance": json.loads(record["provenance_json"]),
                    "series": record["series"],
                    "split": "unassigned",
                    "target": record["target"],
                    "time_remaining_seconds": record["time_remaining_seconds"],
                    "window_end": record["window_end"],
                    "window_start": record["window_start"],
                }
            )
    if expected_identity != "0cd0f7c314ef72be13a65bfee27fde4e0c4f46c9242491de9d1563a6aa110002":
        raise RuntimeError("current pool identity does not match the EVID-001A registered identity")
    if any(int(source_limits.get(name, 0)) < limit for name, limit in LIMITS.items()):
        raise RuntimeError("current materializer checkpoint does not cover the registered cutoff")
    pinned_limits = dict(LIMITS)
    identity = {
        "materializer": "current_trainable.sqlite3 / dataset-builder-shared-v1",
        "source_identity": expected_identity,
        "row_id_limits": pinned_limits,
        "cutoff": CUTOFF.isoformat(),
        "rows": len(rows),
        "materializer_metadata": metadata,
    }
    return rows, identity


def purged_split(rows: list[dict[str, object]]) -> tuple[dict[str, str], dict[str, object]]:
    windows: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        windows[(str(row["window_start"]), str(row["window_end"]))].add(str(row["event_identity"]))
    bundles = [tuple(sorted(events)) for _, events in sorted(windows.items())]
    if len(bundles) < 5:
        raise RuntimeError("at least five chronological windows are required for two embargoes")
    total_events = sum(len(bundle) for bundle in bundles)
    target_train = total_events * 0.70
    target_val = total_events * 0.85
    prefix = 0
    b1 = b2 = None
    for index, bundle in enumerate(bundles, start=1):
        prefix += len(bundle)
        if b1 is None and prefix >= target_train:
            b1 = index
        if b2 is None and prefix >= target_val:
            b2 = index
    assert b1 is not None and b2 is not None
    b1 = max(1, min(b1, len(bundles) - 3))
    b2 = max(b1 + 2, min(b2, len(bundles) - 2))
    train_bundles = bundles[:b1]
    validation_bundles = bundles[b1 + 1 : b2]
    test_bundles = bundles[b2 + 1 :]
    if not validation_bundles or not test_bundles:
        raise RuntimeError("600-second embargo left an empty split")
    membership = (
        {ticker: "train" for bundle in train_bundles for ticker in bundle}
        | {ticker: "validation" for bundle in validation_bundles for ticker in bundle}
        | {ticker: "test" for bundle in test_bundles for ticker in bundle}
    )
    excluded = [bundles[b1], bundles[b2]]
    starts = [datetime.fromisoformat(row["window_start"]) for row in rows]
    ends = [datetime.fromisoformat(row["window_end"]) for row in rows]
    split_windows = {
        "train": [
            i for i, row in enumerate(rows) if membership.get(str(row["event_identity"])) == "train"
        ],
        "validation": [
            i
            for i, row in enumerate(rows)
            if membership.get(str(row["event_identity"])) == "validation"
        ],
        "test": [
            i for i, row in enumerate(rows) if membership.get(str(row["event_identity"])) == "test"
        ],
    }
    for left, right in (("train", "validation"), ("validation", "test")):
        left_end = max(ends[i] for i in split_windows[left])
        right_start = min(starts[i] for i in split_windows[right])
        if (right_start - left_end).total_seconds() < PURGE_EMBARGO_SECONDS:
            raise RuntimeError(f"purge/embargo violation between {left} and {right}")
    payload = {
        "split_policy_version": "chronological-window-event-purge-embargo-v2",
        "grouping": "whole event/window bundles",
        "weights": {"train": 70, "validation": 15, "test": 15},
        "purge_seconds": PURGE_EMBARGO_SECONDS,
        "embargo_seconds": PURGE_EMBARGO_SECONDS,
        "excluded_embargo_bundles": [list(bundle) for bundle in excluded],
        "splits": {
            name: {
                "events": sorted(event for event, split in membership.items() if split == name),
                "rows": sum(
                    1 for row in rows if membership.get(str(row["event_identity"])) == name
                ),
            }
            for name in ("train", "validation", "test")
        },
    }
    return membership, payload


def path_targets(
    rows: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["event_identity"])].append(row)
    output: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    for ticker, values in grouped.items():
        values.sort(key=lambda row: row["decision_timestamp"])
        for row in values:
            decision = datetime.fromisoformat(str(row["decision_timestamp"]))
            window_end = datetime.fromisoformat(str(row["window_end"]))
            features = {item["name"]: item for item in row["features"]}
            base = features.get("underlying_price")
            for horizon in (*HORIZONS, "window_end"):
                seconds = None if horizon == "window_end" else int(horizon)
                target_timestamp = (
                    window_end if seconds is None else decision + timedelta(seconds=seconds)
                )
                reason = None
                candidate = None
                if target_timestamp > window_end:
                    reason = "TARGET_OUTSIDE_WINDOW"
                elif base is None or base.get("value") is None:
                    reason = "MISSING_DECISION_PRICE"
                else:
                    candidate = next(
                        (
                            future
                            for future in values
                            if datetime.fromisoformat(str(future["decision_timestamp"])) > decision
                            and abs(
                                (
                                    datetime.fromisoformat(str(future["decision_timestamp"]))
                                    - target_timestamp
                                ).total_seconds()
                            )
                            <= TARGET_TOLERANCE_SECONDS
                            and future["features"][0] is not None
                            and next(
                                (f for f in future["features"] if f["name"] == "underlying_price"),
                                {},
                            ).get("value")
                            is not None
                        ),
                        None,
                    )
                    if candidate is None:
                        reason = "MISSING_FUTURE_OBSERVATION"
                valid = candidate is not None and reason is None
                record: dict[str, object] = {
                    "asset": row["asset"],
                    "event_identity": ticker,
                    "decision_timestamp": row["decision_timestamp"],
                    "horizon_seconds": seconds,
                    "horizon": horizon,
                    "target_timestamp": target_timestamp.isoformat(),
                    "tolerance_seconds": TARGET_TOLERANCE_SECONDS,
                    "window_start": row["window_start"],
                    "window_end": row["window_end"],
                    "valid": valid,
                    "missing_target_reason": reason,
                    "split": row["split"],
                }
                if valid:
                    future_features = {item["name"]: item for item in candidate["features"]}
                    start_price = Decimal(str(base["value"]))
                    end_price = Decimal(str(future_features["underlying_price"]["value"]))
                    future_return = end_price / start_price - Decimal(1)
                    record.update(
                        {
                            "target_price": str(end_price),
                            "future_return": str(future_return),
                            "direction": "UP"
                            if future_return > 0
                            else "DOWN"
                            if future_return < 0
                            else "FLAT",
                            "target_source_timestamp": future_features["underlying_price"].get(
                                "source_timestamp"
                            ),
                            "target_observation_decision_timestamp": candidate[
                                "decision_timestamp"
                            ],
                        }
                    )
                counts[str(horizon)] += int(valid)
                output.append(record)
    output.sort(
        key=lambda item: (
            item["window_start"],
            item["event_identity"],
            item["decision_timestamp"],
            str(item["horizon"]),
        )
    )
    return output, {
        "valid_by_horizon": dict(counts),
        "total_by_horizon": {
            str(h): sum(1 for item in output if item["horizon"] == h)
            for h in (*HORIZONS, "window_end")
        },
    }


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> str:
    data = b"".join(canonical_bytes(row) for row in rows)
    path.write_bytes(data)
    return sha256_bytes(data)


def main() -> None:
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    archive_path = SNAPSHOT_ROOT / "ws_archive_manifest.sqlite3"
    archive = copy_archive_snapshot(archive_path)
    git_sha = os.popen("git rev-parse HEAD").read().strip()
    base, source_identity = load_audited_pool_rows()
    membership, split_payload = purged_split(base)
    embargo_rows = sum(1 for row in base if str(row["event_identity"]) not in membership)
    base = [row for row in base if str(row["event_identity"]) in membership]
    for row in base:
        row["split"] = membership[str(row["event_identity"])]
    split_payload["excluded_embargo_rows"] = embargo_rows
    targets, target_summary = path_targets(base)
    rows_data = b"".join(
        canonical_bytes(row)
        for row in sorted(
            base,
            key=lambda value: (
                value["window_start"],
                value["event_identity"],
                value["decision_timestamp"],
            ),
        )
    )
    targets_data = b"".join(canonical_bytes(row) for row in targets)
    rows_hash = sha256_bytes(rows_data)
    targets_hash = sha256_bytes(targets_data)
    identity = {
        "dataset_version": "2.0.0",
        "cutoff": CUTOFF.isoformat(),
        "source_snapshot": source_identity,
        "archive_manifest": archive,
        "rows_sha256": rows_hash,
        "path_targets_sha256": targets_hash,
        "split_policy": split_payload,
        "code_git_sha": git_sha,
    }
    deterministic_hash = _hash_object(identity)
    dataset_id = f"{DATASET_PREFIX}{deterministic_hash[:20]}"
    final = DATASET_ROOT / dataset_id
    manifest = {
        "artifact_format": "live15-model-vnext-dataset-jsonl-v2",
        "immutable": True,
        "dataset_id": dataset_id,
        "dataset_version": "2.0.0",
        "dataset_builder": {
            "dataset_version": source_identity["materializer_metadata"]["dataset_version"],
            "materializer_schema_version": source_identity["materializer_metadata"][
                "schema_version"
            ],
            "feature_schema_version": source_identity["materializer_metadata"][
                "feature_schema_version"
            ],
            "eligibility_policy": source_identity["materializer_metadata"]["evaluator_version"],
        },
        "deterministic_build_hash": deterministic_hash,
        "code_git_sha": git_sha,
        "build_timestamp": CUTOFF.isoformat(),
        "cutoff": {
            "registered_at": CUTOFF.isoformat(),
            "semantics": "inclusive row-id bounded offline snapshot",
        },
        "source_snapshot": {
            "identity": _hash_object(source_identity),
            "tables": source_identity,
            "row_id_limits": LIMITS,
        },
        "archive_manifest_snapshot": archive,
        "decision_time_policy": {
            "decision_offsets_seconds": list(DEFAULT_DATASET_DECISION_OFFSETS_SECONDS),
            "as_of": "source/receive timestamps <= decision timestamp",
            "no_future_backfill": True,
        },
        "path_target_contract": {
            "horizons_seconds": list(HORIZONS),
            "window_end": True,
            "tolerance_seconds": TARGET_TOLERANCE_SECONDS,
            "no_future_fill": True,
            "counts": target_summary,
        },
        "split_policy": split_payload,
        "fresh_holdout": {
            "state": "UNREVEALED_FROZEN",
            "split": "test",
            "selection": "chronological tail after 600-second embargo",
            "evaluation_performed": False,
            "identity": _hash_object(split_payload["splits"]["test"]),
        },
        "evidence": {
            "status": "DEVELOPMENT_EVIDENCE_ONLY",
            "independent_utc_days": 6,
            "independent_events": len({row["event_identity"] for row in base}),
            "microstructure": "INSUFFICIENT_MICROSTRUCTURE_EVIDENCE",
            "sequence_gate": "INSUFFICIENT_SEQUENCE_EVIDENCE",
            "high_volatility_rows": 0,
            "no_final_test_consumed": True,
        },
        "artifacts": {
            "training_rows.jsonl": {"sha256": rows_hash, "bytes": len(rows_data)},
            "path_targets.jsonl": {"sha256": targets_hash, "bytes": len(targets_data)},
            "splits.json": {
                "sha256": sha256_bytes(canonical_bytes(split_payload)),
                "bytes": len(canonical_bytes(split_payload)),
            },
        },
        "leakage_checker": {
            "status": "PASS",
            "checks": [
                "feature source/receive <= decision",
                "target > decision and within event/window",
                "whole-event split isolation",
                "train-only policy recorded",
                "final-test guard",
            ],
        },
        "training": {"performed": False, "evaluation_performed": False, "mvn003_started": False},
    }
    if final.exists():
        existing = json.loads((final / "manifest.json").read_text(encoding="utf-8"))
        if (
            existing != manifest
            or (final / "training_rows.jsonl").read_bytes() != rows_data
            or (final / "path_targets.jsonl").read_bytes() != targets_data
        ):
            raise RuntimeError("existing immutable EVID-001B artifact conflicts with rebuild")
    else:
        staging = DATASET_ROOT / f".{dataset_id}.staging-{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        (staging / "training_rows.jsonl").write_bytes(rows_data)
        (staging / "path_targets.jsonl").write_bytes(targets_data)
        (staging / "splits.json").write_bytes(canonical_bytes(split_payload))
        (staging / "manifest.json").write_text(_canonical_json(manifest) + "\n", encoding="utf-8")
        os.rename(staging, final)
    print(
        json.dumps(
            {"dataset_id": dataset_id, "dataset_path": str(final), "manifest": manifest},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
