"""Canonical, provenance-aware evidence reconciliation for model readiness.

Readiness code must consume :class:`CanonicalEvidenceSnapshot`, never a single sampled
artifact.  This module is intentionally offline and metadata-only: it does not read or mutate
Recorder, Dataset v2, holdout, Paper, Production, or model state.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = "canonical-evidence-v1"
H0 = "H0_LIVE_NATIVE"
H1 = "H1_KALSHI_OFFICIAL_HISTORY"
H2 = "H2_DEPTHFEED_RECORDED_L2"
CURRENT_TRAINABLE_POOL = "CURRENT_TRAINABLE_POOL"
FROZEN_DATASET = "FROZEN_DATASET"


class CoverageScope(StrEnum):
    FULL_SOURCE = "FULL_SOURCE"
    BOUNDED_WINDOW = "BOUNDED_WINDOW"
    STRATIFIED_SAMPLE = "STRATIFIED_SAMPLE"
    SAMPLED_SUBSET = "SAMPLED_SUBSET"
    FROZEN_DATASET = "FROZEN_DATASET"
    EXPERIMENT_CUTOFF = "EXPERIMENT_CUTOFF"


class InconsistencyState(StrEnum):
    TEMPORAL_COVERAGE_COLLAPSE = "TEMPORAL_COVERAGE_COLLAPSE"
    ASSET_COVERAGE_COLLAPSE = "ASSET_COVERAGE_COLLAPSE"
    ARTIFACT_SCOPE_MISMATCH = "ARTIFACT_SCOPE_MISMATCH"
    SOURCE_ARTIFACT_COUNT_MISMATCH = "SOURCE_ARTIFACT_COUNT_MISMATCH"
    EVIDENCE_RECONCILIATION_REQUIRED = "EVIDENCE_RECONCILIATION_REQUIRED"
    READINESS_EVIDENCE_INCONSISTENT = "READINESS_EVIDENCE_INCONSISTENT"
    EXPERIMENT_CUTOFF_VIOLATION = "EXPERIMENT_CUTOFF_VIOLATION"


class PreflightStatus(StrEnum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"


class EvidenceReconciliationError(ValueError):
    """Raised when an evidence or sampling contract is unsafe."""


_FORBIDDEN_SAMPLING = re.compile(
    r"(?:first\s*[- ]?n|first\s+\d+|api[- ]?order|storage[- ]?order)", re.IGNORECASE
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evidence timestamps must be timezone-aware")
    return value.astimezone(UTC)


def validate_sampling_policy(policy: str) -> None:
    """Reject API/storage-order first-N policies where temporal breadth matters."""

    if _FORBIDDEN_SAMPLING.search(policy):
        raise EvidenceReconciliationError(
            "first-N/API-order temporal sampling is prohibited for readiness evidence"
        )


def _canonical(value: object) -> object:
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    source_id: str
    provenance_tier: str
    coverage_scope: CoverageScope
    earliest_timestamp: datetime
    latest_timestamp: datetime
    independent_utc_days: int
    independent_events: int
    assets: tuple[str, ...]
    per_day_counts: Mapping[str, int]
    per_asset_counts: Mapping[str, int]
    row_count: int
    artifact_id: str
    cutoff: datetime
    sampling_policy: str
    capped: bool
    cap_size: int | None
    full_source: bool
    data_quality_status: str
    gap_quarantine_state: str
    sequence_availability: Mapping[str, Any]
    microstructure_availability: Mapping[str, Any]
    target_availability: Mapping[str, Any]
    source_independent_utc_days: int | None = None
    source_independent_events: int | None = None
    source_assets: tuple[str, ...] = ()
    coverage_days: tuple[str, ...] = ()
    holdout_accessed: bool = False

    def __post_init__(self) -> None:
        if not self.source_id or not self.provenance_tier or not self.artifact_id:
            raise ValueError("evidence identity is required")
        earliest = _utc(self.earliest_timestamp)
        latest = _utc(self.latest_timestamp)
        cutoff = _utc(self.cutoff)
        if earliest > latest or latest > cutoff:
            raise ValueError("evidence timestamps exceed its source cutoff")
        if min(self.independent_utc_days, self.independent_events, self.row_count) < 0:
            raise ValueError("evidence counts cannot be negative")
        if self.capped and (self.cap_size is None or self.cap_size <= 0):
            raise ValueError("a capped evidence record requires a positive cap_size")
        if self.coverage_scope is CoverageScope.FULL_SOURCE and self.capped:
            raise ValueError("FULL_SOURCE evidence cannot be capped")
        if self.source_independent_utc_days is not None and (
            self.source_independent_utc_days < self.independent_utc_days
        ):
            raise ValueError("source coverage cannot be smaller than artifact coverage")
        if self.source_independent_events is not None and (
            self.source_independent_events < self.independent_events
        ):
            raise ValueError("source event coverage cannot be smaller than artifact coverage")
        validate_sampling_policy(self.sampling_policy)
        if self.holdout_accessed and self.provenance_tier != FROZEN_DATASET:
            raise ValueError("holdout_accessed is only valid for frozen dataset records")

    @property
    def path_days(self) -> int:
        return max(0, int(self.sequence_availability.get("days", 0)))

    @property
    def snapshot_days(self) -> int:
        value = self.microstructure_availability.get("snapshot", {})
        return max(0, int(value.get("days", 0))) if isinstance(value, Mapping) else 0

    @property
    def delta_days(self) -> int:
        value = self.microstructure_availability.get("delta", {})
        return max(0, int(value.get("days", 0))) if isinstance(value, Mapping) else 0

    @property
    def snapshot_sequence_days(self) -> int:
        value = self.microstructure_availability.get("snapshot_sequence", {})
        return max(0, int(value.get("days", 0))) if isinstance(value, Mapping) else 0

    @property
    def delta_sequence_days(self) -> int:
        value = self.microstructure_availability.get("delta_sequence", {})
        return max(0, int(value.get("days", 0))) if isinstance(value, Mapping) else 0

    @property
    def microstructure_training_ready_days(self) -> int:
        value = self.microstructure_availability.get("training_ready", {})
        return max(0, int(value.get("days", 0))) if isinstance(value, Mapping) else 0

    @property
    def target_days(self) -> int:
        return max(0, int(self.target_availability.get("days", 0)))

    @property
    def has_targets(self) -> bool:
        return bool(self.target_availability.get("available"))

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "provenance_tier": self.provenance_tier,
            "coverage_scope": self.coverage_scope.value,
            "earliest_timestamp": _utc(self.earliest_timestamp).isoformat(),
            "latest_timestamp": _utc(self.latest_timestamp).isoformat(),
            "independent_utc_days": self.independent_utc_days,
            "independent_events": self.independent_events,
            "assets": list(self.assets),
            "per_day_counts": dict(sorted(self.per_day_counts.items())),
            "per_asset_counts": dict(sorted(self.per_asset_counts.items())),
            "row_count": self.row_count,
            "artifact_id": self.artifact_id,
            "cutoff": _utc(self.cutoff).isoformat(),
            "sampling_policy": self.sampling_policy,
            "capped": self.capped,
            "cap_size": self.cap_size,
            "full_source": self.full_source,
            "data_quality_status": self.data_quality_status,
            "gap_quarantine_state": self.gap_quarantine_state,
            "sequence_availability": dict(self.sequence_availability),
            "microstructure_availability": dict(self.microstructure_availability),
            "target_availability": dict(self.target_availability),
            "source_independent_utc_days": self.source_independent_utc_days,
            "source_independent_events": self.source_independent_events,
            "source_assets": list(self.source_assets),
            "coverage_days": list(self.coverage_days),
            "holdout_accessed": self.holdout_accessed,
        }


@dataclass(frozen=True, slots=True)
class CanonicalReadiness:
    status: PreflightStatus
    snapshot_id: str
    reasons: tuple[str, ...]
    readiness_days: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class TrainingPreflightResult:
    status: PreflightStatus
    model_family: str
    snapshot_id: str
    training_sources: tuple[EvidenceRecord, ...]
    reasons: tuple[str, ...]
    source_cutoff: datetime
    h0_priority_source: str = H0


@dataclass(frozen=True, slots=True)
class CanonicalEvidenceSnapshot:
    schema_version: str
    snapshot_id: str
    experiment_id: str
    experiment_cutoff: datetime
    records: tuple[EvidenceRecord, ...]
    frozen_datasets: tuple[Mapping[str, object], ...]
    inconsistency_states: tuple[str, ...]
    generated_at: datetime

    @property
    def h0_priority_source(self) -> str:
        return H0

    def _tier_records(self, tier: str) -> tuple[EvidenceRecord, ...]:
        return tuple(record for record in self.records if record.provenance_tier == tier)

    @staticmethod
    def _aggregate_days(records: Sequence[EvidenceRecord], attribute: str) -> int:
        values = [int(getattr(record, attribute)) for record in records]
        # Coverage days describe the record's overall scope, not every capability it
        # carries.  A source may cover six days while having no path/snapshot/delta
        # evidence at all; those days must not be promoted into a capability readiness
        # count.  Fall back to the explicit capability count when no capability-specific
        # day list is available.
        coverage_days = {
            day
            for record in records
            if int(getattr(record, attribute)) > 0
            for day in record.coverage_days
        }
        return len(coverage_days) if coverage_days else max(values, default=0)

    def readiness_days(self) -> dict[str, int]:
        h0 = self._tier_records(H0)
        h1 = self._tier_records(H1)
        h2 = self._tier_records(H2)
        path = {
            "h0_path_days": self._aggregate_days(h0, "path_days"),
            "h1_path_days": self._aggregate_days(h1, "path_days"),
            "h2_path_days": self._aggregate_days(h2, "path_days"),
        }
        snapshots = {
            "h0_snapshot_days": self._aggregate_days(h0, "snapshot_days"),
            "h1_snapshot_days": self._aggregate_days(h1, "snapshot_days"),
            "h2_snapshot_days": self._aggregate_days(h2, "snapshot_days"),
        }
        deltas = {
            "h0_delta_days": self._aggregate_days(h0, "delta_days"),
            "h1_delta_days": self._aggregate_days(h1, "delta_days"),
            "h2_delta_days": self._aggregate_days(h2, "delta_days"),
        }
        snapshot_sequences = {
            "h0_snapshot_sequence_days": self._aggregate_days(h0, "snapshot_sequence_days"),
            "h1_snapshot_sequence_days": self._aggregate_days(h1, "snapshot_sequence_days"),
            "h2_snapshot_sequence_days": self._aggregate_days(h2, "snapshot_sequence_days"),
        }
        delta_sequences = {
            "h0_delta_sequence_days": self._aggregate_days(h0, "delta_sequence_days"),
            "h1_delta_sequence_days": self._aggregate_days(h1, "delta_sequence_days"),
            "h2_delta_sequence_days": self._aggregate_days(h2, "delta_sequence_days"),
        }
        training_ready = {
            "h0_microstructure_training_ready_days": self._aggregate_days(
                h0, "microstructure_training_ready_days"
            ),
            "h1_microstructure_training_ready_days": self._aggregate_days(
                h1, "microstructure_training_ready_days"
            ),
            "h2_microstructure_training_ready_days": self._aggregate_days(
                h2, "microstructure_training_ready_days"
            ),
        }
        all_path_days = {
            day for record in self.records if record.path_days > 0 for day in record.coverage_days
        }
        all_snapshot_days = {
            day
            for record in self.records
            if record.snapshot_days > 0
            for day in record.coverage_days
        }
        all_delta_days = {
            day for record in self.records if record.delta_days > 0 for day in record.coverage_days
        }
        path["combined_path_days"] = len(all_path_days) or max(path.values(), default=0)
        snapshots["combined_snapshot_days"] = len(all_snapshot_days) or max(
            snapshots.values(), default=0
        )
        deltas["combined_delta_days"] = len(all_delta_days) or max(deltas.values(), default=0)
        snapshot_sequences["combined_snapshot_sequence_days"] = max(
            snapshot_sequences.values(), default=0
        )
        delta_sequences["combined_delta_sequence_days"] = max(delta_sequences.values(), default=0)
        training_ready["combined_microstructure_training_ready_days"] = max(
            training_ready.values(), default=0
        )
        return {
            **path,
            **snapshots,
            **deltas,
            **snapshot_sequences,
            **delta_sequences,
            **training_ready,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "experiment_id": self.experiment_id,
            "experiment_cutoff": _utc(self.experiment_cutoff).isoformat(),
            "records": [record.to_dict() for record in self.records],
            "frozen_datasets": [_canonical(item) for item in self.frozen_datasets],
            "inconsistency_states": list(self.inconsistency_states),
            "readiness_days": self.readiness_days(),
            "h0_priority_source": self.h0_priority_source,
            "generated_at": _utc(self.generated_at).isoformat(),
        }


def _detect_inconsistencies(records: Sequence[EvidenceRecord]) -> tuple[str, ...]:
    states: set[str] = set()
    for record in records:
        if record.coverage_scope in {
            CoverageScope.STRATIFIED_SAMPLE,
            CoverageScope.SAMPLED_SUBSET,
        }:
            source_days = record.source_independent_utc_days
            if source_days is not None and source_days > max(1, record.independent_utc_days) * 2:
                explicit_stratification = (
                    record.coverage_scope is CoverageScope.STRATIFIED_SAMPLE
                    and bool(record.sampling_policy)
                )
                if not explicit_stratification:
                    states.update(
                        {
                            InconsistencyState.TEMPORAL_COVERAGE_COLLAPSE.value,
                            InconsistencyState.ARTIFACT_SCOPE_MISMATCH.value,
                            InconsistencyState.EVIDENCE_RECONCILIATION_REQUIRED.value,
                        }
                    )
            if record.source_assets and len(record.source_assets) > max(1, len(record.assets)) * 2:
                states.update(
                    {
                        InconsistencyState.ASSET_COVERAGE_COLLAPSE.value,
                        InconsistencyState.ARTIFACT_SCOPE_MISMATCH.value,
                        InconsistencyState.EVIDENCE_RECONCILIATION_REQUIRED.value,
                    }
                )
            if (
                record.source_independent_events is not None
                and record.source_independent_events > max(1, record.independent_events) * 2
            ):
                states.update(
                    {
                        InconsistencyState.SOURCE_ARTIFACT_COUNT_MISMATCH.value,
                        InconsistencyState.EVIDENCE_RECONCILIATION_REQUIRED.value,
                    }
                )
            if record.capped and record.coverage_scope is CoverageScope.SAMPLED_SUBSET:
                states.add(InconsistencyState.SOURCE_ARTIFACT_COUNT_MISMATCH.value)
        elif record.capped:
            states.add(InconsistencyState.ARTIFACT_SCOPE_MISMATCH.value)
    if states:
        states.add(InconsistencyState.READINESS_EVIDENCE_INCONSISTENT.value)
    return tuple(sorted(states))


def build_canonical_evidence_snapshot(
    *,
    experiment_id: str,
    experiment_cutoff: datetime,
    records: Sequence[EvidenceRecord],
    frozen_datasets: Sequence[Mapping[str, object]] = (),
) -> CanonicalEvidenceSnapshot:
    """Build the only supported input to future readiness and training preflight."""

    if not experiment_id:
        raise ValueError("experiment_id is required")
    cutoff = _utc(experiment_cutoff)
    if not records:
        raise ValueError("at least one evidence record is required")
    for record in records:
        if _utc(record.latest_timestamp) > cutoff or _utc(record.cutoff) > cutoff:
            raise ValueError("evidence record exceeds experiment cutoff")
    ordered = tuple(
        sorted(
            records,
            key=lambda item: (item.provenance_tier, item.source_id, item.artifact_id),
        )
    )
    frozen = tuple(
        sorted(
            (dict(item) for item in frozen_datasets),
            key=lambda item: str(item.get("dataset_id", "")),
        )
    )
    states = _detect_inconsistencies(ordered)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "experiment_cutoff": cutoff.isoformat(),
        "records": [record.to_dict() for record in ordered],
        "frozen_datasets": [_canonical(item) for item in frozen],
        "inconsistency_states": list(states),
    }
    snapshot_id = (
        "ces-"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
    )
    return CanonicalEvidenceSnapshot(
        schema_version=SCHEMA_VERSION,
        snapshot_id=snapshot_id,
        experiment_id=experiment_id,
        experiment_cutoff=cutoff,
        records=ordered,
        frozen_datasets=frozen,
        inconsistency_states=states,
        generated_at=datetime.now(UTC),
    )


def evaluate_canonical_readiness(snapshot: CanonicalEvidenceSnapshot) -> CanonicalReadiness:
    if not isinstance(snapshot, CanonicalEvidenceSnapshot):
        raise TypeError("evaluate_canonical_readiness requires CanonicalEvidenceSnapshot")
    reasons = list(snapshot.inconsistency_states)
    if any(record.holdout_accessed for record in snapshot.records):
        reasons.append("HOLDOUT_ACCESS_FORBIDDEN")
    days = snapshot.readiness_days()
    if reasons:
        return CanonicalReadiness(
            PreflightStatus.BLOCKED,
            snapshot.snapshot_id,
            tuple(dict.fromkeys(reasons)),
            days,
        )
    has_path = days["combined_path_days"] > 0
    has_snapshot = days["combined_snapshot_days"] > 0
    status = PreflightStatus.READY if has_path or has_snapshot else PreflightStatus.PARTIAL
    if not has_path and not has_snapshot:
        return CanonicalReadiness(
            PreflightStatus.PARTIAL,
            snapshot.snapshot_id,
            ("NO_PATH_OR_SNAPSHOT_EVIDENCE",),
            days,
        )
    return CanonicalReadiness(status, snapshot.snapshot_id, (), days)


def training_preflight(
    snapshot: CanonicalEvidenceSnapshot,
    *,
    model_family: str,
    source_cutoff: datetime | None = None,
) -> TrainingPreflightResult:
    """Require canonical reconciliation before any future model training request."""

    if not isinstance(snapshot, CanonicalEvidenceSnapshot):
        raise TypeError("training_preflight requires CanonicalEvidenceSnapshot")
    cutoff = _utc(source_cutoff or snapshot.experiment_cutoff)
    if cutoff > snapshot.experiment_cutoff:
        return TrainingPreflightResult(
            PreflightStatus.BLOCKED,
            model_family,
            snapshot.snapshot_id,
            (),
            ("EXPERIMENT_CUTOFF_VIOLATION",),
            cutoff,
        )
    if any(record.holdout_accessed for record in snapshot.records):
        return TrainingPreflightResult(
            PreflightStatus.BLOCKED,
            model_family,
            snapshot.snapshot_id,
            (),
            ("HOLDOUT_ACCESS_FORBIDDEN",),
            cutoff,
        )
    if InconsistencyState.EVIDENCE_RECONCILIATION_REQUIRED.value in snapshot.inconsistency_states:
        return TrainingPreflightResult(
            PreflightStatus.BLOCKED,
            model_family,
            snapshot.snapshot_id,
            (),
            snapshot.inconsistency_states,
            cutoff,
        )
    days = snapshot.readiness_days()
    family = model_family.casefold()
    if family in {"path_expert", "path"}:
        sources = tuple(record for record in snapshot.records if record.path_days > 0)
        requirement = "PATH_EVIDENCE_REQUIRED"
        if not sources:
            return TrainingPreflightResult(
                PreflightStatus.BLOCKED,
                model_family,
                snapshot.snapshot_id,
                (),
                (requirement,),
                cutoff,
            )
    elif family in {"terminal_expert", "terminal"}:
        sources = tuple(
            record
            for record in snapshot.records
            if record.target_days > 0 or (record.has_targets and record.independent_events > 0)
        )
        if not sources:
            return TrainingPreflightResult(
                PreflightStatus.BLOCKED,
                model_family,
                snapshot.snapshot_id,
                (),
                ("TERMINAL_EVIDENCE_REQUIRED",),
                cutoff,
            )
    elif family in {"microstructure_snapshot_expert", "microstructure_snapshot"}:
        sources = tuple(record for record in snapshot.records if record.snapshot_days > 0)
        if not sources:
            return TrainingPreflightResult(
                PreflightStatus.BLOCKED,
                model_family,
                snapshot.snapshot_id,
                (),
                ("SNAPSHOT_EVIDENCE_REQUIRED",),
                cutoff,
            )
    elif family in {"event_delta_expert", "event_delta"}:
        sources = tuple(record for record in snapshot.records if record.delta_days > 0)
        if not sources:
            return TrainingPreflightResult(
                PreflightStatus.BLOCKED,
                model_family,
                snapshot.snapshot_id,
                (),
                ("DELTA_EVIDENCE_REQUIRED",),
                cutoff,
            )
    elif family in {"mlplob", "microstructure_mlp"}:
        sources = tuple(
            record
            for record in snapshot.records
            if record.snapshot_days > 0 and record.microstructure_training_ready_days > 0
        )
        if not sources:
            return TrainingPreflightResult(
                PreflightStatus.BLOCKED,
                model_family,
                snapshot.snapshot_id,
                (),
                ("MATERIALIZED_SNAPSHOT_EVIDENCE_REQUIRED",),
                cutoff,
            )
    elif family in {"deeplob", "deep_lob"}:
        sources = tuple(record for record in snapshot.records if record.snapshot_sequence_days > 0)
        if not sources:
            return TrainingPreflightResult(
                PreflightStatus.BLOCKED,
                model_family,
                snapshot.snapshot_id,
                (),
                ("SNAPSHOT_SEQUENCE_EVIDENCE_REQUIRED",),
                cutoff,
            )
    elif family == "tlob":
        sources = tuple(record for record in snapshot.records if record.delta_sequence_days > 0)
        if not sources:
            return TrainingPreflightResult(
                PreflightStatus.BLOCKED,
                model_family,
                snapshot.snapshot_id,
                (),
                ("H2_DELTA_SEQUENCE_UNAVAILABLE",),
                cutoff,
            )
    else:
        raise ValueError(f"unsupported model family: {model_family}")
    reasons: list[str] = []
    if days["h0_path_days"] == 0 and family.startswith("path"):
        reasons.append("H0_PRIORITY_VALIDATION_REQUIRED")
    return TrainingPreflightResult(
        PreflightStatus.READY if not reasons else PreflightStatus.PARTIAL,
        model_family,
        snapshot.snapshot_id,
        sources,
        tuple(reasons),
        cutoff,
    )
