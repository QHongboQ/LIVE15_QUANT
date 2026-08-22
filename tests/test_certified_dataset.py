from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import live15_quant.certified_dataset as certified_dataset_module
from live15_quant.certified_dataset import (
    CertifiedDatasetError,
    CertifiedDatasetV1Builder,
    DatasetV1Config,
    chronological_window_split,
)
from live15_quant.models import Asset
from live15_quant.storage import RecorderStore
from live15_quant.ws_retention import WsRetentionManifest
from tests.test_dataset import BASE, add_event, sampling

GIT_SHA = "a" * 40


def _raw_snapshot(tmp_path: Path, *, events: int = 12) -> tuple[Path, Path]:
    raw = tmp_path / "raw.sqlite3"
    manifest = tmp_path / "archive-manifest.sqlite3"
    with RecorderStore(raw) as store:
        for index in range(events):
            add_event(
                store,
                BASE + timedelta(minutes=15 * index),
                result="yes" if index % 2 else "no",
            )
    WsRetentionManifest(manifest)
    return raw, manifest


def _build(tmp_path: Path) -> tuple[CertifiedDatasetV1Builder, DatasetV1Config]:
    raw, manifest = _raw_snapshot(tmp_path)
    return (
        CertifiedDatasetV1Builder(
            raw,
            tmp_path / "datasets",
            archive_manifest_snapshot=manifest,
            git_sha=GIT_SHA,
            snapshot_captured_at=datetime(2026, 8, 21, tzinfo=UTC),
        ),
        DatasetV1Config(sampling()),
    )


def test_dataset_v1_is_deterministic_immutable_and_chronological(tmp_path) -> None:
    builder, config = _build(tmp_path)
    first = builder.build(config)
    second = builder.build(config)

    assert first.dataset_id == second.dataset_id
    assert first.deterministic_build_hash == second.deterministic_build_hash
    assert not first.reused_existing_artifact
    assert second.reused_existing_artifact
    assert first.events == 12
    assert first.rows == 24
    assert sum(first.split_events.values()) == first.events
    assert sum(first.split_rows.values()) == first.rows

    manifest = json.loads((first.artifact_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["git_sha"] == GIT_SHA
    assert manifest["archive_manifest_snapshot"]["availability"] == "captured"
    assert manifest["source_snapshot"]["captured_at"] == "2026-08-21T00:00:00+00:00"
    assert len(manifest["feature_schema"]["order"]) == 42
    assert manifest["label_schema"]["version"] == "kalshi-finalized-yes-no-v1"
    eligibility = manifest["asset_validation_eligibility"]
    assert eligibility["BTC"]["out_of_sample_validation"] is True
    assert eligibility["Gold"]["validation_eligible"] is False
    assert eligibility["Gold"]["test_eligible"] is False
    assert eligibility["Gold"]["status"] == "TRAIN_ONLY_NO_OUT_OF_SAMPLE"

    rows = [
        json.loads(line)
        for line in (first.artifact_path / "training_rows.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert {row["split"] for row in rows} == {"train", "validation", "test"}
    for row in rows:
        decision = datetime.fromisoformat(row["decision_timestamp"])
        assert row["label"] in {"yes", "no"}
        assert len(row["features"]) == 42
        for feature in row["features"]:
            if feature["source_timestamp"] is not None:
                assert datetime.fromisoformat(feature["source_timestamp"]) <= decision

    split_events = manifest["split_definition"]["splits"]
    event_sets = [set(split_events[name]["events"]) for name in ("train", "validation", "test")]
    assert not event_sets[0] & event_sets[1]
    assert not event_sets[0] & event_sets[2]
    assert not event_sets[1] & event_sets[2]


def test_dataset_v1_groups_simultaneous_asset_windows_together(tmp_path) -> None:
    builder, config = _build(tmp_path)
    raw = builder.source_snapshot
    with RecorderStore(raw) as source:
        # DatasetBuilder is already tested independently. Here duplicate each event under a
        # second identity to prove simultaneous windows cannot straddle a split.
        feature_path = tmp_path / "derived.sqlite3"
        from live15_quant.dataset import DatasetBuilder, FeatureStore

        with FeatureStore(feature_path) as feature_store:
            built = DatasetBuilder(source, feature_store).build(config.dataset_build_config())
            btc_rows = feature_store.replay(built.build_id)
    rows = tuple(
        value
        for row in btc_rows
        for value in (row, replace(row, asset=Asset.ETH, ticker=f"ETH-{row.ticker}"))
    )
    splits = chronological_window_split(rows, train_weight=70, validation_weight=15, test_weight=15)
    split_for_ticker = {
        ticker: name for name, value in splits.items() for ticker in value["events"]
    }
    for row in btc_rows:
        assert split_for_ticker[row.ticker] == split_for_ticker[f"ETH-{row.ticker}"]


def test_dataset_v1_existing_manifest_conflict_fails_loudly(tmp_path) -> None:
    builder, config = _build(tmp_path)
    summary = builder.build(config)
    manifest_path = summary.artifact_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["label_schema"]["definition"] = "tampered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CertifiedDatasetError, match="manifest conflicts"):
        builder.build(config)


def test_dataset_v1_legacy_manifest_projects_asset_eligibility_without_mutation(tmp_path) -> None:
    builder, config = _build(tmp_path)
    summary = builder.build(config)
    manifest_path = summary.artifact_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    legacy = manifest.pop("asset_validation_eligibility")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    reused = builder.build(config)
    assert reused.reused_existing_artifact
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "asset_validation_eligibility" not in persisted
    assert legacy["BTC"]["out_of_sample_validation"] is True


def test_dataset_v1_publish_recovers_after_precommit_failure(tmp_path, monkeypatch) -> None:
    builder, config = _build(tmp_path)
    original = certified_dataset_module._verify_artifact

    def fail_before_publish(*_args, **_kwargs) -> None:
        raise RuntimeError("injected verification failure")

    monkeypatch.setattr(certified_dataset_module, "_verify_artifact", fail_before_publish)
    with pytest.raises(RuntimeError, match="injected verification failure"):
        builder.build(config)
    assert not tuple((tmp_path / "datasets").glob("live15-dataset-v1-*"))

    monkeypatch.setattr(certified_dataset_module, "_verify_artifact", original)
    recovered = builder.build(config)
    assert recovered.rows == 24
    assert recovered.artifact_path.is_dir()


def test_dataset_v1_recovers_dead_process_staging_and_rejects_live_owner(
    tmp_path, monkeypatch
) -> None:
    builder, config = _build(tmp_path)
    first = builder.build(config)
    monkeypatch.setattr(certified_dataset_module, "process_alive", lambda pid: pid == os.getpid())
    # A dead publisher may leave an incomplete directory, but it is never authoritative.
    stale = first.artifact_path.parent / f".{first.dataset_id}.staging-999999"
    stale.mkdir()
    (stale / "partial").write_text("never published", encoding="utf-8")
    assert builder.build(config).reused_existing_artifact
    assert not stale.exists()

    active = first.artifact_path.parent / f".{first.dataset_id}.staging-{os.getpid()}"
    active.mkdir()
    with pytest.raises(CertifiedDatasetError, match="already in progress"):
        CertifiedDatasetV1Builder(
            builder.source_snapshot,
            builder.artifact_root,
            archive_manifest_snapshot=builder.archive_manifest_snapshot,
            git_sha=GIT_SHA,
        )._publish_or_verify(
            first.dataset_id,
            first.deterministic_build_hash,
            json.loads((first.artifact_path / "manifest.json").read_text(encoding="utf-8")),
            (first.artifact_path / "training_rows.jsonl").read_bytes(),
            (first.artifact_path / "splits.json").read_bytes(),
            first.diagnostics,
        )
    active.rmdir()


def test_dataset_v1_requires_archive_manifest_snapshot(tmp_path) -> None:
    raw, _manifest = _raw_snapshot(tmp_path)
    builder = CertifiedDatasetV1Builder(raw, tmp_path / "datasets", git_sha=GIT_SHA)
    with pytest.raises(CertifiedDatasetError, match="archive manifest snapshot"):
        builder.build(DatasetV1Config(sampling()))
