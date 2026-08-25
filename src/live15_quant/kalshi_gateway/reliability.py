"""Transport-agnostic LIVE15 reliability rules for canonical Kalshi events."""

from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from live15_quant.kalshi_gateway.canonical_ws import (
    CanonicalEventType,
    CanonicalSdkEvent,
    invalid_payload_event,
    reconnect_event,
)
from live15_quant.kalshi_gateway.shadow_recorder import (
    BookPriceSample,
    GapRecord,
    executable_prices,
)
from live15_quant.kalshi_lifecycle import KalshiLifecycle, KalshiLifecycleStateMachine
from live15_quant.kalshi_ws import (
    KalshiAtomicOrderBookCoordinator,
    KalshiBookInvariantError,
    KalshiBookSide,
    KalshiOrderBookDelta,
    KalshiOrderBookSnapshot,
    KalshiSequenceGapError,
    KalshiUnsynchronizedBookError,
    SynchronizedKalshiOrderBook,
)
from live15_quant.models import Asset


class ReliabilityState(StrEnum):
    WAITING_SNAPSHOT = "WAITING_SNAPSHOT"
    SYNCHRONIZED = "SYNCHRONIZED"
    UNSYNCHRONIZED = "UNSYNCHRONIZED"
    QUARANTINED = "QUARANTINED"
    STALE = "STALE"


@dataclass(slots=True)
class AssetReliabilityState:
    ticker: str
    state: ReliabilityState = ReliabilityState.WAITING_SNAPSHOT
    last_frame_at: datetime | None = None
    last_valid_quote_at: datetime | None = None
    last_snapshot_at: datetime | None = None
    lifecycle: KalshiLifecycle = KalshiLifecycle.UNKNOWN
    snapshots: int = 0
    deltas: int = 0
    gaps: int = 0
    reconnects: int = 0


@dataclass(frozen=True, slots=True)
class ValidatedRecorderEvent:
    canonical: CanonicalSdkEvent
    state: ReliabilityState
    authoritative: bool
    book: SynchronizedKalshiOrderBook | None
    lifecycle: KalshiLifecycle


class ReliabilityPersistenceSink(Protocol):
    """Atomic persistence boundary used by the reliability layer.

    The adapter deliberately owns ordering, sequence and book reconstruction.
    A sink only records the already-validated outcome; it must never apply a
    second coordinator or infer an authoritative book from a delta.
    """

    def record_validated(
        self,
        event: CanonicalSdkEvent,
        *,
        authoritative: bool,
        book: SynchronizedKalshiOrderBook | None,
        transitions: tuple[tuple[Asset, str, str, str], ...] = (),
        transition_tickers: dict[Asset, str] | None = None,
        gap: GapRecord | None = None,
        recover_open_gaps: bool = False,
        diagnostic: tuple[str, str] | None = None,
    ) -> int: ...

    def record_state_transitions(
        self,
        *,
        observed_at: datetime,
        transitions: tuple[tuple[Asset, str, str, str], ...],
        tickers: dict[Asset, str],
    ) -> None: ...


_OPEN_VALUES: Final = frozenset({"active", "activated", "open", "opened", "market_open"})
_PAUSED_VALUES: Final = frozenset({"deactivated", "inactive", "paused", "market_paused"})
_CLOSED_VALUES: Final = frozenset({"closed", "market_closed"})
_PENDING_VALUES: Final = frozenset({"determined", "settlement_pending", "disputed", "amended"})


def lifecycle_from_canonical(event: CanonicalSdkEvent) -> KalshiLifecycle | None:
    if event.event_type is not CanonicalEventType.LIFECYCLE:
        raise ValueError("canonical event is not lifecycle data")
    value = event.lifecycle_type or ""
    if value == "created":
        return KalshiLifecycle.UPCOMING
    if value in _OPEN_VALUES:
        return KalshiLifecycle.OPEN
    if value in _PAUSED_VALUES:
        return KalshiLifecycle.PAUSED
    if value in _CLOSED_VALUES:
        return KalshiLifecycle.CLOSED
    if value in _PENDING_VALUES:
        return KalshiLifecycle.SETTLEMENT_PENDING
    if value in {"settled", "market_settled"}:
        if event.lifecycle_result == "yes":
            return KalshiLifecycle.SETTLED_YES
        if event.lifecycle_result == "no":
            return KalshiLifecycle.SETTLED_NO
        # Production can announce ``settled`` before including the official
        # binary result. Keep that recognized shape pending; finalized YES/NO
        # truth still requires an explicit provider result/settlement read.
        return KalshiLifecycle.SETTLEMENT_PENDING
    return None


class KalshiReliabilityAdapter:
    """Apply LIVE15 gap, quarantine, freshness and lifecycle rules to canonical input."""

    def __init__(
        self,
        asset_by_ticker: dict[str, Asset],
        recorder: ReliabilityPersistenceSink,
        *,
        connection_id: str,
        stale_seconds: float,
    ) -> None:
        if not asset_by_ticker or not connection_id or stale_seconds <= 0:
            raise ValueError("reliability adapter configuration is invalid")
        if len(set(asset_by_ticker.values())) != len(asset_by_ticker):
            raise ValueError("reliability adapter asset universe is not unique")
        self.asset_by_ticker = dict(asset_by_ticker)
        self.ticker_by_asset = {asset: ticker for ticker, asset in asset_by_ticker.items()}
        self.recorder = recorder
        self.connection_id = connection_id
        self.stale_seconds = stale_seconds
        self.coordinator = KalshiAtomicOrderBookCoordinator(connection_id, tuple(asset_by_ticker))
        self.assets = {
            asset: AssetReliabilityState(ticker=ticker) for ticker, asset in asset_by_ticker.items()
        }
        self.books: dict[Asset, SynchronizedKalshiOrderBook] = {}
        self.book_history: dict[Asset, deque[BookPriceSample]] = {
            asset: deque(maxlen=20_000) for asset in self.assets
        }
        self.connected_state = "disconnected"
        self._last_sequence: int | None = None
        self._last_orderbook_frame_at: datetime | None = None
        # ``AssetReliabilityState.gaps`` is historical diagnostics.  Health
        # gates need the narrower fact: gaps that have not yet been repaired
        # by a complete authoritative snapshot set.
        self._unrecovered_gap_assets: set[Asset] = set()

    @property
    def unrecovered_gap_count(self) -> int:
        """Number of assets covered by the currently open gap incident."""

        return len(self._unrecovered_gap_assets)

    @staticmethod
    def _transition(
        state: AssetReliabilityState,
        asset: Asset,
        new: ReliabilityState,
        reason: str,
        transitions: list[tuple[Asset, str, str, str]],
    ) -> None:
        if state.state is new:
            return
        old = state.state
        state.state = new
        transitions.append((asset, old.value, new.value, reason))

    def _quarantine_all(
        self,
        transitions: list[tuple[Asset, str, str, str]],
        *,
        reason: str,
        count_gap: bool,
    ) -> None:
        self.books.clear()
        for asset, state in self.assets.items():
            self._transition(state, asset, ReliabilityState.UNSYNCHRONIZED, reason, transitions)
            self._transition(state, asset, ReliabilityState.QUARANTINED, reason, transitions)
            # A reconnect can deliver several deltas before its replacement
            # snapshot set.  They all belong to one open incident, not a new
            # unrecovered gap on every rejected delta.
            if count_gap and asset not in self._unrecovered_gap_assets:
                self._unrecovered_gap_assets.add(asset)
                state.gaps += 1

    def _book_message(
        self, event: CanonicalSdkEvent
    ) -> KalshiOrderBookSnapshot | KalshiOrderBookDelta:
        if event.subscription_id is None or event.sequence is None or event.market_id is None:
            raise ValueError("canonical book envelope is incomplete")
        if event.event_type is CanonicalEventType.SNAPSHOT:
            return KalshiOrderBookSnapshot(
                connection_id=event.connection_id,
                subscription_id=event.subscription_id,
                sequence=event.sequence,
                ticker=event.ticker,
                market_id=event.market_id,
                yes_bids=event.yes_bids,
                no_bids=event.no_bids,
                source_timestamp=event.exchange_timestamp,
                socket_received_timestamp=event.sdk_receive_timestamp,
                parse_timestamp=event.sdk_receive_timestamp,
                provenance=event.provenance,
            )
        if (
            event.event_type is not CanonicalEventType.DELTA
            or event.delta_side not in {"yes", "no"}
            or event.delta_price is None
            or event.delta_quantity is None
        ):
            raise ValueError("canonical delta is incomplete")
        return KalshiOrderBookDelta(
            connection_id=event.connection_id,
            subscription_id=event.subscription_id,
            sequence=event.sequence,
            ticker=event.ticker,
            market_id=event.market_id,
            side=(KalshiBookSide.YES if event.delta_side == "yes" else KalshiBookSide.NO),
            price=event.delta_price,
            quantity_delta=event.delta_quantity,
            source_timestamp=event.exchange_timestamp,
            socket_received_timestamp=event.sdk_receive_timestamp,
            parse_timestamp=event.sdk_receive_timestamp,
            provenance=event.provenance,
        )

    def _persist(
        self,
        event: CanonicalSdkEvent,
        *,
        authoritative: bool,
        book: SynchronizedKalshiOrderBook | None,
        transitions: list[tuple[Asset, str, str, str]],
        gap: GapRecord | None = None,
        recover_open_gaps: bool = False,
        diagnostic: tuple[str, str] | None = None,
    ) -> None:
        try:
            self.recorder.record_validated(
                event,
                authoritative=authoritative,
                book=book,
                transitions=tuple(transitions),
                transition_tickers=self.ticker_by_asset,
                gap=gap,
                recover_open_gaps=recover_open_gaps,
                diagnostic=diagnostic,
            )
        except sqlite3.Error:
            # Recorder atomicity is a reliability dependency for this shadow.
            # Never expose an in-memory book whose persistence outcome is unknown.
            emergency: list[tuple[Asset, str, str, str]] = []
            self._quarantine_all(
                emergency,
                reason="SHADOW_RECORDER_COMMIT_FAILED",
                count_gap=False,
            )
            self.coordinator.reset()
            raise

    def accept(self, event: CanonicalSdkEvent) -> ValidatedRecorderEvent:
        if event.connection_id != self.connection_id:
            raise ValueError("canonical event connection identity mismatch")
        if self.asset_by_ticker.get(event.ticker) is not event.asset:
            raise ValueError("canonical event asset/ticker identity mismatch")
        state = self.assets[event.asset]
        state.last_frame_at = event.sdk_receive_timestamp
        transitions: list[tuple[Asset, str, str, str]] = []

        if event.event_type in {
            CanonicalEventType.LIFECYCLE,
            CanonicalEventType.UNKNOWN_LIFECYCLE,
        }:
            if event.event_type is CanonicalEventType.LIFECYCLE and (
                event.event_ticker is None or not event.ticker.startswith(f"{event.event_ticker}-")
            ):
                self._persist(
                    event,
                    authoritative=False,
                    book=None,
                    transitions=transitions,
                    diagnostic=("LIFECYCLE_WINDOW_IDENTITY_INVALID", "fail_closed"),
                )
                return ValidatedRecorderEvent(event, state.state, False, None, state.lifecycle)
            observed = (
                None
                if event.event_type is CanonicalEventType.UNKNOWN_LIFECYCLE
                else lifecycle_from_canonical(event)
            )
            if observed is None:
                self._persist(
                    event,
                    authoritative=False,
                    book=None,
                    transitions=transitions,
                    diagnostic=(
                        "UNKNOWN_LIFECYCLE",
                        event.diagnostic or event.lifecycle_type or "",
                    ),
                )
                return ValidatedRecorderEvent(event, state.state, False, None, state.lifecycle)
            try:
                steps = KalshiLifecycleStateMachine.transition(state.lifecycle, observed)
            except Exception as error:
                self._persist(
                    event,
                    authoritative=False,
                    book=None,
                    transitions=transitions,
                    diagnostic=("LIFECYCLE_REGRESSION", type(error).__name__),
                )
                return ValidatedRecorderEvent(event, state.state, False, None, state.lifecycle)
            state.lifecycle = steps[-1]
            self._persist(
                event,
                authoritative=True,
                book=None,
                transitions=transitions,
            )
            return ValidatedRecorderEvent(event, state.state, True, None, state.lifecycle)

        if event.event_type is CanonicalEventType.TICKER:
            self._persist(
                event,
                authoritative=state.state is ReliabilityState.SYNCHRONIZED,
                book=None,
                transitions=transitions,
            )
            return ValidatedRecorderEvent(
                event,
                state.state,
                state.state is ReliabilityState.SYNCHRONIZED,
                None,
                state.lifecycle,
            )

        if event.event_type not in {CanonicalEventType.SNAPSHOT, CanonicalEventType.DELTA}:
            raise ValueError("canonical event is not accepted by the reliability adapter")

        message = self._book_message(event)
        materialize_delta = True
        cached_book = self.books.get(event.asset)
        if event.event_type is CanonicalEventType.DELTA and cached_book is not None:
            levels = cached_book.yes_bids if event.delta_side == "yes" else cached_book.no_bids
            cached_best = levels[0].price if levels else None
            # A lower-priced update cannot change either executable top.
            # Keep applying every sequenced delta to the coordinator, but
            # avoid rebuilding/sorting the full depth until the top can move.
            materialize_delta = cached_best is None or (
                event.delta_price is not None and event.delta_price >= cached_best
            )
        expected = None if self._last_sequence is None else self._last_sequence + 1
        gap: GapRecord | None = None
        recover = False
        try:
            self.coordinator.accept(message, materialize=False)
        except KalshiSequenceGapError as error:
            affected = tuple(sorted(self.assets, key=lambda item: item.value))
            gap = GapRecord(
                expected_sequence=expected,
                received_sequence=event.sequence,
                subscription_id=event.subscription_id,
                reason=str(error)[:200],
                affected_assets=affected,
            )
            self._quarantine_all(
                transitions,
                reason="SEQUENCE_GAP",
                count_gap=True,
            )
            if event.event_type is not CanonicalEventType.SNAPSHOT:
                self._persist(
                    event,
                    authoritative=False,
                    book=None,
                    transitions=transitions,
                    gap=gap,
                )
                return ValidatedRecorderEvent(event, state.state, False, None, state.lifecycle)
            self.coordinator.accept(message, materialize=False)
        except KalshiBookInvariantError as error:
            affected = tuple(sorted(self.assets, key=lambda item: item.value))
            gap = GapRecord(
                expected_sequence=expected,
                received_sequence=event.sequence,
                subscription_id=event.subscription_id,
                reason=f"BOOK_INVARIANT:{type(error).__name__}",
                affected_assets=affected,
            )
            self._quarantine_all(
                transitions,
                reason="BOOK_INVARIANT",
                count_gap=True,
            )
            self.coordinator.reset()
            if event.event_type is not CanonicalEventType.SNAPSHOT:
                self._persist(
                    event,
                    authoritative=False,
                    book=None,
                    transitions=transitions,
                    gap=gap,
                )
                return ValidatedRecorderEvent(event, state.state, False, None, state.lifecycle)
            self.coordinator.accept(message, materialize=False)

        self._last_sequence = event.sequence
        self._last_orderbook_frame_at = event.sdk_receive_timestamp
        if event.event_type is CanonicalEventType.SNAPSHOT:
            state.snapshots += 1
            state.last_snapshot_at = event.sdk_receive_timestamp
        else:
            state.deltas += 1

        synchronized = set(self.coordinator.synchronized_tickers)
        # A delta can change only its own market. Re-copying all ten full
        # books for every high-frequency delta multiplies the hot path without
        # adding any reliability information. Snapshots still evaluate the
        # complete set because they may complete a global resynchronization.
        affected_books = (
            self.asset_by_ticker.items()
            if event.event_type is CanonicalEventType.SNAPSHOT
            else ((event.ticker, event.asset),)
            if materialize_delta
            else ()
        )
        for ticker, asset in affected_books:
            asset_state = self.assets[asset]
            if ticker not in synchronized:
                if asset_state.state is not ReliabilityState.QUARANTINED:
                    self._transition(
                        asset_state,
                        asset,
                        ReliabilityState.WAITING_SNAPSHOT,
                        "AWAITING_AUTHORITATIVE_SNAPSHOT",
                        transitions,
                    )
                self.books.pop(asset, None)
                continue
            try:
                book = self.coordinator.book(ticker)
            except KalshiUnsynchronizedBookError:
                continue
            self.books[asset] = book
            if asset is event.asset or event.event_type is CanonicalEventType.SNAPSHOT:
                self.book_history[asset].append(
                    BookPriceSample(
                        observed_at=event.sdk_receive_timestamp,
                        sequence=book.sequence,
                        prices=executable_prices(book.yes_bids, book.no_bids),
                    )
                )
            asset_state.last_valid_quote_at = book.received_timestamp
            self._transition(
                asset_state,
                asset,
                ReliabilityState.SYNCHRONIZED,
                "AUTHORITATIVE_SNAPSHOT_SET_COMPLETE",
                transitions,
            )
        if (
            event.event_type is CanonicalEventType.DELTA
            and not materialize_delta
            and cached_book is not None
            and event.ticker in synchronized
        ):
            state.last_valid_quote_at = event.sdk_receive_timestamp
            self.book_history[event.asset].append(
                BookPriceSample(
                    observed_at=event.sdk_receive_timestamp,
                    sequence=event.sequence or cached_book.sequence,
                    prices=executable_prices(cached_book.yes_bids, cached_book.no_bids),
                )
            )
            self._transition(
                state,
                event.asset,
                ReliabilityState.SYNCHRONIZED,
                "AUTHORITATIVE_DELTA_CONTINUITY",
                transitions,
            )
        if event.event_type is CanonicalEventType.SNAPSHOT and len(synchronized) == len(
            self.asset_by_ticker
        ):
            recover = True
            self._unrecovered_gap_assets.clear()
        authoritative_book = self.books.get(event.asset)
        authoritative = (
            state.state is ReliabilityState.SYNCHRONIZED and authoritative_book is not None
        )
        self._persist(
            event,
            authoritative=authoritative,
            book=authoritative_book if authoritative else None,
            transitions=transitions,
            gap=gap,
            recover_open_gaps=recover,
        )
        return ValidatedRecorderEvent(
            event,
            state.state,
            authoritative,
            authoritative_book if authoritative else None,
            state.lifecycle,
        )

    def connection_state_changed(
        self, old_state: str, new_state: str, observed_at: datetime
    ) -> None:
        self.connected_state = new_state.lower()
        if self.connected_state not in {"connecting", "reconnecting", "disconnected", "closed"}:
            return
        self.coordinator.reset()
        self._last_sequence = None
        self._last_orderbook_frame_at = None
        transitions: list[tuple[Asset, str, str, str]] = []
        self._quarantine_all(
            transitions,
            reason="SDK_CONNECTION_NOT_STREAMING",
            count_gap=False,
        )
        for state in self.assets.values():
            if self.connected_state == "reconnecting":
                state.reconnects += 1
        # One canonical fact per asset makes the blast radius explicit.
        for asset, state in self.assets.items():
            event = reconnect_event(
                asset=asset,
                ticker=state.ticker,
                connection_id=self.connection_id,
                observed_at=observed_at,
                old_state=old_state,
                new_state=new_state,
            )
            relevant = tuple(item for item in transitions if item[0] is asset)
            self._persist(
                event,
                authoritative=False,
                book=None,
                transitions=list(relevant),
            )

    def payload_invalidated(
        self,
        *,
        ticker: str | None,
        subscription_id: int | None,
        sequence: int | None,
        observed_at: datetime,
        reason: str,
    ) -> None:
        asset = self.asset_by_ticker.get(ticker or "")
        if asset is None:
            asset = sorted(self.assets, key=lambda item: item.value)[0]
            ticker = self.ticker_by_asset[asset]
        expected = None if self._last_sequence is None else self._last_sequence + 1
        self.coordinator.reset()
        self._last_sequence = None
        transitions: list[tuple[Asset, str, str, str]] = []
        self._quarantine_all(
            transitions,
            reason="MALFORMED_SEQUENCED_PAYLOAD",
            count_gap=True,
        )
        event = invalid_payload_event(
            asset=asset,
            ticker=ticker,
            connection_id=self.connection_id,
            observed_at=observed_at,
            subscription_id=subscription_id,
            sequence=sequence,
            diagnostic=reason,
        )
        self._persist(
            event,
            authoritative=False,
            book=None,
            transitions=transitions,
            gap=GapRecord(
                expected_sequence=expected,
                received_sequence=sequence,
                subscription_id=subscription_id,
                reason="MALFORMED_SEQUENCED_PAYLOAD",
                affected_assets=tuple(sorted(self.assets, key=lambda item: item.value)),
            ),
            diagnostic=("MALFORMED_SEQUENCED_PAYLOAD", reason),
        )

    def refresh_freshness(self, observed_at: datetime) -> None:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("freshness timestamp must be timezone-aware")
        observed = observed_at.astimezone(UTC)
        transitions: list[tuple[Asset, str, str, str]] = []
        for asset, state in self.assets.items():
            if state.state not in {ReliabilityState.SYNCHRONIZED, ReliabilityState.STALE}:
                continue
            age = (
                None
                if state.last_valid_quote_at is None
                else max(0.0, (observed - state.last_valid_quote_at).total_seconds())
            )
            if age is None or age > self.stale_seconds:
                self._transition(
                    state,
                    asset,
                    ReliabilityState.STALE,
                    "PER_ASSET_QUOTE_STALE",
                    transitions,
                )
                self.books.pop(asset, None)
        if transitions:
            self.recorder.record_state_transitions(
                observed_at=observed,
                transitions=tuple(transitions),
                tickers=self.ticker_by_asset,
            )

    def book(self, asset: Asset) -> SynchronizedKalshiOrderBook:
        state = self.assets[asset]
        if state.state is not ReliabilityState.SYNCHRONIZED:
            raise KalshiUnsynchronizedBookError(
                f"SDK reliability book is not authoritative: {asset.value}"
            )
        try:
            book = self.coordinator.book(self.ticker_by_asset[asset])
        except KalshiUnsynchronizedBookError:
            raise KalshiUnsynchronizedBookError(
                f"SDK reliability book is unavailable: {asset.value}"
            ) from None
        self.books[asset] = book
        return book

    def price_samples(
        self,
        asset: Asset,
        *,
        since: datetime,
        until: datetime,
    ) -> tuple[BookPriceSample, ...]:
        return tuple(
            sample for sample in self.book_history[asset] if since <= sample.observed_at <= until
        )

    def nearest_price_sample(
        self,
        asset: Asset,
        *,
        target: datetime,
        tolerance_seconds: float,
    ) -> BookPriceSample | None:
        if tolerance_seconds <= 0:
            raise ValueError("alignment tolerance must be positive")
        candidates = self.book_history[asset]
        if not candidates:
            return None
        sample = min(
            candidates,
            key=lambda item: abs((item.observed_at - target).total_seconds()),
        )
        if abs((sample.observed_at - target).total_seconds()) > tolerance_seconds:
            return None
        return sample

    def maximum_last_frame_age(self, observed_at: datetime) -> float | None:
        ages = [
            max(0.0, (observed_at - state.last_frame_at).total_seconds())
            for state in self.assets.values()
            if state.last_frame_at is not None
        ]
        return max(ages) if ages else None

    def orderbook_processed_through(self, timestamp: datetime) -> bool:
        """Whether the canonical consumer crossed a wire-receive watermark."""

        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("orderbook watermark timestamp must be timezone-aware")
        return (
            self._last_orderbook_frame_at is not None
            and self._last_orderbook_frame_at >= timestamp.astimezone(UTC)
        )

    def health(self, observed_at: datetime) -> dict[str, object]:
        self.refresh_freshness(observed_at)
        observed = observed_at.astimezone(UTC)
        per_asset: dict[str, object] = {}
        synchronized = 0
        for asset, state in sorted(self.assets.items(), key=lambda item: item[0].value):
            frame_age = (
                None
                if state.last_frame_at is None
                else max(0.0, (observed - state.last_frame_at).total_seconds())
            )
            quote_age = (
                None
                if state.last_valid_quote_at is None
                else max(0.0, (observed - state.last_valid_quote_at).total_seconds())
            )
            snapshot_age = (
                None
                if state.last_snapshot_at is None
                else max(0.0, (observed - state.last_snapshot_at).total_seconds())
            )
            if state.state is ReliabilityState.SYNCHRONIZED:
                synchronized += 1
            per_asset[asset.value] = {
                "ticker": state.ticker,
                "state": state.state.value,
                "last_frame_age_seconds": frame_age,
                "last_valid_quote_age_seconds": quote_age,
                "last_snapshot_age_seconds": snapshot_age,
                "snapshots": state.snapshots,
                "deltas": state.deltas,
                "gaps": state.gaps,
                "reconnects": state.reconnects,
                "lifecycle": state.lifecycle.value,
            }
        return {
            "connected_status": self.connected_state,
            "subscribed_assets": len(self.assets),
            "synchronized_count": synchronized,
            "assets": per_asset,
            "metrics": self.recorder.summary(),
        }


def assert_shadow_path_isolated(shadow_path: Path, official_recorder_path: Path) -> None:
    if shadow_path.resolve() == official_recorder_path.resolve():
        raise ValueError("shadow and official Recorder paths must be different")
