from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import live15_quant.cli as cli
from live15_quant.config import Settings
from live15_quant.research_data_authority import (
    FeatureFreshnessPolicy,
    ForwardOosFreshnessPolicy,
    FrozenHoldoutMetadata,
    ResearchDataAuthority,
    ResearchFreshnessPolicy,
    ResearchObservation,
    ResearchSourceManifest,
    ResearchSourceType,
    ResearchUniverseBuilder,
    SessionSemantics,
    TrainingRecencyPolicy,
    TrustTier,
    require_reproduction_only,
)
from tools import run_factor001r

NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)


def source(
    source_id: str,
    source_type: ResearchSourceType,
    tier: TrustTier,
    *,
    first: datetime = NOW - timedelta(days=8),
    latest: datetime = NOW,
    events: int = 8,
    observations: int = 16,
) -> ResearchSourceManifest:
    return ResearchSourceManifest(
        source_id=source_id,
        source_type=source_type,
        trust_tier=tier,
        provider_version="test-v1",
        schema_version="research-source-v1",
        earliest_timestamp=first,
        latest_timestamp=latest,
        utc_calendar_days=(first.date().isoformat(), latest.date().isoformat()),
        market_session_days=("session-a", "session-b"),
        assets=("BTC",),
        eligible_events=events,
        eligible_observations=observations,
        availability_semantics="strict_as_of",
        verification_state="VERIFIED",
        provenance="test",
        content_identity=f"{source_id}-content",
        limitations=(),
    )


def observation(
    source_id: str,
    source_type: ResearchSourceType,
    tier: TrustTier,
    *,
    event_id: str = "event-1",
    observation_id: str = "obs-1",
    content_hash: str = "same",
    value_hash: str = "same",
    timestamp: datetime = NOW - timedelta(days=3),
) -> ResearchObservation:
    return ResearchObservation(
        source_id=source_id,
        source_type=source_type,
        trust_tier=tier,
        event_id=event_id,
        observation_id=observation_id,
        equivalence_key=f"{event_id}:book:1",
        market_id="market-1",
        asset="BTC",
        source_timestamp=timestamp,
        received_timestamp=timestamp,
        utc_calendar_day=timestamp.date().isoformat(),
        market_session_day="session-a",
        content_hash=content_hash,
        value_hash=value_hash,
        quality_class="HISTORICAL_L2_SNAPSHOT",
    )


def policy() -> ResearchFreshnessPolicy:
    return ResearchFreshnessPolicy(
        feature_freshness=FeatureFreshnessPolicy(max_observation_age=timedelta(seconds=30)),
        training_recency=TrainingRecencyPolicy.expanding(),
        forward_oos_freshness=ForwardOosFreshnessPolicy(
            specification_frozen_at=NOW - timedelta(days=1)
        ),
    )


def test_validation_two_days_never_collapses_total_universe_days() -> None:
    snapshot = ResearchUniverseBuilder(
        freshness_policy=policy(),
        session_semantics=SessionSemantics("live15-session-v1"),
        sources=(source("recorder", ResearchSourceType.OWN_RECORDER, TrustTier.H0),),
        observations=(observation("recorder", ResearchSourceType.OWN_RECORDER, TrustTier.H0),),
        frozen_holdout=FrozenHoldoutMetadata.unrevealed(
            "dataset-v2", validation_days=("2026-08-26", "2026-08-27")
        ),
    ).build(cutoff_timestamp=NOW, code_git_sha="a" * 40)

    assert snapshot.validation_days == ("2026-08-26", "2026-08-27")
    assert snapshot.total_development_days == ("session-a",)
    assert snapshot.total_development_days != snapshot.validation_days
    assert snapshot.holdout_accessed is False


def test_feature_freshness_is_independent_from_training_recency_and_oos() -> None:
    freshness = policy()
    decision = NOW
    assert freshness.feature_freshness.is_available(
        source_timestamp=decision - timedelta(seconds=20),
        received_timestamp=decision - timedelta(seconds=10),
        decision_timestamp=decision,
    )
    assert not freshness.feature_freshness.is_available(
        source_timestamp=decision - timedelta(seconds=31),
        received_timestamp=decision - timedelta(seconds=10),
        decision_timestamp=decision,
    )
    assert freshness.training_recency.mode == "EXPANDING"
    assert freshness.forward_oos_freshness.is_forward_oos(NOW)
    assert "freshness_days" not in freshness.to_manifest()


def test_old_decision_time_valid_observation_remains_development_eligible() -> None:
    old = NOW - timedelta(days=30)
    snapshot = ResearchUniverseBuilder(
        freshness_policy=policy(),
        session_semantics=SessionSemantics("live15-session-v1"),
        sources=(
            source(
                "official",
                ResearchSourceType.KALSHI_OFFICIAL_HISTORY,
                TrustTier.H1,
                first=old,
                latest=old,
            ),
        ),
        observations=(
            observation(
                "official", ResearchSourceType.KALSHI_OFFICIAL_HISTORY, TrustTier.H1, timestamp=old
            ),
        ),
        frozen_holdout=FrozenHoldoutMetadata.unrevealed("dataset-v2"),
    ).build(cutoff_timestamp=NOW, code_git_sha="b" * 40)

    assert snapshot.eligible_observations == 1
    assert snapshot.earliest_timestamp == old


def test_holdout_overlap_is_excluded_without_exposing_payload() -> None:
    snapshot = ResearchUniverseBuilder(
        freshness_policy=policy(),
        session_semantics=SessionSemantics("live15-session-v1"),
        sources=(source("recorder", ResearchSourceType.OWN_RECORDER, TrustTier.H0),),
        observations=(observation("recorder", ResearchSourceType.OWN_RECORDER, TrustTier.H0),),
        frozen_holdout=FrozenHoldoutMetadata.unrevealed(
            "dataset-v2", excluded_event_ids=("event-1",)
        ),
    ).build(cutoff_timestamp=NOW, code_git_sha="c" * 40)

    assert snapshot.eligible_observations == 0
    assert snapshot.holdout_excluded_observations == 1
    assert "label" not in snapshot.to_public_dict()
    assert "feature" not in snapshot.to_public_dict()


def test_owned_source_precedence_is_deterministic_and_duplicate_is_not_double_counted() -> None:
    snapshot = ResearchUniverseBuilder(
        freshness_policy=policy(),
        session_semantics=SessionSemantics("live15-session-v1"),
        sources=(
            source("official", ResearchSourceType.KALSHI_OFFICIAL_HISTORY, TrustTier.H1),
            source("recorder", ResearchSourceType.OWN_RECORDER, TrustTier.H0),
        ),
        observations=(
            observation("official", ResearchSourceType.KALSHI_OFFICIAL_HISTORY, TrustTier.H1),
            observation("recorder", ResearchSourceType.OWN_RECORDER, TrustTier.H0),
        ),
        frozen_holdout=FrozenHoldoutMetadata.unrevealed("dataset-v2"),
    ).build(cutoff_timestamp=NOW, code_git_sha="d" * 40)

    assert snapshot.eligible_observations == 1
    assert snapshot.deduplicated_observations == 1
    assert snapshot.selected_source_ids == ("recorder",)


def test_conflicting_equivalent_sources_are_quarantined_not_silently_selected() -> None:
    snapshot = ResearchUniverseBuilder(
        freshness_policy=policy(),
        session_semantics=SessionSemantics("live15-session-v1"),
        sources=(
            source("official", ResearchSourceType.KALSHI_OFFICIAL_HISTORY, TrustTier.H1),
            source("recorder", ResearchSourceType.OWN_RECORDER, TrustTier.H0),
        ),
        observations=(
            observation(
                "official",
                ResearchSourceType.KALSHI_OFFICIAL_HISTORY,
                TrustTier.H1,
                value_hash="other",
            ),
            observation("recorder", ResearchSourceType.OWN_RECORDER, TrustTier.H0),
        ),
        frozen_holdout=FrozenHoldoutMetadata.unrevealed("dataset-v2"),
    ).build(cutoff_timestamp=NOW, code_git_sha="e" * 40)

    assert snapshot.eligible_observations == 0
    assert snapshot.conflicting_observations == 2
    assert snapshot.quarantined_observations == 2


def test_observation_cannot_claim_a_tier_or_type_different_from_its_registry_source() -> None:
    with pytest.raises(ValueError, match="must match its registered source"):
        ResearchUniverseBuilder(
            freshness_policy=policy(),
            session_semantics=SessionSemantics("live15-session-v1"),
            sources=(source("official", ResearchSourceType.KALSHI_OFFICIAL_HISTORY, TrustTier.H1),),
            observations=(
                observation(
                    "official",
                    ResearchSourceType.OWN_RECORDER,
                    TrustTier.H0,
                ),
            ),
            frozen_holdout=FrozenHoldoutMetadata.unrevealed("dataset-v2"),
        ).build(cutoff_timestamp=NOW, code_git_sha="tier" * 10)


def test_unverified_h2_observation_is_quarantined_and_cannot_enter_research() -> None:
    unverified_h2 = replace(
        source(
            "depthfeed",
            ResearchSourceType.DEPTHFEED_KALSHI_L2,
            TrustTier.H2,
        ),
        verification_state="CONFIGURED_NO_ACQUISITION",
    )
    snapshot = ResearchUniverseBuilder(
        freshness_policy=policy(),
        session_semantics=SessionSemantics("live15-session-v1"),
        sources=(unverified_h2,),
        observations=(
            observation(
                "depthfeed",
                ResearchSourceType.DEPTHFEED_KALSHI_L2,
                TrustTier.H2,
            ),
        ),
        frozen_holdout=FrozenHoldoutMetadata.unrevealed("dataset-v2"),
    ).build(cutoff_timestamp=NOW, code_git_sha="h2" * 20)

    assert snapshot.eligible_observations == 0
    assert snapshot.quarantined_observations == 1


def test_same_inputs_are_deterministic_and_new_recorder_coverage_changes_universe() -> None:
    builder = ResearchUniverseBuilder(
        freshness_policy=policy(),
        session_semantics=SessionSemantics("live15-session-v1"),
        sources=(source("recorder", ResearchSourceType.OWN_RECORDER, TrustTier.H0),),
        observations=(observation("recorder", ResearchSourceType.OWN_RECORDER, TrustTier.H0),),
        frozen_holdout=FrozenHoldoutMetadata.unrevealed("dataset-v2"),
    )
    first = builder.build(cutoff_timestamp=NOW, code_git_sha="f" * 40)
    second = builder.build(cutoff_timestamp=NOW, code_git_sha="f" * 40)
    newer = ResearchUniverseBuilder(
        freshness_policy=policy(),
        session_semantics=SessionSemantics("live15-session-v1"),
        sources=(
            source(
                "recorder",
                ResearchSourceType.OWN_RECORDER,
                TrustTier.H0,
                latest=NOW + timedelta(days=1),
            ),
        ),
        observations=(
            observation(
                "recorder",
                ResearchSourceType.OWN_RECORDER,
                TrustTier.H0,
                timestamp=NOW + timedelta(days=1),
            ),
        ),
        frozen_holdout=FrozenHoldoutMetadata.unrevealed("dataset-v2"),
    ).build(cutoff_timestamp=NOW + timedelta(days=1), code_git_sha="f" * 40)

    assert first.content_hash == second.content_hash
    assert first.universe_id == second.universe_id
    assert newer.content_hash != first.content_hash


def test_holdout_metadata_rejects_payload_fields() -> None:
    with pytest.raises(TypeError, match="metadata-only"):
        FrozenHoldoutMetadata("dataset-v2", "UNREVEALED_FROZEN", payload={"label": "yes"})


def test_aggregate_runtime_coverage_never_sums_overlapping_recorder_and_archive(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current.sqlite3"
    archive = tmp_path / "archive.sqlite3"
    with sqlite3.connect(current) as connection:
        connection.executescript(
            """
            CREATE TABLE current_trainable_events (
              eligibility_status TEXT, window_start TEXT, window_end TEXT, asset TEXT,
              materialized_timestamp TEXT
            );
            CREATE TABLE current_trainable_rows (decision_timestamp TEXT);
            INSERT INTO current_trainable_events VALUES
              ('eligible', '2026-08-20T00:00:00+00:00',
               '2026-08-20T00:15:00+00:00', 'BTC', '2026-08-20T00:16:00+00:00'),
              ('eligible', '2026-08-20T00:15:00+00:00',
               '2026-08-20T00:30:00+00:00', 'BTC', '2026-08-20T00:31:00+00:00');
            INSERT INTO current_trainable_rows VALUES
              ('2026-08-20T00:01:00+00:00'), ('2026-08-20T00:16:00+00:00');
            """
        )
    with sqlite3.connect(archive) as connection:
        connection.executescript(
            """
            CREATE TABLE ws_retention_chunks (
              state TEXT, event_count INTEGER, first_source_timestamp TEXT,
              last_source_timestamp TEXT, last_event_id INTEGER
            );
            INSERT INTO ws_retention_chunks VALUES
              ('purged', 100, '2026-08-20T00:00:00+00:00', '2026-08-20T00:30:00+00:00', 100);
            """
        )
    settings = Settings(
        recorder_data_path=tmp_path / "raw.sqlite3",
        feature_store_path=tmp_path / "features.sqlite3",
        paper_data_path=tmp_path / "paper.sqlite3",
        recorder_health_path=tmp_path / "health.json",
        current_trainable_path=current,
        ws_archive_manifest_path=archive,
    )
    authority = ResearchDataAuthority(settings, project_root=tmp_path)
    snapshot = authority.snapshot()

    assert snapshot.eligible_events == 2
    assert snapshot.eligible_observations == 2
    assert snapshot.selected_source_ids == ("live15_current_trainable",)
    with sqlite3.connect(current) as connection:
        connection.execute(
            "INSERT INTO current_trainable_events VALUES (?, ?, ?, ?, ?)",
            (
                "eligible",
                "2026-08-21T00:00:00+00:00",
                "2026-08-21T00:15:00+00:00",
                "BTC",
                "2026-08-21T00:16:00+00:00",
            ),
        )
    refreshed = authority.snapshot()
    assert refreshed.eligible_events == 3
    assert refreshed.content_hash != snapshot.content_hash


def test_current_model_entrypoints_require_explicit_reproduction_acknowledgement() -> None:
    with pytest.raises(SystemExit):
        cli.model_zoo_main(["--dataset", "immutable-dataset"])
    with pytest.raises(SystemExit):
        cli.model_zoo_v2_main(["--dataset", "immutable-dataset", "--v1-model-zoo", "v1"])
    require_reproduction_only(reproduction_only=True, entrypoint="test")


def test_legacy_factor_entrypoint_requires_explicit_reproduction_acknowledgement(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_factor001r.py",
            "--dataset-root",
            "immutable-dataset",
            "--output-json",
            "out.json",
            "--output-md",
            "out.md",
        ],
    )
    with pytest.raises(SystemExit):
        run_factor001r.main()
