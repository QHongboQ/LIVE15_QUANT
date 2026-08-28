"""Bounded, read-only research materialization from verified WS archive chunks.

This module deliberately reuses the retention archive decoder and replay state.  It is
not an archive writer, retention worker, feature runner, or training input shortcut.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .kalshi_ws import KalshiWsEventKind
from .records import KalshiWsOrderBookEventRecord
from .ws_archive import WsArchiveError, decode_archive_chunk
from .ws_retention import ArchiveState, WsRetentionError, _ReplayState

if TYPE_CHECKING:
    from .canonical_evidence import EvidenceRecord
    from .research_data_authority import ResearchObservation, ResearchSourceManifest


_RESEARCH_STATES = frozenset(
    {
        ArchiveState.COMMITTED.value,
        ArchiveState.PURGE_ELIGIBLE.value,
        ArchiveState.PURGED.value,
    }
)
MAXIMUM_ARCHIVE_RESEARCH_CHUNKS = 4


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("archive research timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _hash(value: object) -> str:
    def canonical(item: object) -> object:
        if isinstance(item, datetime):
            return _utc(item).isoformat()
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, dict):
            return {str(key): canonical(value) for key, value in sorted(item.items())}
        if isinstance(item, (tuple, list)):
            return [canonical(value) for value in item]
        return item

    return hashlib.sha256(
        json.dumps(canonical(value), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _utc(parsed)


def _asset(ticker: str) -> str:
    prefix = ticker.split("15M-", 1)[0]
    return prefix.removeprefix("KX") or prefix


@dataclass(frozen=True, slots=True)
class ArchiveResearchQuery:
    """An explicit, bounded archive range and point-in-time materialization cutoff."""

    first_event_id: int
    last_event_id: int
    as_of_timestamp: datetime
    maximum_chunks: int = 4

    def __post_init__(self) -> None:
        if self.first_event_id <= 0 or self.last_event_id < self.first_event_id:
            raise ValueError("archive research query event range is invalid")
        _utc(self.as_of_timestamp)


@dataclass(frozen=True, slots=True)
class ArchiveBookMaterialization:
    source_id: str
    archive_chunk_id: str
    event_id: int
    sequence: int
    connection_id: str
    subscription_id: int
    ticker: str
    market_id: str
    source_timestamp: datetime
    received_timestamp: datetime
    materialization_timestamp: datetime
    as_of_timestamp: datetime
    replay_baseline_hash: str
    replay_state_hash: str
    event_kind: str
    yes_bid_depth: Decimal
    no_bid_depth: Decimal

    def research_observation(self) -> ResearchObservation:
        """Expose the materialization as a provenance-bearing RDA observation only."""

        from .research_data_authority import ResearchObservation, ResearchSourceType, TrustTier

        payload = {
            "archive_chunk_id": self.archive_chunk_id,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "ticker": self.ticker,
            "market_id": self.market_id,
            "replay_baseline_hash": self.replay_baseline_hash,
            "replay_state_hash": self.replay_state_hash,
            "yes_bid_depth": self.yes_bid_depth,
            "no_bid_depth": self.no_bid_depth,
        }
        content_hash = _hash(payload)
        return ResearchObservation(
            source_id=self.source_id,
            source_type=ResearchSourceType.OWN_VERIFIED_ARCHIVE,
            trust_tier=TrustTier.H0,
            event_id=f"archive-event-{self.event_id}",
            observation_id=f"archive-book-{self.event_id}-{self.replay_state_hash[:12]}",
            equivalence_key=f"archive-book:{self.market_id}:{self.event_id}",
            market_id=self.market_id,
            asset=_asset(self.ticker),
            source_timestamp=self.source_timestamp,
            received_timestamp=self.received_timestamp,
            utc_calendar_day=self.source_timestamp.date().isoformat(),
            market_session_day=self.source_timestamp.date().isoformat(),
            content_hash=content_hash,
            value_hash=_hash((self.yes_bid_depth, self.no_bid_depth, self.replay_state_hash)),
            quality_class="REPLAY_VERIFIED_ARCHIVE_BOOK",
        )


@dataclass(frozen=True, slots=True)
class ArchiveResearchSelection:
    available: bool
    reason: str | None
    query: ArchiveResearchQuery
    source_identity: str
    chunk_ids: tuple[str, ...] = ()
    materializations: tuple[ArchiveBookMaterialization, ...] = ()

    def source_manifest(self) -> ResearchSourceManifest:
        from .research_data_authority import ResearchSourceManifest, ResearchSourceType, TrustTier

        timestamps = [item.source_timestamp for item in self.materializations]
        observations = [item.research_observation() for item in self.materializations]
        days = tuple(sorted({item.utc_calendar_day for item in observations}))
        assets = tuple(sorted({item.asset for item in observations}))
        return ResearchSourceManifest(
            source_id="live15_verified_archive",
            source_type=ResearchSourceType.OWN_VERIFIED_ARCHIVE,
            trust_tier=TrustTier.H0,
            provider_version="ws-archive",
            schema_version="archive-research-v1",
            earliest_timestamp=min(timestamps) if timestamps else None,
            latest_timestamp=max(timestamps) if timestamps else None,
            utc_calendar_days=days,
            market_session_days=days,
            assets=assets,
            eligible_events=len({item.event_id for item in self.materializations}),
            eligible_observations=len(self.materializations),
            availability_semantics="explicit_bounded_as_of_replay_materialization",
            verification_state="VERIFIED_REPLAYED_MATERIALIZATION"
            if self.available
            else "UNAVAILABLE_RESEARCH_SELECTION",
            provenance="LIVE15 verified COLD archive decoded and causally replayed read-only",
            content_identity=self.source_identity,
            limitations=(
                "explicit bounded query only",
                "quarantined, failed, waiting, and unverified chunks are excluded",
                "not a training runner input",
            ),
            capability_days={"LIVE_NATIVE_DAYS": days},
            coverage_status={
                "metadata": "VERIFIED_METADATA",
                "replay": "REPLAY_VERIFIED",
                "materialization": "EXPLICIT_BOUNDED_READ_ONLY",
                "selection": "AVAILABLE" if self.available else "UNAVAILABLE",
            },
        )

    def canonical_evidence_record(self) -> EvidenceRecord:
        from .canonical_evidence import H0, CoverageScope, EvidenceRecord

        if not self.available or not self.materializations:
            raise ValueError("unavailable archive selection cannot produce canonical evidence")
        observations = tuple(item.research_observation() for item in self.materializations)
        days = tuple(sorted({item.utc_calendar_day for item in observations}))
        assets = tuple(sorted({item.asset for item in observations}))
        per_day = {day: sum(item.utc_calendar_day == day for item in observations) for day in days}
        per_asset = {asset: sum(item.asset == asset for item in observations) for asset in assets}
        snapshots = {
            item.source_timestamp.date().isoformat()
            for item in self.materializations
            if item.event_kind == KalshiWsEventKind.SNAPSHOT.value
        }
        deltas = {
            item.source_timestamp.date().isoformat()
            for item in self.materializations
            if item.event_kind == KalshiWsEventKind.DELTA.value
        }
        earliest = min(item.source_timestamp for item in self.materializations)
        latest = max(item.source_timestamp for item in self.materializations)
        return EvidenceRecord(
            source_id="live15_verified_archive",
            provenance_tier=H0,
            coverage_scope=CoverageScope.BOUNDED_WINDOW,
            earliest_timestamp=earliest,
            latest_timestamp=latest,
            independent_utc_days=len(days),
            independent_events=len({item.event_id for item in self.materializations}),
            assets=assets,
            per_day_counts=per_day,
            per_asset_counts=per_asset,
            row_count=len(self.materializations),
            artifact_id=self.source_identity,
            cutoff=_utc(self.query.as_of_timestamp),
            sampling_policy="explicit bounded manifest event range",
            capped=True,
            cap_size=self.query.maximum_chunks,
            full_source=False,
            data_quality_status="REPLAY_VERIFIED",
            gap_quarantine_state="QUARANTINED_RANGES_EXCLUDED",
            sequence_availability={"available": True, "days": len(days)},
            microstructure_availability={
                "snapshot": {"available": bool(snapshots), "days": len(snapshots)},
                "delta": {"available": bool(deltas), "days": len(deltas)},
            },
            target_availability={"available": False, "days": 0},
            source_independent_utc_days=len(days),
            source_independent_events=len({item.event_id for item in self.materializations}),
            source_assets=assets,
            coverage_days=days,
            holdout_accessed=False,
        )


class ArchiveResearchSourceAdapter:
    """Read-only, bounded access seam for archived replay-verified order-book state."""

    def __init__(self, archive_root: Path, manifest_path: Path) -> None:
        self.archive_root = archive_root.resolve()
        self.manifest_path = manifest_path.resolve()

    def materialize(self, query: ArchiveResearchQuery) -> ArchiveResearchSelection:
        source_identity = _hash(
            {
                "schema": "archive-research-v1",
                "first_event_id": query.first_event_id,
                "last_event_id": query.last_event_id,
                "as_of": _utc(query.as_of_timestamp),
                "maximum_chunks": query.maximum_chunks,
            }
        )
        identity = f"archive-research-{source_identity[:32]}"
        if query.maximum_chunks <= 0:
            return ArchiveResearchSelection(
                False, "ARCHIVE_QUERY_MAXIMUM_CHUNKS_INVALID", query, identity
            )
        if query.maximum_chunks > MAXIMUM_ARCHIVE_RESEARCH_CHUNKS:
            return ArchiveResearchSelection(
                False, "ARCHIVE_QUERY_MAXIMUM_CHUNKS_HARD_LIMIT", query, identity
            )
        if not self.manifest_path.is_file() or not self.archive_root.is_dir():
            return ArchiveResearchSelection(
                False, "ARCHIVE_RESEARCH_SOURCE_UNAVAILABLE", query, identity
            )
        try:
            chunks = self._chunks(query)
        except sqlite3.Error:
            return ArchiveResearchSelection(False, "ARCHIVE_MANIFEST_READ_ERROR", query, identity)
        if not chunks:
            return ArchiveResearchSelection(
                False, "ARCHIVE_QUERY_RANGE_UNAVAILABLE", query, identity
            )
        if len(chunks) > query.maximum_chunks:
            return ArchiveResearchSelection(
                False, "ARCHIVE_QUERY_MAXIMUM_CHUNKS_EXCEEDED", query, identity
            )
        if not self._covers(chunks, query):
            return ArchiveResearchSelection(
                False, "ARCHIVE_QUERY_RANGE_NONCONTIGUOUS", query, identity
            )
        if any(str(chunk["state"]) not in _RESEARCH_STATES for chunk in chunks):
            return ArchiveResearchSelection(
                False, "ARCHIVE_CHUNK_NOT_RESEARCH_ELIGIBLE", query, identity
            )
        try:
            materializations, provenance = self._replay(
                chunks, query, materialized_at=datetime.now(UTC)
            )
        except (OSError, WsRetentionError, _Unavailable) as error:
            reason = error.reason if isinstance(error, _Unavailable) else "ARCHIVE_REPLAY_INVALID"
            return ArchiveResearchSelection(False, reason, query, identity)
        source_identity = f"archive-research-{_hash(provenance)[:32]}"
        return ArchiveResearchSelection(
            True,
            None,
            query,
            source_identity,
            tuple(str(chunk["chunk_id"]) for chunk in chunks),
            tuple(materializations),
        )

    def _chunks(self, query: ArchiveResearchQuery) -> tuple[sqlite3.Row, ...]:
        connection = sqlite3.connect(f"file:{self.manifest_path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            return tuple(
                connection.execute(
                    """SELECT * FROM ws_retention_chunks
                    WHERE NOT(last_event_id<? OR first_event_id>?)
                    ORDER BY first_event_id""",
                    (query.first_event_id, query.last_event_id),
                )
            )
        finally:
            connection.close()

    @staticmethod
    def _covers(chunks: tuple[sqlite3.Row, ...], query: ArchiveResearchQuery) -> bool:
        return (
            int(chunks[0]["first_event_id"]) <= query.first_event_id
            and int(chunks[-1]["last_event_id"]) >= query.last_event_id
            and all(
                int(right["first_event_id"]) == int(left["last_event_id"]) + 1
                for left, right in pairwise(chunks)
            )
        )

    def _replay(
        self,
        chunks: tuple[sqlite3.Row, ...],
        query: ArchiveResearchQuery,
        *,
        materialized_at: datetime,
    ) -> tuple[list[ArchiveBookMaterialization], tuple[object, ...]]:
        decoded = tuple((chunk, self._decode_chunk(chunk)) for chunk in chunks)
        state = self._baseline_before(decoded[0][0], decoded[0][1][0][0], query)
        as_of_state = _ReplayState.from_json(state.as_json())
        baseline_hash = state.digest()
        materializations: list[ArchiveBookMaterialization] = []
        provenance: list[object] = []
        seen_future = False
        for chunk, (records, header) in decoded:
            for record in records:
                state.apply(record)
                source_time = record.source_timestamp
                event_time = source_time or record.socket_received_timestamp
                in_as_of = _utc(record.socket_received_timestamp) <= _utc(
                    query.as_of_timestamp
                ) and (source_time is None or _utc(source_time) <= _utc(query.as_of_timestamp))
                if seen_future and in_as_of:
                    raise _Unavailable("ARCHIVE_AS_OF_TIMESTAMP_ORDER_INVALID")
                seen_future = seen_future or not in_as_of
                if not in_as_of:
                    continue
                as_of_state.apply(record)
                if (
                    record.row_id < query.first_event_id
                    or record.row_id > query.last_event_id
                    or record.ticker is None
                    or record.market_id is None
                    or record.event_kind
                    not in {KalshiWsEventKind.SNAPSHOT, KalshiWsEventKind.DELTA}
                ):
                    continue
                book = as_of_state.books.get(record.ticker)
                if (
                    book is None
                    or book.market_id != record.market_id
                    or not as_of_state.synchronized
                ):
                    raise _Unavailable("ARCHIVE_REPLAY_BASELINE_UNUSABLE")
                materializations.append(
                    ArchiveBookMaterialization(
                        source_id="live15_verified_archive",
                        archive_chunk_id=str(chunk["chunk_id"]),
                        event_id=record.row_id,
                        sequence=record.sequence,
                        connection_id=record.connection_id,
                        subscription_id=record.subscription_id,
                        ticker=record.ticker,
                        market_id=record.market_id,
                        source_timestamp=_utc(event_time),
                        received_timestamp=_utc(record.socket_received_timestamp),
                        materialization_timestamp=_utc(materialized_at),
                        as_of_timestamp=_utc(query.as_of_timestamp),
                        replay_baseline_hash=baseline_hash,
                        replay_state_hash=as_of_state.digest(),
                        event_kind=record.event_kind.value,
                        yes_bid_depth=sum(book.yes.values(), Decimal(0)),
                        no_bid_depth=sum(book.no.values(), Decimal(0)),
                    )
                )
            expected = chunk["archive_replay_hash"]
            if (
                not expected
                or state.digest() != str(expected)
                or state.digest() != str(chunk["source_replay_hash"])
            ):
                raise _Unavailable("ARCHIVE_REPLAY_HASH_MISMATCH")
            if str(header.get("checksum_sha256")) != str(chunk["logical_checksum"]):
                raise _Unavailable("ARCHIVE_LOGICAL_CHECKSUM_MISMATCH")
            provenance.append(
                (
                    chunk["chunk_id"],
                    chunk["logical_checksum"],
                    chunk["file_checksum"],
                    chunk["archive_replay_hash"],
                    chunk["source_replay_hash"],
                )
            )
        if not materializations:
            raise _Unavailable("ARCHIVE_QUERY_NO_AS_OF_BOOK_MATERIALIZATIONS")
        return materializations, (
            query.first_event_id,
            query.last_event_id,
            _utc(query.as_of_timestamp),
            query.maximum_chunks,
            tuple(provenance),
        )

    def _baseline_before(
        self,
        first: sqlite3.Row,
        first_record: KalshiWsOrderBookEventRecord,
        query: ArchiveResearchQuery,
    ) -> _ReplayState:
        connection = sqlite3.connect(f"file:{self.manifest_path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """SELECT * FROM ws_retention_chunks WHERE last_event_id<?
                ORDER BY last_event_id DESC LIMIT 1""",
                (int(first["first_event_id"]),),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return _ReplayState.empty()
        if str(row["state"]) not in _RESEARCH_STATES:
            return _ReplayState.empty()
        if int(row["last_event_id"]) != int(first["first_event_id"]) - 1:
            return _ReplayState.empty()
        last_received = _timestamp(row["last_received_timestamp"])
        try:
            state = _ReplayState.from_json(row["end_replay_state"])
        except (KeyError, TypeError, ValueError, WsRetentionError) as error:
            raise _Unavailable("ARCHIVE_REPLAY_BASELINE_UNUSABLE") from error
        if (state.connection_id, state.subscription_id) != (
            first_record.connection_id,
            first_record.subscription_id,
        ):
            return _ReplayState.empty()
        last_source = _timestamp(row["last_source_timestamp"])
        if (
            last_received is None
            or last_received > _utc(query.as_of_timestamp)
            or (last_source is not None and last_source > _utc(query.as_of_timestamp))
        ):
            raise _Unavailable("ARCHIVE_REPLAY_BASELINE_AFTER_AS_OF")
        if not row["archive_replay_hash"] or state.digest() != row["archive_replay_hash"]:
            raise _Unavailable("ARCHIVE_REPLAY_BASELINE_HASH_MISMATCH")
        return state

    def _decode_chunk(
        self, chunk: sqlite3.Row
    ) -> tuple[tuple[KalshiWsOrderBookEventRecord, ...], dict[str, Any]]:
        relative = Path(str(chunk["relative_path"]))
        if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".zlib":
            raise _Unavailable("ARCHIVE_PATH_INVALID")
        path = (self.archive_root / relative).resolve()
        if self.archive_root not in path.parents or not path.is_file():
            raise _Unavailable("ARCHIVE_FILE_UNAVAILABLE")
        blob = path.read_bytes()
        if not chunk["file_checksum"] or hashlib.sha256(blob).hexdigest() != chunk["file_checksum"]:
            raise _Unavailable("ARCHIVE_FILE_CHECKSUM_MISMATCH")
        try:
            records, header = decode_archive_chunk(blob)
        except WsArchiveError as error:
            raise _Unavailable("ARCHIVE_CHUNK_DECODE_INVALID") from error
        if (
            records[0].row_id != int(chunk["first_event_id"])
            or records[-1].row_id != int(chunk["last_event_id"])
            or len(records) != int(chunk["event_count"])
        ):
            raise _Unavailable("ARCHIVE_CHUNK_MANIFEST_RANGE_MISMATCH")
        return records, header


class ArchiveResearchUnavailable(ValueError):
    """Raised only by the RDA convenience path for an explicit unavailable selection."""


class _Unavailable(ArchiveResearchUnavailable):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)
