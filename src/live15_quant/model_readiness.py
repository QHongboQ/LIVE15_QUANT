"""Bounded readiness gates for the offline Model vNext foundation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

MIN_SEQUENCE_DAYS = 30
MIN_SEQUENCE_EVENTS = 1_000
_VALID_STATUSES = {"READY", "PARTIAL", "BLOCKED", "RESEARCH_ONLY", "ARCHITECTURE_ONLY"}


class ReadinessStatus(StrEnum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    ARCHITECTURE_ONLY = "ARCHITECTURE_ONLY"


@dataclass(frozen=True, slots=True)
class DataReadinessEvidence:
    independent_utc_days: int
    independent_events: int
    approved_historical_representation: bool
    detail_coverage_complete: bool
    h0_orderbook: bool
    h2_snapshots: bool
    h2_ticks: bool
    holdout_accessed: bool = False

    def __post_init__(self) -> None:
        if min(self.independent_utc_days, self.independent_events) < 0:
            raise ValueError("readiness evidence counts cannot be negative")


@dataclass(frozen=True, slots=True)
class ReadinessDecision:
    status: ReadinessStatus
    decision: str
    blocked_by: tuple[str, ...] = ()
    full_training_status: ReadinessStatus | None = None


def _holdout_block(evidence: DataReadinessEvidence) -> ReadinessDecision | None:
    if evidence.holdout_accessed:
        return ReadinessDecision(
            ReadinessStatus.BLOCKED,
            "HOLDOUT_ACCESS_FORBIDDEN",
            ("UNREVEALED_FROZEN_DATASET_V2_HOLDOUT",),
        )
    return None


def evaluate_path_readiness(evidence: DataReadinessEvidence) -> ReadinessDecision:
    blocked = _holdout_block(evidence)
    if blocked:
        return blocked
    reasons: list[str] = []
    if not evidence.approved_historical_representation:
        reasons.append("APPROVED_HISTORICAL_REPRESENTATION_REQUIRED")
    if evidence.independent_utc_days < MIN_SEQUENCE_DAYS:
        reasons.append(f"SEQUENCE_DAYS_BELOW_{MIN_SEQUENCE_DAYS}")
    if evidence.independent_events < MIN_SEQUENCE_EVENTS:
        reasons.append(f"EVENTS_BELOW_{MIN_SEQUENCE_EVENTS}")
    if reasons:
        return ReadinessDecision(
            ReadinessStatus.BLOCKED, "SEQUENCE_EVIDENCE_INSUFFICIENT", tuple(reasons)
        )
    full_status = (
        ReadinessStatus.READY if evidence.detail_coverage_complete else ReadinessStatus.PARTIAL
    )
    return ReadinessDecision(
        ReadinessStatus.READY,
        "APPROVED_FOR_FOUNDATION",
        (() if full_status is ReadinessStatus.READY else ("BOUNDED_DETAIL_COVERAGE",)),
        full_status,
    )


def evaluate_microstructure_readiness(evidence: DataReadinessEvidence) -> ReadinessDecision:
    blocked = _holdout_block(evidence)
    if blocked:
        return blocked
    if not evidence.h0_orderbook and not evidence.h2_snapshots:
        return ReadinessDecision(
            ReadinessStatus.BLOCKED,
            "MICROSTRUCTURE_EVIDENCE_UNAVAILABLE",
            ("H0_OR_H2_ORDERBOOK_REQUIRED",),
        )
    if not evidence.h2_ticks:
        return ReadinessDecision(
            ReadinessStatus.PARTIAL,
            "SNAPSHOT_ONLY_MICROSTRUCTURE_EVIDENCE",
            ("H2_TICKS_UNAVAILABLE",),
        )
    return ReadinessDecision(ReadinessStatus.READY, "MICROSTRUCTURE_EVIDENCE_AVAILABLE")


def load_model_zoo_manifest(path: Path) -> dict[str, Any]:
    """Load and validate the small tracked model-zoo manifest."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("model-zoo manifest is missing or malformed") from error
    if not isinstance(payload, dict) or payload.get("foundation_version") != "1.0.0":
        raise ValueError("model-zoo foundation version is invalid")
    families = payload.get("families")
    if not isinstance(families, list) or not families:
        raise ValueError("model-zoo families are missing")
    required = {
        "family_id",
        "upstream_name",
        "role",
        "status",
        "approved_data_sources",
        "blocked_by",
        "notes",
    }
    seen: set[str] = set()
    for family in families:
        if not isinstance(family, dict) or not required <= set(family):
            raise ValueError("model-zoo family schema is invalid")
        family_id = family["family_id"]
        if not isinstance(family_id, str) or not family_id or family_id in seen:
            raise ValueError("model-zoo family identity is invalid")
        seen.add(family_id)
        if family["status"] not in _VALID_STATUSES:
            raise ValueError("model-zoo family status is invalid")
        sources = family["approved_data_sources"]
        if not isinstance(sources, list) or not sources:
            raise ValueError("model-zoo family sources are invalid")
        if any("holdout" in str(source).lower() for source in sources):
            raise ValueError("model-zoo family cannot allow holdout data")
    return payload
