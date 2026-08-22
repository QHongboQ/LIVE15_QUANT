"""Immutable, lineage-bound Dataset v1 artifacts built from offline raw snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from live15_quant.dataset import (
    ASOF_QUERY_VERSION,
    DATASET_VERSION,
    DatasetBuildConfig,
    DatasetBuilder,
    FeatureStore,
    TrainingRow,
    dataset_diagnostics,
)
from live15_quant.feature_registry import FEATURE_REGISTRY, FEATURE_SCHEMA_VERSION
from live15_quant.features import SamplingPolicy
from live15_quant.models import Asset
from live15_quant.recorder_control import process_alive
from live15_quant.storage import RecorderStore

CERTIFIED_DATASET_VERSION = "1.0.0"
LABEL_SCHEMA_VERSION = "kalshi-finalized-yes-no-v1"
SPLIT_POLICY_VERSION = "chronological-window-event-v1"
QUARANTINE_POLICY_VERSION = "gap-and-missing-fail-closed-v1"
MARKET_SESSION_SEMANTICS_VERSION = "market-session-aware-v1"


class CertifiedDatasetError(RuntimeError):
    """A Dataset v1 input, artifact, or lineage fact is invalid."""


@dataclass(frozen=True, slots=True)
class DatasetV1Config:
    """The complete, immutable policy for one certified Dataset v1 build."""

    sampling_policy: SamplingPolicy
    assets: tuple[Asset, ...] = tuple(Asset)
    train_weight: int = 70
    validation_weight: int = 15
    test_weight: int = 15

    def __post_init__(self) -> None:
        if not self.assets or len(set(self.assets)) != len(self.assets):
            raise ValueError("Dataset v1 assets must be non-empty and unique")
        if min(self.train_weight, self.validation_weight, self.test_weight) <= 0:
            raise ValueError("Dataset v1 split weights must be positive")

    def dataset_build_config(self) -> DatasetBuildConfig:
        return DatasetBuildConfig(self.sampling_policy, assets=self.assets)

    def payload(self) -> dict[str, object]:
        return {
            "dataset_builder": self.dataset_build_config().payload(),
            "split_policy": {
                "version": SPLIT_POLICY_VERSION,
                "grouping": "all rows for one event identity; simultaneous windows stay together",
                "ordering": "window_start,window_end,event_identity",
                "weights": {
                    "train": self.train_weight,
                    "validation": self.validation_weight,
                    "test": self.test_weight,
                },
            },
        }


@dataclass(frozen=True, slots=True)
class DatasetV1Summary:
    dataset_id: str
    deterministic_build_hash: str
    artifact_path: Path
    events: int
    rows: int
    split_events: dict[str, int]
    split_rows: dict[str, int]
    diagnostics: dict[str, object]
    reused_existing_artifact: bool


@dataclass(frozen=True, slots=True)
class ModelDatasetLineage:
    """Required immutable boundary that future model artifacts must record."""

    dataset_id: str
    deterministic_build_hash: str
    feature_schema_version: str
    code_git_sha: str
    label_schema_version: str = LABEL_SCHEMA_VERSION

    def as_dict(self) -> dict[str, str]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_deterministic_build_hash": self.deterministic_build_hash,
            "feature_schema_version": self.feature_schema_version,
            "label_schema_version": self.label_schema_version,
            "code_git_sha": self.code_git_sha,
        }


class CertifiedDatasetV1Builder:
    """Publish one immutable Dataset v1 artifact from an offline raw snapshot only."""

    def __init__(
        self,
        source_snapshot: Path,
        artifact_root: Path,
        *,
        archive_manifest_snapshot: Path | None = None,
        git_sha: str | None = None,
        snapshot_captured_at: datetime | None = None,
    ) -> None:
        self.source_snapshot = source_snapshot.resolve()
        self.artifact_root = artifact_root.resolve()
        self.archive_manifest_snapshot = (
            archive_manifest_snapshot.resolve() if archive_manifest_snapshot is not None else None
        )
        self.git_sha = git_sha or _git_sha()
        if snapshot_captured_at is not None and (
            snapshot_captured_at.tzinfo is None or snapshot_captured_at.utcoffset() is None
        ):
            raise ValueError("Dataset v1 snapshot capture timestamp must be timezone-aware")
        self.snapshot_captured_at = snapshot_captured_at

    def build(self, config: DatasetV1Config) -> DatasetV1Summary:
        if not self.source_snapshot.is_file():
            raise CertifiedDatasetError("Dataset v1 requires an existing offline raw snapshot")
        if len(self.git_sha) != 40 or any(
            character not in "0123456789abcdef" for character in self.git_sha
        ):
            raise CertifiedDatasetError("Dataset v1 git SHA must be a lowercase full commit SHA")

        if self.archive_manifest_snapshot is None:
            raise CertifiedDatasetError(
                "Dataset v1 requires an offline archive manifest snapshot for lineage"
            )
        archive_identity = archive_manifest_identity(self.archive_manifest_snapshot)
        with tempfile.TemporaryDirectory(prefix="live15-dataset-v1-build-") as directory:
            work = Path(directory)
            feature_path = work / "feature-store.sqlite3"
            with (
                RecorderStore(self.source_snapshot) as source,
                FeatureStore(feature_path) as destination,
            ):
                built = DatasetBuilder(source, destination).build(config.dataset_build_config())
                if not built.complete:
                    raise CertifiedDatasetError("Dataset v1 build stopped before completion")
                rows = destination.replay(built.build_id)
                source_identity = destination.build_source_snapshot(built.build_id)
                base_diagnostics = destination.build_diagnostics(built.build_id)
            if base_diagnostics is None:
                raise CertifiedDatasetError("Dataset v1 source build lacks complete diagnostics")

            _validate_training_rows(rows)
            splits = chronological_window_split(
                rows,
                train_weight=config.train_weight,
                validation_weight=config.validation_weight,
                test_weight=config.test_weight,
            )
            split_by_ticker = {
                ticker: name for name, partition in splits.items() for ticker in partition["events"]
            }
            row_bytes = _rows_jsonl(rows, split_by_ticker)
            split_payload = _split_payload(splits)
            split_bytes = _canonical_json(split_payload).encode("utf-8") + b"\n"
            rows_hash = _sha256(row_bytes)
            splits_hash = _sha256(split_bytes)
            diagnostics = _certified_diagnostics(rows, splits, base_diagnostics)
            identity = {
                "certified_dataset_version": CERTIFIED_DATASET_VERSION,
                "git_sha": self.git_sha,
                "dataset_build_id": built.build_id,
                "dataset_builder_version": DATASET_VERSION,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "label_schema_version": LABEL_SCHEMA_VERSION,
                "source_snapshot_identity": _hash_object(source_identity),
                "archive_manifest_identity": archive_identity["content_sha256"],
                "policy": config.payload(),
                "rows_sha256": rows_hash,
                "splits_sha256": splits_hash,
            }
            deterministic_build_hash = _hash_object(identity)
            dataset_id = f"live15-dataset-v1-{deterministic_build_hash[:20]}"
            manifest = _manifest(
                dataset_id=dataset_id,
                deterministic_build_hash=deterministic_build_hash,
                git_sha=self.git_sha,
                source_identity=source_identity,
                snapshot_captured_at=self.snapshot_captured_at,
                archive_identity=archive_identity,
                config=config,
                dataset_build_id=built.build_id,
                rows_hash=rows_hash,
                rows_bytes=len(row_bytes),
                splits_hash=splits_hash,
                splits_bytes=len(split_bytes),
                splits=splits,
                diagnostics=diagnostics,
            )
            return self._publish_or_verify(
                dataset_id,
                deterministic_build_hash,
                manifest,
                row_bytes,
                split_bytes,
                diagnostics,
            )

    def _publish_or_verify(
        self,
        dataset_id: str,
        deterministic_build_hash: str,
        manifest: dict[str, object],
        row_bytes: bytes,
        split_bytes: bytes,
        diagnostics: dict[str, object],
    ) -> DatasetV1Summary:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        _recover_stale_staging(self.artifact_root, dataset_id)
        final = self.artifact_root / dataset_id
        if final.exists():
            return _verify_existing(
                final, dataset_id, deterministic_build_hash, manifest, diagnostics
            )

        staging = self.artifact_root / f".{dataset_id}.staging-{os.getpid()}"
        if staging.exists():
            raise CertifiedDatasetError("Dataset v1 staging directory already exists")
        staging.mkdir()
        try:
            (staging / "training_rows.jsonl").write_bytes(row_bytes)
            (staging / "splits.json").write_bytes(split_bytes)
            (staging / "manifest.json").write_text(
                _canonical_json(manifest) + "\n", encoding="utf-8"
            )
            _verify_artifact(staging, manifest)
            try:
                os.rename(staging, final)
            except FileExistsError:
                return _verify_existing(
                    final, dataset_id, deterministic_build_hash, manifest, diagnostics
                )
        finally:
            if staging.exists():
                _remove_tree(staging)
        return _summary_from_manifest(final, manifest, diagnostics, reused_existing_artifact=False)


def chronological_window_split(
    rows: tuple[TrainingRow, ...],
    *,
    train_weight: int,
    validation_weight: int,
    test_weight: int,
) -> dict[str, dict[str, tuple[TrainingRow, ...] | tuple[str, ...]]]:
    """Chronologically partition whole event windows; rows never cross a split."""

    if not rows:
        raise CertifiedDatasetError("Dataset v1 requires at least one trainable row")
    if min(train_weight, validation_weight, test_weight) <= 0:
        raise ValueError("split weights must be positive")
    event_rows: dict[str, list[TrainingRow]] = defaultdict(list)
    for row in rows:
        event_rows[row.ticker].append(row)
    windows: dict[tuple[datetime, datetime], list[str]] = defaultdict(list)
    for ticker, values in event_rows.items():
        first = min(values, key=lambda row: row.decision_timestamp)
        windows[(first.window_start, first.window_end)].append(ticker)
    ordered_windows = tuple(
        tuple(sorted(tickers))
        for _window, tickers in sorted(windows.items(), key=lambda item: (*item[0], item[1]))
    )
    if len(ordered_windows) < 3:
        raise CertifiedDatasetError("Dataset v1 requires at least three chronological windows")
    total_events = len(event_rows)
    total_weight = train_weight + validation_weight + test_weight
    train_end = _nearest_window_boundary(
        ordered_windows,
        total_events * train_weight / total_weight,
        minimum=1,
        maximum=len(ordered_windows) - 2,
    )
    validation_end = _nearest_window_boundary(
        ordered_windows,
        total_events * (train_weight + validation_weight) / total_weight,
        minimum=train_end + 1,
        maximum=len(ordered_windows) - 1,
    )
    event_partitions = {
        "train": tuple(ticker for bundle in ordered_windows[:train_end] for ticker in bundle),
        "validation": tuple(
            ticker for bundle in ordered_windows[train_end:validation_end] for ticker in bundle
        ),
        "test": tuple(ticker for bundle in ordered_windows[validation_end:] for ticker in bundle),
    }
    if any(not events for events in event_partitions.values()):
        raise CertifiedDatasetError("Dataset v1 chronological split has an empty partition")
    result: dict[str, dict[str, tuple[TrainingRow, ...] | tuple[str, ...]]] = {}
    for name, events in event_partitions.items():
        event_set = set(events)
        split_rows = tuple(
            row
            for row in sorted(
                rows,
                key=lambda row: (
                    row.window_start,
                    row.window_end,
                    row.ticker,
                    row.decision_timestamp,
                ),
            )
            if row.ticker in event_set
        )
        result[name] = {"events": events, "rows": split_rows}
    _validate_split_isolation(result)
    return result


def archive_manifest_identity(path: Path | None) -> dict[str, object]:
    """Hash archive-manifest facts from an offline copy without recording local paths."""

    if path is None:
        return {"availability": "not_configured", "content_sha256": _hash_object({})}
    if not path.is_file():
        raise CertifiedDatasetError("Dataset v1 archive manifest snapshot is missing")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            if not str(row[0]).startswith("sqlite_")
        )
        records: dict[str, list[dict[str, object]]] = {}
        for table in tables:
            columns = tuple(
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            )
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            records[table] = [
                {column: _json_scalar(value) for column, value in zip(columns, row, strict=True)}
                for row in rows
            ]
        payload = {
            "tables": records,
            "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        }
        return {
            "availability": "captured",
            "content_sha256": _hash_object(payload),
            "table_counts": {table: len(rows) for table, rows in records.items()},
        }
    finally:
        connection.close()


def _nearest_window_boundary(
    bundles: tuple[tuple[str, ...], ...], target_events: float, *, minimum: int, maximum: int
) -> int:
    prefix = 0
    candidates: list[tuple[float, int]] = []
    for index, bundle in enumerate(bundles, start=1):
        prefix += len(bundle)
        if minimum <= index <= maximum:
            candidates.append((abs(prefix - target_events), index))
    if not candidates:
        raise CertifiedDatasetError("Dataset v1 lacks a valid chronological split boundary")
    return min(candidates)[1]


def _validate_split_isolation(
    splits: dict[str, dict[str, tuple[TrainingRow, ...] | tuple[str, ...]]],
) -> None:
    event_sets = {name: set(value["events"]) for name, value in splits.items()}
    if (
        event_sets["train"] & event_sets["validation"]
        or event_sets["train"] & event_sets["test"]
        or event_sets["validation"] & event_sets["test"]
    ):
        raise CertifiedDatasetError("Dataset v1 event identity crosses split boundaries")
    ordered = tuple(splits[name]["rows"] for name in ("train", "validation", "test"))
    for earlier, later in pairwise(ordered):
        assert isinstance(earlier, tuple) and isinstance(later, tuple)
        if max(row.window_end for row in earlier) > min(row.window_start for row in later):
            raise CertifiedDatasetError("Dataset v1 splits are not strictly chronological")


def _validate_training_rows(rows: tuple[TrainingRow, ...]) -> None:
    expected = tuple(definition.name for definition in FEATURE_REGISTRY)
    for row in rows:
        if tuple(item.name for item in row.features.observations) != expected:
            raise CertifiedDatasetError(
                "Dataset v1 feature order does not match the certified registry"
            )
        if row.label.value not in {"yes", "no"}:
            raise CertifiedDatasetError("Dataset v1 label is not official finalized YES/NO truth")
        for observation in row.features.observations:
            if (
                observation.source_timestamp is not None
                and observation.source_timestamp > row.decision_timestamp
            ):
                raise CertifiedDatasetError(
                    "Dataset v1 feature source timestamp leaks beyond decision time"
                )
            if (observation.value is None) != (observation.missing_reason is not None):
                raise CertifiedDatasetError("Dataset v1 feature missing semantics are inconsistent")


def _rows_jsonl(rows: tuple[TrainingRow, ...], split_by_ticker: dict[str, str]) -> bytes:
    values: list[str] = []
    for row in sorted(
        rows,
        key=lambda item: (item.window_start, item.window_end, item.ticker, item.decision_timestamp),
    ):
        values.append(
            _canonical_json(
                {
                    "asset": row.asset.value,
                    "decision_timestamp": row.decision_timestamp.isoformat(),
                    "event_identity": row.ticker,
                    "features": [
                        {
                            "missing_reason": (
                                observation.missing_reason.value
                                if observation.missing_reason is not None
                                else None
                            ),
                            "name": observation.name,
                            "source_timestamp": (
                                observation.source_timestamp.isoformat()
                                if observation.source_timestamp is not None
                                else None
                            ),
                            "value": (
                                str(observation.value) if observation.value is not None else None
                            ),
                        }
                        for observation in row.features.observations
                    ],
                    "label": row.label.value,
                    "provenance": {
                        "coinbase_tick_row_ids": list(row.source_tick_row_ids),
                        "market_row_id": row.source_market_row_id,
                        "quote_row_ids": list(row.source_quote_row_ids),
                        "underlying_observation_row_ids": list(row.source_underlying_row_ids),
                    },
                    "series": row.series,
                    "split": split_by_ticker[row.ticker],
                    "target": str(row.target),
                    "time_remaining_seconds": str(row.time_remaining_seconds),
                    "window_end": row.window_end.isoformat(),
                    "window_start": row.window_start.isoformat(),
                }
            )
        )
    return ("\n".join(values) + "\n").encode("utf-8")


def _split_payload(
    splits: dict[str, dict[str, tuple[TrainingRow, ...] | tuple[str, ...]]],
) -> dict[str, object]:
    return {
        "split_policy_version": SPLIT_POLICY_VERSION,
        "splits": {
            name: {
                "event_digest": _hash_object(list(value["events"])),
                "events": list(value["events"]),
            }
            for name, value in splits.items()
        },
    }


def _certified_diagnostics(
    rows: tuple[TrainingRow, ...],
    splits: dict[str, dict[str, tuple[TrainingRow, ...] | tuple[str, ...]]],
    build_diagnostics: dict[str, object],
) -> dict[str, object]:
    result = {
        "overall": dataset_diagnostics(rows),
        "splits": {},
        "quarantine": {
            "evaluated_finalized_events": build_diagnostics.get("evaluated_finalized_events"),
            "events_without_training_rows": build_diagnostics.get("events_without_training_rows"),
            "skipped_decisions": build_diagnostics.get("skipped_decisions"),
            "trainability_rejections": build_diagnostics.get("trainability_rejections", {}),
        },
    }
    split_diagnostics: dict[str, object] = {}
    for name, partition in splits.items():
        split_rows = partition["rows"]
        assert isinstance(split_rows, tuple)
        item = dataset_diagnostics(split_rows)
        item["events_count"] = len(partition["events"])
        item["temporal_range"] = {
            "start": min(row.window_start for row in split_rows).isoformat(),
            "end": max(row.window_end for row in split_rows).isoformat(),
        }
        event_by_asset: dict[str, set[str]] = defaultdict(set)
        for row in split_rows:
            event_by_asset[row.asset.value].add(row.ticker)
        item["events_per_asset"] = {
            asset.value: len(event_by_asset[asset.value]) for asset in Asset
        }
        split_diagnostics[name] = item
    result["splits"] = split_diagnostics
    return result


def _asset_validation_eligibility(diagnostics: dict[str, object]) -> dict[str, object]:
    """Summarize whether each configured asset has genuine out-of-sample coverage.

    This is derived only from the immutable split diagnostics.  It must not rebalance
    rows or infer eligibility from label balance: an asset with train-only history is
    explicitly reported as such.
    """

    split_diagnostics = diagnostics.get("splits")
    if not isinstance(split_diagnostics, dict):
        raise CertifiedDatasetError("Dataset v1 split diagnostics are malformed")
    result: dict[str, object] = {}
    for asset in Asset:
        counts: dict[str, int] = {}
        rows: dict[str, int] = {}
        for split in ("train", "validation", "test"):
            item = split_diagnostics.get(split)
            if not isinstance(item, dict):
                raise CertifiedDatasetError("Dataset v1 split diagnostics are incomplete")
            events_per_asset = item.get("events_per_asset")
            rows_per_asset = item.get("rows_per_asset")
            if not isinstance(events_per_asset, dict) or not isinstance(rows_per_asset, dict):
                raise CertifiedDatasetError("Dataset v1 asset diagnostics are malformed")
            event_count = events_per_asset.get(asset.value)
            row_count = rows_per_asset.get(asset.value)
            if not isinstance(event_count, int) or not isinstance(row_count, int):
                raise CertifiedDatasetError("Dataset v1 asset diagnostics are incomplete")
            counts[split] = event_count
            rows[split] = row_count
        has_validation = counts["validation"] > 0
        has_test = counts["test"] > 0
        result[asset.value] = {
            "train_events": counts["train"],
            "validation_events": counts["validation"],
            "test_events": counts["test"],
            "train_rows": rows["train"],
            "validation_rows": rows["validation"],
            "test_rows": rows["test"],
            "validation_eligible": has_validation,
            "test_eligible": has_test,
            "out_of_sample_validation": has_validation and has_test,
            "status": (
                "OUT_OF_SAMPLE_VALIDATED"
                if has_validation and has_test
                else "TRAIN_ONLY_NO_OUT_OF_SAMPLE"
            ),
        }
    return result


def _manifest(
    *,
    dataset_id: str,
    deterministic_build_hash: str,
    git_sha: str,
    source_identity: dict[str, object],
    snapshot_captured_at: datetime | None,
    archive_identity: dict[str, object],
    config: DatasetV1Config,
    dataset_build_id: str,
    rows_hash: str,
    rows_bytes: int,
    splits_hash: str,
    splits_bytes: int,
    splits: dict[str, dict[str, tuple[TrainingRow, ...] | tuple[str, ...]]],
    diagnostics: dict[str, object],
) -> dict[str, object]:
    return {
        "artifact_format": "live15-certified-dataset-jsonl-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "dataset_builder_build_id": dataset_build_id,
        "dataset_id": dataset_id,
        "dataset_version": CERTIFIED_DATASET_VERSION,
        "deterministic_build_hash": deterministic_build_hash,
        "git_sha": git_sha,
        "source_snapshot": {
            "identity": _hash_object(source_identity),
            "tables": source_identity,
            "captured_at": (
                snapshot_captured_at.astimezone(UTC).isoformat()
                if snapshot_captured_at is not None
                else None
            ),
            "as_of_semantics": "fixed offline SQLite snapshot with bounded content hashes",
        },
        "archive_manifest_snapshot": archive_identity,
        "feature_schema": {
            "version": FEATURE_SCHEMA_VERSION,
            "order": [definition.name for definition in FEATURE_REGISTRY],
            "definitions": [
                {
                    **asdict(definition),
                    "family": definition.family.value,
                    "missing_policy": definition.missing_policy.value,
                    "timestamp_semantics": definition.timestamp_semantics.value,
                }
                for definition in FEATURE_REGISTRY
            ],
        },
        "label_schema": {
            "version": LABEL_SCHEMA_VERSION,
            "definition": "Kalshi official finalized exact ticker/window YES or NO result only",
        },
        "decision_time_policy": config.dataset_build_config().payload(),
        "quarantine_gap_policy": {
            "version": QUARANTINE_POLICY_VERSION,
            "asof_query_version": ASOF_QUERY_VERSION,
            "rules": [
                "source and receive timestamps must not exceed decision_timestamp",
                "gap, stale, unavailable, market_closed, and "
                "insufficient-lookback rows are rejected",
                "no forward fill, backward fill, synthetic fill, or future backfill",
            ],
        },
        "market_session_semantics": {
            "version": MARKET_SESSION_SEMANTICS_VERSION,
            "rule": "closed Gold/Silver/WTI sessions are DATA_UNAVAILABLE, "
            "not fresh inputs or source-failure gaps",
        },
        "source_provider_semantics": {
            "Kalshi": "official finalized settlements are label truth; "
            "quotes remain contract-market inputs",
            "Coinbase": "predictive underlying input for BTC/ETH/XRP/SOL/DOGE only",
            "Pyth": "predictive underlying input for Gold/Silver/WTI/HYPE/BNB only",
            "secondary_sources": "not consumed by the certified 42-feature registry",
        },
        "split_definition": _split_payload(splits),
        "asset_validation_eligibility": _asset_validation_eligibility(diagnostics),
        "artifacts": {
            "training_rows.jsonl": {"sha256": rows_hash, "bytes": rows_bytes},
            "splits.json": {"sha256": splits_hash, "bytes": splits_bytes},
        },
        "diagnostics": diagnostics,
        "model_lineage_contract": {
            "required_fields": [
                "model_version",
                "dataset_id",
                "dataset_deterministic_build_hash",
                "feature_schema_version",
                "label_schema_version",
                "code_git_sha",
            ]
        },
    }


def _verify_existing(
    path: Path,
    dataset_id: str,
    deterministic_build_hash: str,
    expected_manifest: dict[str, object],
    diagnostics: dict[str, object],
) -> DatasetV1Summary:
    manifest = _read_manifest(path)
    if (
        manifest.get("dataset_id") != dataset_id
        or manifest.get("deterministic_build_hash") != deterministic_build_hash
    ):
        raise CertifiedDatasetError("existing Dataset v1 identity conflicts with this build")
    if _manifest_identity_view(manifest) != _manifest_identity_view(expected_manifest):
        raise CertifiedDatasetError("existing Dataset v1 manifest conflicts with this build")
    _verify_artifact(path, manifest)
    return _summary_from_manifest(path, manifest, diagnostics, reused_existing_artifact=True)


def _verify_artifact(path: Path, manifest: dict[str, object]) -> None:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise CertifiedDatasetError("Dataset v1 manifest artifacts are malformed")
    for name in ("training_rows.jsonl", "splits.json"):
        expected = artifacts.get(name)
        if not isinstance(expected, dict) or not isinstance(expected.get("sha256"), str):
            raise CertifiedDatasetError("Dataset v1 manifest artifact hash is malformed")
        artifact = path / name
        if not artifact.is_file() or _sha256(artifact.read_bytes()) != expected["sha256"]:
            raise CertifiedDatasetError("Dataset v1 immutable artifact hash mismatch")


def _summary_from_manifest(
    path: Path,
    manifest: dict[str, object],
    diagnostics: dict[str, object],
    *,
    reused_existing_artifact: bool,
) -> DatasetV1Summary:
    split_definition = manifest.get("split_definition")
    if not isinstance(split_definition, dict) or not isinstance(
        split_definition.get("splits"), dict
    ):
        raise CertifiedDatasetError("Dataset v1 split manifest is malformed")
    splits = split_definition["splits"]
    certified = manifest.get("diagnostics")
    if not isinstance(certified, dict) or not isinstance(certified.get("splits"), dict):
        raise CertifiedDatasetError("Dataset v1 diagnostics are malformed")
    split_rows = {
        name: int(value["rows_count"])
        for name, value in certified["splits"].items()
        if isinstance(value, dict) and isinstance(value.get("rows_count"), int)
    }
    split_events = {
        name: len(value["events"])
        for name, value in splits.items()
        if isinstance(value, dict) and isinstance(value.get("events"), list)
    }
    overall = certified.get("overall")
    if not isinstance(overall, dict):
        raise CertifiedDatasetError("Dataset v1 overall diagnostics are malformed")
    return DatasetV1Summary(
        dataset_id=str(manifest["dataset_id"]),
        deterministic_build_hash=str(manifest["deterministic_build_hash"]),
        artifact_path=path,
        events=int(overall["events_count"]),
        rows=int(overall["rows_count"]),
        split_events=split_events,
        split_rows=split_rows,
        diagnostics=diagnostics if not reused_existing_artifact else certified,
        reused_existing_artifact=reused_existing_artifact,
    )


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CertifiedDatasetError("Dataset v1 manifest is missing or malformed") from error
    if not isinstance(value, dict):
        raise CertifiedDatasetError("Dataset v1 manifest is malformed")
    return value


def _manifest_identity_view(manifest: dict[str, object]) -> dict[str, object]:
    """Exclude informative capture times from an otherwise immutable manifest comparison."""

    view = json.loads(_canonical_json(manifest))
    assert isinstance(view, dict)
    # Dataset v1 artifacts published before asset-validation eligibility was added remain
    # immutable.  Project the field from their existing split diagnostics for comparison,
    # without mutating the artifact or treating any other manifest difference as compatible.
    if "asset_validation_eligibility" not in view:
        diagnostics = view.get("diagnostics")
        if isinstance(diagnostics, dict):
            view["asset_validation_eligibility"] = _asset_validation_eligibility(diagnostics)
    view.pop("created_at", None)
    source_snapshot = view.get("source_snapshot")
    if isinstance(source_snapshot, dict):
        source_snapshot.pop("captured_at", None)
    return view


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CertifiedDatasetError("Dataset v1 could not resolve the current git SHA") from error
    value = result.stdout.strip()
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash_object(value: object) -> str:
    return _sha256(_canonical_json(value).encode("utf-8"))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_scalar(value: object) -> object:
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _remove_tree(path: Path) -> None:
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file():
            child.unlink()
        else:
            child.rmdir()
    path.rmdir()


def _recover_stale_staging(artifact_root: Path, dataset_id: str) -> None:
    """Remove only staging directories whose recorded owner process is no longer alive."""

    prefix = f".{dataset_id}.staging-"
    for candidate in artifact_root.glob(f"{prefix}*"):
        if not candidate.is_dir():
            raise CertifiedDatasetError("Dataset v1 staging artifact has an invalid type")
        try:
            owner_pid = int(candidate.name.removeprefix(prefix))
        except ValueError as error:
            raise CertifiedDatasetError(
                "Dataset v1 staging artifact has an invalid owner"
            ) from error
        if owner_pid <= 0:
            raise CertifiedDatasetError("Dataset v1 staging artifact has an invalid owner")
        if process_alive(owner_pid):
            raise CertifiedDatasetError("Dataset v1 build is already in progress")
        _remove_tree(candidate)
