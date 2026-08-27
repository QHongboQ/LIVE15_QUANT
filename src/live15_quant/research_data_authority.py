"""Typed, read-only authority for LIVE15 research data coverage.

This module deliberately separates decision-time feature freshness, development-history
recency, and post-specification forward OOS evidence.  It only reads aggregate metadata;
Dataset v2 holdout payloads, Recorder mutation, factor execution, and model training are
outside this boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from live15_quant.config import Settings
from live15_quant.historical_providers import (
    DEPTHFEED_INTEGRATION_READY_KEY_REQUIRED,
    DEPTHFEED_KALSHI_L2,
    DEPTHFEED_NOT_CONFIGURED,
    KALSHI_OFFICIAL,
    depthfeed_key_status,
)

if TYPE_CHECKING:
    from live15_quant.archive_research import ArchiveResearchQuery, ArchiveResearchSelection

RESEARCH_FRESHNESS_POLICY_VERSION = "research-freshness-v1"
RESEARCH_SOURCE_REGISTRY_VERSION = "research-source-registry-v1"
RESEARCH_UNIVERSE_SCHEMA_VERSION = "research-universe-v1"
SESSION_SEMANTICS_VERSION = "live15-session-v1"
_PRECEDENCE = {"H0": 0, "H1": 1, "H2": 2}
CAPABILITY_DAY_KEYS = (
    "PATH_TERMINAL_DAYS",
    "TRADE_SEQUENCE_DAYS",
    "L2_SNAPSHOT_DAYS",
    "L2_DELTA_DAYS",
    "LIVE_NATIVE_DAYS",
    "FORWARD_OOS_DAYS",
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("research timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed) if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _canonical(value: object) -> object:
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def _hash(value: object) -> str:
    encoded = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ResearchSourceType(StrEnum):
    OWN_RECORDER = "OWN_RECORDER"
    OWN_VERIFIED_ARCHIVE = "OWN_VERIFIED_ARCHIVE"
    KALSHI_OFFICIAL_HISTORY = "KALSHI_OFFICIAL_HISTORY"
    DEPTHFEED_KALSHI_L2 = "DEPTHFEED_KALSHI_L2"


class TrustTier(StrEnum):
    H0 = "H0"
    H1 = "H1"
    H2 = "H2"


class TrainingRecencyMode(StrEnum):
    EXPANDING = "EXPANDING"
    ROLLING_SESSIONS = "ROLLING_SESSIONS"
    TIME_DECAY = "TIME_DECAY"


@dataclass(frozen=True, slots=True)
class FeatureFreshnessPolicy:
    """Strict per-decision as-of contract, measured in seconds/minutes."""

    max_observation_age: timedelta

    def __post_init__(self) -> None:
        if self.max_observation_age <= timedelta(0):
            raise ValueError("max_observation_age must be positive")

    def is_available(
        self,
        *,
        source_timestamp: datetime,
        received_timestamp: datetime,
        decision_timestamp: datetime,
    ) -> bool:
        source = _utc(source_timestamp)
        received = _utc(received_timestamp)
        decision = _utc(decision_timestamp)
        return (
            source <= decision
            and received <= decision
            and decision - source <= self.max_observation_age
            and decision - received <= self.max_observation_age
        )

    def to_manifest(self) -> dict[str, object]:
        return {
            "max_observation_age_seconds": self.max_observation_age.total_seconds(),
            "as_of": "STRICT",
        }


@dataclass(frozen=True, slots=True)
class TrainingRecencyPolicy:
    """Development history policy.  It intentionally has no feature freshness field."""

    mode: TrainingRecencyMode
    rolling_session_count: int | None = None
    age_weight_half_life_days: float | None = None

    @classmethod
    def expanding(cls) -> TrainingRecencyPolicy:
        return cls(TrainingRecencyMode.EXPANDING)

    def __post_init__(self) -> None:
        if self.mode is TrainingRecencyMode.ROLLING_SESSIONS:
            if self.rolling_session_count is None or self.rolling_session_count <= 0:
                raise ValueError("rolling sessions requires a positive session count")
        elif self.rolling_session_count is not None:
            raise ValueError("rolling_session_count only applies to ROLLING_SESSIONS")
        if self.mode is TrainingRecencyMode.TIME_DECAY:
            if self.age_weight_half_life_days is None or self.age_weight_half_life_days <= 0:
                raise ValueError("time decay requires a positive half-life")
        elif self.age_weight_half_life_days is not None:
            raise ValueError("age weighting only applies to TIME_DECAY")

    def to_manifest(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "rolling_session_count": self.rolling_session_count,
            "age_weight_half_life_days": self.age_weight_half_life_days,
        }


@dataclass(frozen=True, slots=True)
class ForwardOosFreshnessPolicy:
    """Post-specification evidence contract; never a substitute for development history."""

    specification_frozen_at: datetime

    def __post_init__(self) -> None:
        _utc(self.specification_frozen_at)

    def is_forward_oos(self, timestamp: datetime) -> bool:
        return _utc(timestamp) > _utc(self.specification_frozen_at)

    def to_manifest(self) -> dict[str, object]:
        return {
            "specification_frozen_at": _utc(self.specification_frozen_at).isoformat(),
            "strictly_after": True,
        }


@dataclass(frozen=True, slots=True)
class ResearchFreshnessPolicy:
    feature_freshness: FeatureFreshnessPolicy
    training_recency: TrainingRecencyPolicy
    forward_oos_freshness: ForwardOosFreshnessPolicy
    version: str = RESEARCH_FRESHNESS_POLICY_VERSION

    def to_manifest(self) -> dict[str, object]:
        return {
            "version": self.version,
            "feature_freshness": self.feature_freshness.to_manifest(),
            "training_recency": self.training_recency.to_manifest(),
            "forward_oos_freshness": self.forward_oos_freshness.to_manifest(),
        }


@dataclass(frozen=True, slots=True)
class SessionSemantics:
    version: str = SESSION_SEMANTICS_VERSION

    def utc_calendar_day(self, timestamp: datetime) -> str:
        return _utc(timestamp).date().isoformat()

    def market_session_day(
        self, *, event_window_start: datetime | None, timestamp: datetime
    ) -> str:
        # The type is deliberately distinct even when a continuous crypto session currently
        # maps to the window's UTC date.  Callers must not relabel it as calendar coverage.
        return self.utc_calendar_day(event_window_start or timestamp)


@dataclass(frozen=True, slots=True)
class FrozenHoldoutMetadata:
    """Safe identity/time exclusion metadata; it cannot contain holdout payloads."""

    dataset_id: str
    status: str
    excluded_event_ids: tuple[str, ...] = ()
    excluded_time_ranges: tuple[tuple[datetime, datetime], ...] = ()
    validation_days: tuple[str, ...] = ()
    payload: Mapping[str, object] | None = None

    @classmethod
    def unrevealed(
        cls,
        dataset_id: str,
        *,
        excluded_event_ids: Sequence[str] = (),
        excluded_time_ranges: Sequence[tuple[datetime, datetime]] = (),
        validation_days: Sequence[str] = (),
    ) -> FrozenHoldoutMetadata:
        return cls(
            dataset_id,
            "UNREVEALED_FROZEN",
            tuple(sorted(set(excluded_event_ids))),
            tuple(excluded_time_ranges),
            tuple(sorted(set(validation_days))),
        )

    def __post_init__(self) -> None:
        if self.status != "UNREVEALED_FROZEN":
            raise ValueError("only UNREVEALED_FROZEN holdout metadata is accepted")
        if self.payload is not None:
            raise TypeError("frozen holdout is metadata-only; payload access is forbidden")
        for start, end in self.excluded_time_ranges:
            if _utc(start) > _utc(end):
                raise ValueError("holdout exclusion range is reversed")

    def excludes(self, observation: ResearchObservation) -> bool:
        if observation.event_id in self.excluded_event_ids:
            return True
        return any(
            _utc(start) <= observation.source_timestamp <= _utc(end)
            for start, end in self.excluded_time_ranges
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "status": self.status,
            "excluded_event_count": len(self.excluded_event_ids),
            "excluded_time_range_count": len(self.excluded_time_ranges),
            "validation_days": list(self.validation_days),
            "payload_accessed": False,
        }


@dataclass(frozen=True, slots=True)
class ResearchSourceManifest:
    source_id: str
    source_type: ResearchSourceType
    trust_tier: TrustTier
    provider_version: str | None
    schema_version: str
    earliest_timestamp: datetime | None
    latest_timestamp: datetime | None
    utc_calendar_days: tuple[str, ...]
    market_session_days: tuple[str, ...]
    assets: tuple[str, ...]
    eligible_events: int
    eligible_observations: int
    availability_semantics: str
    verification_state: str
    provenance: str
    content_identity: str
    limitations: tuple[str, ...] = ()
    capability_days: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    coverage_status: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_id or not self.content_identity:
            raise ValueError("source identity is required")
        if self.earliest_timestamp is not None:
            _utc(self.earliest_timestamp)
        if self.latest_timestamp is not None:
            _utc(self.latest_timestamp)
        if (
            self.earliest_timestamp
            and self.latest_timestamp
            and self.earliest_timestamp > self.latest_timestamp
        ):
            raise ValueError("source coverage is reversed")
        if self.eligible_events < 0 or self.eligible_observations < 0:
            raise ValueError("source counts cannot be negative")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type.value,
            "trust_tier": self.trust_tier.value,
            "provider_version": self.provider_version,
            "schema_version": self.schema_version,
            "earliest_timestamp": _utc(self.earliest_timestamp).isoformat()
            if self.earliest_timestamp
            else None,
            "latest_timestamp": _utc(self.latest_timestamp).isoformat()
            if self.latest_timestamp
            else None,
            "utc_calendar_days": list(self.utc_calendar_days),
            "market_session_days": list(self.market_session_days),
            "assets": list(self.assets),
            "eligible_events": self.eligible_events,
            "eligible_observations": self.eligible_observations,
            "availability_semantics": self.availability_semantics,
            "verification_state": self.verification_state,
            "provenance": self.provenance,
            "content_identity": self.content_identity,
            "limitations": list(self.limitations),
            "capability_days": {
                key: list(value) for key, value in sorted(self.capability_days.items())
            },
            "coverage_status": dict(sorted(self.coverage_status.items())),
        }


@dataclass(frozen=True, slots=True)
class ResearchObservation:
    source_id: str
    source_type: ResearchSourceType
    trust_tier: TrustTier
    event_id: str
    observation_id: str
    equivalence_key: str
    market_id: str
    asset: str
    source_timestamp: datetime
    received_timestamp: datetime
    utc_calendar_day: str
    market_session_day: str
    content_hash: str
    value_hash: str
    quality_class: str

    def __post_init__(self) -> None:
        _utc(self.source_timestamp)
        _utc(self.received_timestamp)
        if not all(
            (
                self.source_id,
                self.event_id,
                self.observation_id,
                self.equivalence_key,
                self.content_hash,
                self.value_hash,
            )
        ):
            raise ValueError("observation identity is required")


@dataclass(frozen=True, slots=True)
class ResearchUniverseSnapshot:
    universe_id: str
    content_hash: str
    cutoff_timestamp: datetime
    code_git_sha: str
    freshness_policy: ResearchFreshnessPolicy
    session_semantics_version: str
    source_manifests: tuple[ResearchSourceManifest, ...]
    earliest_timestamp: datetime | None
    latest_timestamp: datetime | None
    utc_calendar_days: tuple[str, ...]
    market_session_days: tuple[str, ...]
    eligible_development_days: tuple[str, ...]
    validation_days: tuple[str, ...]
    assets: tuple[str, ...]
    eligible_events: int
    eligible_observations: int
    deduplicated_observations: int
    conflicting_observations: int
    quarantined_observations: int
    holdout_excluded_observations: int
    selected_source_ids: tuple[str, ...]
    frozen_holdout: FrozenHoldoutMetadata
    depthfeed_status: str
    capability_days: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def total_development_days(self) -> tuple[str, ...]:
        return self.eligible_development_days

    @property
    def holdout_accessed(self) -> bool:
        return False

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema_version": RESEARCH_UNIVERSE_SCHEMA_VERSION,
            "universe_id": self.universe_id,
            "content_hash": self.content_hash,
            "cutoff_timestamp": _utc(self.cutoff_timestamp).isoformat(),
            "code_git_sha": self.code_git_sha,
            "freshness_policy": self.freshness_policy.to_manifest(),
            "session_semantics_version": self.session_semantics_version,
            "source_registry_version": RESEARCH_SOURCE_REGISTRY_VERSION,
            "sources": [item.to_public_dict() for item in self.source_manifests],
            "earliest_timestamp": _utc(self.earliest_timestamp).isoformat()
            if self.earliest_timestamp
            else None,
            "latest_timestamp": _utc(self.latest_timestamp).isoformat()
            if self.latest_timestamp
            else None,
            "utc_calendar_days": list(self.utc_calendar_days),
            "market_session_days": list(self.market_session_days),
            "eligible_development_days": list(self.eligible_development_days),
            "validation_days": list(self.validation_days),
            "assets": list(self.assets),
            "eligible_events": self.eligible_events,
            "eligible_observations": self.eligible_observations,
            "deduplicated_observations": self.deduplicated_observations,
            "conflicting_observations": self.conflicting_observations,
            "quarantined_observations": self.quarantined_observations,
            "holdout_excluded_observations": self.holdout_excluded_observations,
            "selected_source_ids": list(self.selected_source_ids),
            "frozen_holdout": self.frozen_holdout.to_public_dict(),
            "holdout_accessed": False,
            "depthfeed_status": self.depthfeed_status,
            "capability_days": {
                key: list(value) for key, value in sorted(self.capability_days.items())
            },
        }


@dataclass(frozen=True, slots=True)
class ResearchUniverseBuilder:
    freshness_policy: ResearchFreshnessPolicy
    session_semantics: SessionSemantics
    sources: tuple[ResearchSourceManifest, ...]
    observations: tuple[ResearchObservation, ...]
    frozen_holdout: FrozenHoldoutMetadata

    def build(self, *, cutoff_timestamp: datetime, code_git_sha: str) -> ResearchUniverseSnapshot:
        cutoff = _utc(cutoff_timestamp)
        if len(code_git_sha) < 7:
            raise ValueError("code_git_sha is required")
        source_map = {item.source_id: item for item in self.sources}
        if len(source_map) != len(self.sources):
            raise ValueError("source registry IDs must be unique")
        accepted: list[ResearchObservation] = []
        holdout_excluded = 0
        unverified_excluded = 0
        groups: dict[str, list[ResearchObservation]] = defaultdict(list)
        for item in self.observations:
            registered = source_map.get(item.source_id)
            if registered is None:
                raise ValueError("every observation must have a registered source")
            if (
                item.source_type is not registered.source_type
                or item.trust_tier is not registered.trust_tier
            ):
                raise ValueError("observation type and tier must match its registered source")
            if not registered.verification_state.startswith("VERIFIED"):
                unverified_excluded += 1
                continue
            if item.source_timestamp > cutoff or item.received_timestamp > cutoff:
                continue
            if self.frozen_holdout.excludes(item):
                holdout_excluded += 1
                continue
            groups[item.equivalence_key].append(item)
        deduplicated = conflicts = 0
        quarantined = unverified_excluded
        for equivalent in groups.values():
            hashes = {item.value_hash for item in equivalent}
            if len(hashes) > 1:
                conflicts += len(equivalent)
                quarantined += len(equivalent)
                continue
            ordered = sorted(
                equivalent,
                key=lambda item: (
                    _PRECEDENCE[item.trust_tier.value],
                    item.source_id,
                    item.observation_id,
                ),
            )
            accepted.append(ordered[0])
            deduplicated += len(ordered) - 1
        accepted = sorted(
            accepted, key=lambda item: (item.source_timestamp, item.source_id, item.observation_id)
        )
        manifests = tuple(
            sorted(self.sources, key=lambda item: (item.trust_tier.value, item.source_id))
        )
        utc_days = tuple(sorted({item.utc_calendar_day for item in accepted}))
        session_days = tuple(sorted({item.market_session_day for item in accepted}))
        timestamps = [item.source_timestamp for item in accepted]
        payload = {
            "schema_version": RESEARCH_UNIVERSE_SCHEMA_VERSION,
            "cutoff_timestamp": cutoff,
            "code_git_sha": code_git_sha,
            "freshness_policy": self.freshness_policy.to_manifest(),
            "session_semantics_version": self.session_semantics.version,
            "sources": [item.to_public_dict() for item in manifests],
            "accepted": [
                (item.source_id, item.observation_id, item.content_hash) for item in accepted
            ],
            "holdout": self.frozen_holdout.to_public_dict(),
            "counts": (deduplicated, conflicts, quarantined, holdout_excluded),
        }
        content_hash = _hash(payload)
        capability_days = {
            key: tuple(
                sorted({day for source in manifests for day in source.capability_days.get(key, ())})
            )
            for key in CAPABILITY_DAY_KEYS
        }
        return ResearchUniverseSnapshot(
            universe_id=f"research-universe-{content_hash[:20]}",
            content_hash=content_hash,
            cutoff_timestamp=cutoff,
            code_git_sha=code_git_sha,
            freshness_policy=self.freshness_policy,
            session_semantics_version=self.session_semantics.version,
            source_manifests=manifests,
            earliest_timestamp=min(timestamps) if timestamps else None,
            latest_timestamp=max(timestamps) if timestamps else None,
            utc_calendar_days=utc_days,
            market_session_days=session_days,
            eligible_development_days=session_days,
            validation_days=self.frozen_holdout.validation_days,
            assets=tuple(
                sorted(
                    {item.asset for item in accepted}
                    | {asset for source in manifests for asset in source.assets}
                )
            ),
            eligible_events=len({item.event_id for item in accepted}),
            eligible_observations=len(accepted),
            deduplicated_observations=deduplicated,
            conflicting_observations=conflicts,
            quarantined_observations=quarantined,
            holdout_excluded_observations=holdout_excluded,
            selected_source_ids=tuple(
                sorted(
                    {item.source_id for item in accepted},
                    key=lambda value: (_PRECEDENCE[source_map[value].trust_tier.value], value),
                )
            ),
            frozen_holdout=self.frozen_holdout,
            depthfeed_status=next(
                (
                    item.verification_state
                    for item in manifests
                    if item.source_type is ResearchSourceType.DEPTHFEED_KALSHI_L2
                ),
                DEPTHFEED_NOT_CONFIGURED,
            ),
            capability_days=capability_days,
        )


def _readonly_connection(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


def _one(connection: sqlite3.Connection, query: str) -> Mapping[str, object]:
    cursor = connection.execute(query)
    row = cursor.fetchone()
    if row is None:
        return {}
    return dict(zip((column[0] for column in cursor.description), row, strict=True))


def _days(connection: sqlite3.Connection, table: str, column: str) -> tuple[str, ...]:
    try:
        rows = connection.execute(
            f"SELECT DISTINCT substr({column}, 1, 10) FROM {table} "
            f"WHERE {column} IS NOT NULL ORDER BY 1"
        ).fetchall()
    except sqlite3.OperationalError:
        return ()
    return tuple(str(row[0]) for row in rows if row[0])


def _safe_json(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _day_range(start: datetime | None, end: datetime | None) -> tuple[str, ...]:
    if start is None or end is None:
        return ()
    current, last = _utc(start).date(), _utc(end).date()
    return tuple(
        (current + timedelta(days=index)).isoformat() for index in range((last - current).days + 1)
    )


class ResearchDataAuthority:
    """Low-cost read-only source registry with process-local high-water caching."""

    def __init__(self, settings: Settings, *, project_root: Path | None = None) -> None:
        self.settings = settings
        self.project_root = project_root or Path.cwd()
        self._cached_key: str | None = None
        self._cached_snapshot: ResearchUniverseSnapshot | None = None

    def snapshot(self, *, code_git_sha: str = "runtime") -> ResearchUniverseSnapshot:
        sources, holdout = self._source_manifests()
        key = _hash(
            {
                "sources": [item.to_public_dict() for item in sources],
                "frozen_holdout": holdout.to_public_dict(),
            }
        )
        if self._cached_key == key and self._cached_snapshot is not None:
            return self._cached_snapshot
        snapshot = ResearchUniverseBuilder(
            freshness_policy=ResearchFreshnessPolicy(
                FeatureFreshnessPolicy(timedelta(seconds=30)),
                TrainingRecencyPolicy.expanding(),
                ForwardOosFreshnessPolicy(datetime(2026, 8, 20, tzinfo=UTC)),
            ),
            session_semantics=SessionSemantics(),
            sources=tuple(sources),
            observations=(),
            frozen_holdout=holdout,
        ).build(cutoff_timestamp=datetime.now(UTC), code_git_sha=code_git_sha)
        # Aggregate-only source manifests still define present coverage when the UI does not
        # materialize observations.  This keeps runtime reads bounded and never opens holdout rows.
        snapshot = _with_source_coverage(snapshot)
        self._cached_key, self._cached_snapshot = key, snapshot
        return snapshot

    def archive_research_snapshot(
        self, query: ArchiveResearchQuery, *, code_git_sha: str
    ) -> tuple[ResearchUniverseSnapshot, ArchiveResearchSelection]:
        """Build a bounded RDA snapshot from explicit replay-verified archive materialization.

        This is intentionally separate from :meth:`snapshot`: the normal runtime
        registry stays aggregate-only, while research must opt into an exact range
        and as-of cutoff.
        """

        from .archive_research import ArchiveResearchSourceAdapter, ArchiveResearchUnavailable

        if self.settings.ws_archive_root is None or self.settings.ws_archive_manifest_path is None:
            raise ArchiveResearchUnavailable("ARCHIVE_NOT_CONFIGURED")
        selection = ArchiveResearchSourceAdapter(
            self.settings.ws_archive_root, self.settings.ws_archive_manifest_path
        ).materialize(query)
        if not selection.available:
            raise ArchiveResearchUnavailable(selection.reason or "ARCHIVE_RESEARCH_UNAVAILABLE")
        source = selection.source_manifest()
        observations = tuple(item.research_observation() for item in selection.materializations)
        snapshot = ResearchUniverseBuilder(
            freshness_policy=ResearchFreshnessPolicy(
                FeatureFreshnessPolicy(timedelta(seconds=30)),
                TrainingRecencyPolicy.expanding(),
                ForwardOosFreshnessPolicy(datetime(2026, 8, 20, tzinfo=UTC)),
            ),
            session_semantics=SessionSemantics(),
            sources=(source,),
            observations=observations,
            frozen_holdout=self._holdout_metadata(),
        ).build(cutoff_timestamp=query.as_of_timestamp, code_git_sha=code_git_sha)
        return snapshot, selection

    def _source_manifests(self) -> tuple[list[ResearchSourceManifest], FrozenHoldoutMetadata]:
        return (
            [
                self._recorder_manifest(),
                self._archive_manifest(),
                self._official_manifest(),
                self._depthfeed_manifest(),
            ],
            self._holdout_metadata(),
        )

    def _recorder_manifest(self) -> ResearchSourceManifest:
        path = self.settings.current_trainable_path
        connection = _readonly_connection(path)
        if connection is None:
            return _missing_source(
                "live15_current_trainable",
                ResearchSourceType.OWN_RECORDER,
                TrustTier.H0,
                "CURRENT_TRAINABLE_UNAVAILABLE",
            )
        try:
            aggregate = _one(
                connection,
                "SELECT count(*) events, min(window_start) earliest, max(window_end) latest, "
                "count(DISTINCT asset) assets, max(materialized_timestamp) highwater "
                "FROM current_trainable_events WHERE eligibility_status='eligible'",
            )
            rows = _one(connection, "SELECT count(*) rows FROM current_trainable_rows")
            assets = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT asset FROM current_trainable_events "
                    "WHERE eligibility_status='eligible' ORDER BY asset"
                )
                if row[0]
            )
            days = _days(connection, "current_trainable_events", "window_start")
            return ResearchSourceManifest(
                "live15_current_trainable",
                ResearchSourceType.OWN_RECORDER,
                TrustTier.H0,
                "live15",
                "current-trainable-v1",
                _parse_timestamp(aggregate.get("earliest")),
                _parse_timestamp(aggregate.get("latest")),
                days,
                days,
                assets,
                int(aggregate.get("events") or 0),
                int(rows.get("rows") or 0),
                "strict_as_of_materialized_decisions",
                "VERIFIED_METADATA",
                "LIVE15 current trainable projection",
                _hash(aggregate),
                capability_days={
                    "LIVE_NATIVE_DAYS": days,
                    "PATH_TERMINAL_DAYS": days,
                },
            )
        except sqlite3.Error:
            return _missing_source(
                "live15_current_trainable",
                ResearchSourceType.OWN_RECORDER,
                TrustTier.H0,
                "CURRENT_TRAINABLE_READ_ERROR",
            )
        finally:
            connection.close()

    def _archive_manifest(self) -> ResearchSourceManifest:
        path = self.settings.ws_archive_manifest_path
        if path is None:
            return _missing_source(
                "live15_verified_archive",
                ResearchSourceType.OWN_VERIFIED_ARCHIVE,
                TrustTier.H0,
                "ARCHIVE_NOT_CONFIGURED",
            )
        connection = _readonly_connection(path)
        if connection is None:
            return _missing_source(
                "live15_verified_archive",
                ResearchSourceType.OWN_VERIFIED_ARCHIVE,
                TrustTier.H0,
                "ARCHIVE_MANIFEST_UNAVAILABLE",
            )
        try:
            aggregate = _one(
                connection,
                "SELECT count(*) chunks, coalesce(sum(event_count),0) observations, "
                "min(first_source_timestamp) earliest, max(last_source_timestamp) latest, "
                "max(last_event_id) highwater FROM ws_retention_chunks "
                "WHERE state IN ('replay_verified','purge_eligible','purged')",
            )
            days = _days(connection, "ws_retention_chunks", "first_source_timestamp")
            return ResearchSourceManifest(
                "live15_verified_archive",
                ResearchSourceType.OWN_VERIFIED_ARCHIVE,
                TrustTier.H0,
                "ws-archive",
                "ws-retention-v1",
                _parse_timestamp(aggregate.get("earliest")),
                _parse_timestamp(aggregate.get("latest")),
                days,
                days,
                (),
                int(aggregate.get("chunks") or 0),
                int(aggregate.get("observations") or 0),
                "replay_verified_archive",
                "VERIFIED_METADATA",
                "LIVE15 retention manifest",
                _hash(aggregate),
                ("quarantined chunks are excluded",),
                capability_days={"LIVE_NATIVE_DAYS": days},
                coverage_status={
                    "metadata": "VERIFIED_METADATA",
                    "replay": "REPLAY_VERIFIED",
                    "materialization": "EXPLICIT_QUERY_REQUIRED",
                    "selection": "NOT_SELECTED",
                },
            )
        except sqlite3.Error:
            return _missing_source(
                "live15_verified_archive",
                ResearchSourceType.OWN_VERIFIED_ARCHIVE,
                TrustTier.H0,
                "ARCHIVE_MANIFEST_READ_ERROR",
            )
        finally:
            connection.close()

    def _official_manifest(self) -> ResearchSourceManifest:
        payload = _safe_json(self.project_root / "docs" / "hist003_acquisition_summary.json")
        counts = payload.get("counts") if isinstance(payload.get("counts"), Mapping) else {}
        window = payload.get("window") if isinstance(payload.get("window"), Mapping) else {}
        universe = payload.get("universe") if isinstance(payload.get("universe"), Mapping) else {}
        earliest = _parse_timestamp(window.get("start"))
        latest = _parse_timestamp(window.get("end"))
        declared_days = window.get("days")
        days = (
            tuple(
                (_utc(earliest) + timedelta(days=index)).date().isoformat()
                for index in range(int(declared_days))
            )
            if earliest is not None and isinstance(declared_days, int) and declared_days > 0
            else _day_range(earliest, latest)
        )
        detail_days = self._official_detail_days()
        return ResearchSourceManifest(
            KALSHI_OFFICIAL,
            ResearchSourceType.KALSHI_OFFICIAL_HISTORY,
            TrustTier.H1,
            "kalshi-sdk-v12",
            "hist003-v1",
            earliest,
            latest,
            days,
            days,
            tuple(sorted(str(item) for item in universe)),
            int(counts.get("markets", 0)) if isinstance(counts, Mapping) else 0,
            int(counts.get("trades", 0)) if isinstance(counts, Mapping) else 0,
            "official_completed_history_only",
            "VERIFIED_ARTIFACT" if payload else "UNAVAILABLE",
            "official Kalshi historical acquisition summary",
            _hash(payload or {"missing": True}),
            (
                "candles/trades are not full historical L2",
                "detail scope is bounded; see HIST-003 manifest",
            ),
            capability_days={
                "PATH_TERMINAL_DAYS": detail_days["PATH_TERMINAL_DAYS"],
                "TRADE_SEQUENCE_DAYS": detail_days["TRADE_SEQUENCE_DAYS"],
            },
        )

    def _official_detail_days(self) -> dict[str, tuple[str, ...]]:
        path = self.project_root / "data" / "research" / "hist003" / "hist003_official.sqlite3"
        connection = _readonly_connection(path)
        if connection is None:
            return {"PATH_TERMINAL_DAYS": (), "TRADE_SEQUENCE_DAYS": ()}
        try:
            trade_days = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT substr(created_time, 1, 10) FROM trades "
                    "WHERE created_time IS NOT NULL ORDER BY 1"
                )
                if row[0]
            )
            candle_days = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT date(end_period_ts, 'unixepoch') FROM candles "
                    "WHERE end_period_ts IS NOT NULL ORDER BY 1"
                )
                if row[0]
            )
            return {
                "PATH_TERMINAL_DAYS": tuple(sorted(set(trade_days) | set(candle_days))),
                "TRADE_SEQUENCE_DAYS": trade_days,
            }
        except sqlite3.Error:
            return {"PATH_TERMINAL_DAYS": (), "TRADE_SEQUENCE_DAYS": ()}
        finally:
            connection.close()

    def _depthfeed_manifest(self) -> ResearchSourceManifest:
        status = depthfeed_key_status(project_root=self.project_root)
        verification = (
            DEPTHFEED_INTEGRATION_READY_KEY_REQUIRED
            if status == DEPTHFEED_NOT_CONFIGURED
            else "DEPTHFEED_BASE_URL_REQUIRED"
            if not os.environ.get("DEPTHFEED_BASE_URL", "").strip()
            else "CONFIGURED_NO_ACQUISITION"
        )
        return ResearchSourceManifest(
            DEPTHFEED_KALSHI_L2,
            ResearchSourceType.DEPTHFEED_KALSHI_L2,
            TrustTier.H2,
            "DepthFeed",
            "depthfeed-l2-v1",
            None,
            None,
            (),
            (),
            ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"),
            0,
            0,
            "server_side_credentialed_as_of_only",
            verification,
            "optional historical L2 adapter",
            _hash({"status": status, "provider": DEPTHFEED_KALSHI_L2}),
            (
                "no bounded acquisition without configured credential",
                "ticks are partial until overlap semantics are verified",
            ),
            capability_days={
                "L2_SNAPSHOT_DAYS": (),
                "L2_DELTA_DAYS": (),
            },
        )

    def _holdout_metadata(self) -> FrozenHoldoutMetadata:
        # Never read Dataset v2 payload rows.  A missing metadata manifest fails closed by
        # exposing no development overlap rather than guessing reconstruction identities.
        manifest = _safe_json(
            self.project_root / "docs" / "model_vnext_dataset_v2_freeze_20260826_001.json"
        )
        dataset_id = str(manifest.get("dataset_id", "dataset-v2"))
        dataset_path = manifest.get("dataset_path")
        split_path = self.project_root / str(dataset_path or "") / "splits.json"
        splits = _safe_json(split_path)
        split_map = splits.get("splits") if isinstance(splits.get("splits"), Mapping) else {}
        test = split_map.get("test") if isinstance(split_map.get("test"), Mapping) else {}
        events = (
            test.get("events")
            if isinstance(test.get("events"), Sequence) and not isinstance(test.get("events"), str)
            else ()
        )
        # These are identity-only test event handles.  No Dataset v2 rows, labels, features,
        # predictions, metrics, or performance are opened by this authority.
        event_ids = tuple(sorted(str(item) for item in events if isinstance(item, (str, int))))
        return FrozenHoldoutMetadata.unrevealed(dataset_id, excluded_event_ids=event_ids)


def require_reproduction_only(*, reproduction_only: bool, entrypoint: str) -> None:
    """Guard legacy Dataset-artifact commands from masquerading as current research.

    Current factor/model selection must be built from a ResearchUniverseSnapshot.  These
    existing commands intentionally remain available only for immutable-artifact
    reproduction until a universe-backed runner is introduced.
    """

    if not reproduction_only:
        raise ValueError(
            f"{entrypoint} is a reproduction-only Dataset artifact command; "
            "current research requires ResearchUniverseSnapshot"
        )


def _missing_source(
    source_id: str, source_type: ResearchSourceType, tier: TrustTier, reason: str
) -> ResearchSourceManifest:
    return ResearchSourceManifest(
        source_id,
        source_type,
        tier,
        None,
        "research-source-v1",
        None,
        None,
        (),
        (),
        (),
        0,
        0,
        "unknown",
        reason,
        "read-only runtime metadata",
        _hash({"source": source_id, "reason": reason}),
        (reason,),
    )


def _with_source_coverage(snapshot: ResearchUniverseSnapshot) -> ResearchUniverseSnapshot:
    sources = tuple(
        item for item in snapshot.source_manifests if item.verification_state.startswith("VERIFIED")
    )
    earliest = min(
        (item.earliest_timestamp for item in sources if item.earliest_timestamp), default=None
    )
    latest = max((item.latest_timestamp for item in sources if item.latest_timestamp), default=None)
    # Aggregate manifests are coverage metadata, not normalized row streams.  Summing their
    # counts would double-count Recorder data that has moved to cold archive (or an H1 overlap).
    # Until a caller supplies normalized ResearchObservation identities, report only the
    # deterministic highest-tier development projection as the unified eligible count.
    countable = [
        item for item in sources if item.eligible_events > 0 and item.eligible_observations > 0
    ]
    primary = min(
        countable,
        key=lambda item: (_PRECEDENCE[item.trust_tier.value], item.source_id),
        default=None,
    )
    # Keep legacy aggregate fields tied to the highest-precedence countable source.  Never
    # union H0/H1/H2 windows into one misleading development-day total.
    primary_days = primary.utc_calendar_days if primary else ()
    primary_sessions = primary.market_session_days if primary else ()
    capability_days: dict[str, tuple[str, ...]] = {}
    for key in CAPABILITY_DAY_KEYS:
        capability_days[key] = tuple(
            sorted({day for item in sources for day in item.capability_days.get(key, ())})
        )
    payload = snapshot.to_public_dict() | {
        "aggregate_source_coverage": [item.to_public_dict() for item in sources]
    }
    content_hash = _hash(payload)
    return ResearchUniverseSnapshot(
        universe_id=f"research-universe-{content_hash[:20]}",
        content_hash=content_hash,
        cutoff_timestamp=snapshot.cutoff_timestamp,
        code_git_sha=snapshot.code_git_sha,
        freshness_policy=snapshot.freshness_policy,
        session_semantics_version=snapshot.session_semantics_version,
        source_manifests=snapshot.source_manifests,
        earliest_timestamp=earliest,
        latest_timestamp=latest,
        utc_calendar_days=primary_days,
        market_session_days=primary_sessions,
        eligible_development_days=primary_sessions,
        validation_days=snapshot.validation_days,
        assets=tuple(sorted({asset for item in sources for asset in item.assets})),
        eligible_events=primary.eligible_events if primary else 0,
        eligible_observations=primary.eligible_observations if primary else 0,
        deduplicated_observations=snapshot.deduplicated_observations,
        conflicting_observations=snapshot.conflicting_observations,
        quarantined_observations=snapshot.quarantined_observations,
        holdout_excluded_observations=snapshot.holdout_excluded_observations,
        selected_source_ids=(primary.source_id,) if primary else (),
        frozen_holdout=snapshot.frozen_holdout,
        depthfeed_status=snapshot.depthfeed_status,
        capability_days=capability_days,
    )

