from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from live15_quant.archive_research import ArchiveResearchQuery, ArchiveResearchUnavailable
from live15_quant.config import Settings
from live15_quant.kalshi_ws import (
    KalshiBookSide,
    KalshiBookSyncStatus,
    KalshiCommandAcknowledged,
    KalshiOrderBookDelta,
    KalshiOrderBookSnapshot,
)
from live15_quant.models import OrderBookLevel
from live15_quant.research_data_authority import ResearchDataAuthority
from live15_quant.research_runner import (
    ExperimentConflictError,
    HashWorkAdapter,
    ResearchRunConfig,
    ResearchRunInput,
    ResearchRunner,
    ResearchRunnerPathError,
    ResumeMismatchError,
    load_research_input_snapshot,
    write_research_input_snapshot,
)
from live15_quant.storage import RecorderStore
from live15_quant.ws_retention import ArchiveState, WsArchiveService, WsRetentionManifest

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
TICKER = "KXBTC15M-26AUG241215-15"


def _archive(tmp_path: Path) -> tuple[Settings, Path, tuple[object, ...]]:
    database = tmp_path / "raw.sqlite3"
    root = tmp_path / "archive"
    manifest_path = tmp_path / "manifest.sqlite3"
    received = NOW - timedelta(hours=8)
    store = RecorderStore(database)
    store.append_kalshi_ws_orderbook_event(
        KalshiCommandAcknowledged(
            connection_id="integration-connection",
            request_id=1,
            subscription_id=2,
            sequence=1,
            market_tickers=(TICKER,),
            socket_received_timestamp=received,
            parse_timestamp=received + timedelta(microseconds=1),
        ),
        sync_status_after=KalshiBookSyncStatus.UNSYNCHRONIZED,
    )
    store.append_kalshi_ws_orderbook_event(
        KalshiOrderBookSnapshot(
            connection_id="integration-connection",
            subscription_id=2,
            sequence=2,
            ticker=TICKER,
            market_id="integration-market",
            yes_bids=(OrderBookLevel(Decimal("0.50"), Decimal("10")),),
            no_bids=(OrderBookLevel(Decimal("0.49"), Decimal("11")),),
            source_timestamp=received,
            socket_received_timestamp=received + timedelta(microseconds=2),
            parse_timestamp=received + timedelta(microseconds=3),
        ),
        sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED,
    )
    for sequence in range(3, 17):
        observed = received + timedelta(seconds=sequence)
        store.append_kalshi_ws_orderbook_event(
            KalshiOrderBookDelta(
                connection_id="integration-connection",
                subscription_id=2,
                sequence=sequence,
                ticker=TICKER,
                market_id="integration-market",
                side=KalshiBookSide.YES,
                price=Decimal("0.50"),
                quantity_delta=Decimal("1"),
                source_timestamp=observed,
                socket_received_timestamp=observed,
                parse_timestamp=observed + timedelta(microseconds=1),
            ),
            sync_status_after=KalshiBookSyncStatus.SYNCHRONIZED,
        )
    store.close()
    manifest = WsRetentionManifest(manifest_path)
    service = WsArchiveService(
        database,
        root,
        manifest,
        hot_retention=timedelta(hours=6),
        chunk_records=8,
    )
    chunks = []
    while result := service.run_once(now=NOW).chunk:
        assert result.state is ArchiveState.PURGE_ELIGIBLE
        chunks.append(result)
    settings = Settings(
        recorder_data_path=database,
        current_trainable_path=tmp_path / "current.sqlite3",
        ws_archive_root=root,
        ws_archive_manifest_path=manifest_path,
        feature_store_path=tmp_path / "features.sqlite3",
        paper_data_path=tmp_path / "paper.sqlite3",
        recorder_health_path=tmp_path / "health.json",
        recorder_control_path=tmp_path / "control.json",
        recorder_pid_path=tmp_path / "recorder.pid",
        readiness_report_path=tmp_path / "readiness.json",
    )
    return settings, manifest_path, tuple(chunks)


def _prepared(authority: ResearchDataAuthority, chunk: object):
    from live15_quant.archive_mvn003 import prepare_archive_research_run

    return prepare_archive_research_run(
        authority,
        ArchiveResearchQuery(chunk.first_event_id, chunk.last_event_id, NOW, maximum_chunks=1),
        code_git_sha="a" * 40,
        experiment_id="archive-mvn003-integration",
        model_family="path_expert",
    )


def test_verified_archive_builds_immutable_mvn003_input_and_resumes_deterministically(
    tmp_path: Path,
) -> None:
    settings, _manifest_path, (first, second) = _archive(tmp_path)
    authority = ResearchDataAuthority(settings, project_root=tmp_path)
    prepared = _prepared(authority, first)
    repeat = _prepared(authority, first)

    assert prepared.selection.source_identity == repeat.selection.source_identity
    assert prepared.research_universe.content_hash == repeat.research_universe.content_hash
    assert prepared.canonical_evidence.snapshot_id == repeat.canonical_evidence.snapshot_id
    assert prepared.run_input.research_universe is prepared.research_universe
    assert prepared.run_input.canonical_evidence is prepared.canonical_evidence
    assert prepared.run_input.holdout_accessed is False
    assert prepared.research_universe.holdout_accessed is False
    assert (
        prepared.research_universe.source_manifests[0].coverage_status["replay"]
        == "REPLAY_VERIFIED"
    )

    adapter = HashWorkAdapter(4)
    interrupted = ResearchRunner(
        output_root=tmp_path / "isolated" / "interrupted",
        protected_roots=(tmp_path / "production",),
        code_identity="a" * 40,
        config=ResearchRunConfig(max_work_units=2, checkpoint_interval_units=1),
    )
    partial = interrupted.run("archive-run", prepared.run_input, adapter, seed=7)
    resumed = interrupted.run("archive-run", prepared.run_input, adapter, seed=7, resume=True)
    uninterrupted = ResearchRunner(
        output_root=tmp_path / "isolated" / "uninterrupted",
        protected_roots=(tmp_path / "production",),
        code_identity="a" * 40,
        config=ResearchRunConfig(max_work_units=4, checkpoint_interval_units=1),
    ).run("archive-run", prepared.run_input, adapter, seed=7)

    assert partial.status == "PAUSED"
    assert partial.completed_units == ("unit-000", "unit-001")
    assert resumed.status == uninterrupted.status == "COMPLETE"
    assert resumed.completed_units == uninterrupted.completed_units
    assert resumed.result_digest == uninterrupted.result_digest
    assert len(resumed.completed_units) == len(set(resumed.completed_units))

    changed_archive = _prepared(authority, second)
    with pytest.raises(ResumeMismatchError):
        interrupted.run("archive-run", changed_archive.run_input, adapter, seed=7, resume=True)
    with pytest.raises(ResumeMismatchError):
        interrupted.run(
            "archive-run",
            ResearchRunInput(
                replace(prepared.research_universe, content_hash="different-universe"),
                prepared.canonical_evidence,
                model_family="path_expert",
            ),
            adapter,
            seed=7,
            resume=True,
        )
    with pytest.raises(ResumeMismatchError, match="canonical evidence"):
        interrupted.run(
            "archive-run",
            ResearchRunInput(
                prepared.research_universe,
                replace(prepared.canonical_evidence, snapshot_id="different-evidence"),
                model_family="path_expert",
            ),
            adapter,
            seed=7,
            resume=True,
        )


def test_archive_mvn003_integration_rejects_unavailable_or_forbidden_inputs(tmp_path: Path) -> None:
    settings, manifest_path, (first, second) = _archive(tmp_path)
    authority = ResearchDataAuthority(settings, project_root=tmp_path)
    baseline = _prepared(authority, first)

    with pytest.raises(
        ArchiveResearchUnavailable, match="ARCHIVE_QUERY_NO_AS_OF_BOOK_MATERIALIZATIONS"
    ):
        from live15_quant.archive_mvn003 import prepare_archive_research_run

        prepare_archive_research_run(
            authority,
            ArchiveResearchQuery(
                first.first_event_id,
                first.last_event_id,
                NOW - timedelta(hours=8) + timedelta(microseconds=1),
                maximum_chunks=1,
            ),
            code_git_sha="a" * 40,
            experiment_id="future-cutoff",
            model_family="path_expert",
        )

    manifest = WsRetentionManifest(manifest_path)
    manifest.advance(second.chunk_id, ArchiveState.FAILED, now=NOW)
    manifest.quarantine_failed_chunk(second.chunk_id, now=NOW)
    with pytest.raises(ArchiveResearchUnavailable, match="ARCHIVE_CHUNK_NOT_RESEARCH_ELIGIBLE"):
        _prepared(authority, second)

    snapshot = tmp_path / "typed-input.json"
    write_research_input_snapshot(snapshot, baseline.run_input)
    envelope = json.loads(snapshot.read_text(encoding="utf-8"))
    envelope["payload"]["dataset_path"] = "forbidden.sqlite3"
    envelope["checksum"] = sha256(
        json.dumps(envelope["payload"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    snapshot.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ExperimentConflictError, match="Dataset"):
        load_research_input_snapshot(snapshot)

    with pytest.raises(ResearchRunnerPathError, match="protected"):
        ResearchRunner(
            output_root=tmp_path / "production" / "forbidden-output",
            protected_roots=(tmp_path / "production",),
            code_identity="a" * 40,
            config=ResearchRunConfig(max_work_units=1),
        )
