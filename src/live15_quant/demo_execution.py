"""Fail-closed Kalshi Demo order intent, risk, and reconciliation infrastructure."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from live15_quant.execution import ExecutionAction
from live15_quant.fees import KalshiTakerFeeModel
from live15_quant.providers.kalshi_demo_execution import (
    DemoBookSide,
    DemoOrderRequest,
    DemoRemoteFill,
    DemoRemoteOrder,
    DemoRemoteOrderState,
    KalshiDemoAmbiguousWriteError,
    KalshiDemoExecutionClient,
    KalshiDemoExecutionError,
    KalshiDemoWriteRejectedError,
)
from live15_quant.runtime_status import RuntimeStatusError, read_json


class DemoExecutionError(RuntimeError):
    """Raised when Demo execution correctness cannot be guaranteed."""


class DemoEnvironment(StrEnum):
    KALSHI_DEMO = "KALSHI_DEMO"


class DemoLifecycleState(StrEnum):
    INTENT = "INTENT"
    SUBMITTING = "SUBMITTING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    SETTLED = "SETTLED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class DemoIntentPurpose(StrEnum):
    """Explicitly separates a future first-write smoke from model-forward execution."""

    EXECUTION_SMOKE = "EXECUTION_SMOKE"
    MODEL_FORWARD = "MODEL_FORWARD"


class DemoRiskReason(StrEnum):
    WRITES_DISABLED = "writes_disabled"
    EXPLICIT_SMOKE_APPROVAL_REQUIRED = "explicit_smoke_approval_required"
    SMOKE_ONLY = "smoke_only"
    KILL_SWITCH = "kill_switch"
    MAX_ORDER_SIZE = "max_order_size"
    MAX_ORDER_NOTIONAL = "max_order_notional"
    MAX_EVENT_EXPOSURE = "max_event_exposure"
    MAX_TOTAL_EXPOSURE = "max_total_exposure"
    MAX_CONCURRENT_POSITIONS = "max_concurrent_positions"
    MAX_DAILY_LOSS = "max_daily_loss"
    INSUFFICIENT_BUYING_POWER = "insufficient_buying_power"
    RECONCILIATION_UNCERTAIN = "reconciliation_uncertain"
    FIXED_SIZING_POLICY = "fixed_sizing_policy"
    REMOTE_RISK_STATE_UNKNOWN = "remote_risk_state_unknown"
    OFFICIAL_TRUTH_UNAVAILABLE = "official_truth_unavailable"
    EXCHANGE_UNAVAILABLE = "exchange_unavailable"
    MARKET_NOT_TRADEABLE = "market_not_tradeable"
    DECISION_STALE = "decision_stale"
    CLOCK_UNCERTAIN = "clock_uncertain"
    PRE_SUBMIT_DATA_UNAVAILABLE = "pre_submit_data_unavailable"
    PRICE_MOVED_TOO_FAR = "price_moved_too_far"
    EDGE_DECAYED_BEFORE_SUBMIT = "edge_decayed_before_submit"


class DemoExecutionResultCode(StrEnum):
    PRE_SUBMIT_ALLOWED = "PRE_SUBMIT_ALLOWED"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    PRICE_MOVED_TOO_FAR = "PRICE_MOVED_TOO_FAR"
    EDGE_DECAYED_BEFORE_SUBMIT = "EDGE_DECAYED_BEFORE_SUBMIT"
    HTTP_SUCCESS_FILLED = "HTTP_SUCCESS_FILLED"
    HTTP_SUCCESS_NO_FILL = "HTTP_SUCCESS_NO_FILL"
    HTTP_4XX = "HTTP_4XX"
    HTTP_400 = "HTTP_400"
    HTTP_401 = "HTTP_401"
    HTTP_403 = "HTTP_403"
    HTTP_404 = "HTTP_404"
    HTTP_422 = "HTTP_422"
    HTTP_409 = "HTTP_409"
    HTTP_429 = "HTTP_429"
    HTTP_5XX = "HTTP_5XX"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    MALFORMED_ACK = "MALFORMED_ACK"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class DemoDataUnavailableReason(StrEnum):
    """Stable, non-sensitive reasons for a fail-closed pre-submit data gate."""

    BOOK_UNSYNCHRONIZED = "BOOK_UNSYNCHRONIZED"
    SNAPSHOT_NOT_READY = "SNAPSHOT_NOT_READY"
    QUOTE_STALE = "QUOTE_STALE"
    NO_EXECUTABLE_ASK = "NO_EXECUTABLE_ASK"
    NO_EXECUTABLE_BID = "NO_EXECUTABLE_BID"
    MARKET_INACTIVE = "MARKET_INACTIVE"
    MARKET_ROLLED = "MARKET_ROLLED"
    DECISION_STALE = "DECISION_STALE"
    BOOK_PROVENANCE_MISMATCH = "BOOK_PROVENANCE_MISMATCH"
    HEALTH_UNAVAILABLE = "HEALTH_UNAVAILABLE"
    CHECKPOINT_MALFORMED = "CHECKPOINT_MALFORMED"
    CHECKPOINT_NOT_READY = "CHECKPOINT_NOT_READY"
    OFFICIAL_TRUTH_UNAVAILABLE = "OFFICIAL_TRUTH_UNAVAILABLE"
    LIVE_WS_UNAVAILABLE = "LIVE_WS_UNAVAILABLE"
    LIVE_WS_STATE_MALFORMED = "LIVE_WS_STATE_MALFORMED"


class DemoSizingMode(StrEnum):
    FIXED = "fixed"
    BOUNDED_EQUITY_FUTURE = "bounded_equity_future"


@dataclass(frozen=True, slots=True)
class DemoSizingPolicy:
    """Frozen Demo-v1 size; equity-based sizing is an explicit inactive future boundary."""

    mode: DemoSizingMode = DemoSizingMode.FIXED
    fixed_order_count: Decimal = Decimal("1")
    equity_sizing_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.fixed_order_count.is_finite() or self.fixed_order_count <= 0:
            raise ValueError("Demo fixed order count must be finite and positive")
        if self.fixed_order_count > Decimal("1"):
            raise ValueError("Demo v1 fixed order count may not exceed one contract")
        if self.mode is not DemoSizingMode.FIXED or self.equity_sizing_enabled:
            raise ValueError("bounded equity sizing is not enabled in Demo v1")


@dataclass(frozen=True, slots=True)
class DemoRiskLimits:
    max_order_count: Decimal = Decimal("1")
    max_order_notional: Decimal = Decimal("1")
    max_event_exposure: Decimal = Decimal("2")
    max_total_exposure: Decimal = Decimal("5")
    max_concurrent_positions: int = 3
    max_daily_loss: Decimal = Decimal("2")

    def __post_init__(self) -> None:
        decimals = (
            self.max_order_count,
            self.max_order_notional,
            self.max_event_exposure,
            self.max_total_exposure,
            self.max_daily_loss,
        )
        if any(not value.is_finite() or value <= 0 for value in decimals):
            raise ValueError("Demo risk limits must be finite and positive")
        if self.max_concurrent_positions <= 0:
            raise ValueError("Demo concurrent-position limit must be positive")
        maximums = (
            (self.max_order_count, Decimal("1")),
            (self.max_order_notional, Decimal("1")),
            (self.max_event_exposure, Decimal("2")),
            (self.max_total_exposure, Decimal("5")),
            (self.max_daily_loss, Decimal("2")),
        )
        if any(value > maximum for value, maximum in maximums) or self.max_concurrent_positions > 3:
            raise ValueError("Demo v1 hard-risk limits may be tightened but never expanded")


@dataclass(frozen=True, slots=True)
class DemoRiskContext:
    event_exposure: Decimal
    total_exposure: Decimal
    open_positions: int
    daily_realized_pnl: Decimal
    reconciliation_certain: bool = True
    kill_switch: bool = True
    account_state_known: bool = False
    positions_state_known: bool = False
    open_orders_state_known: bool = False
    daily_pnl_known: bool = False
    official_buying_power: Decimal | None = None
    official_exchange_active: bool | None = None
    official_trading_active: bool | None = None
    official_market_ticker: str | None = None
    official_market_status: str | None = None
    official_truth_received_at: datetime | None = None

    def __post_init__(self) -> None:
        values = (self.event_exposure, self.total_exposure, self.daily_realized_pnl)
        if any(not value.is_finite() for value in values):
            raise ValueError("Demo risk context values must be finite")
        if self.event_exposure < 0 or self.total_exposure < 0 or self.open_positions < 0:
            raise ValueError("Demo exposure/position context must be non-negative")
        if self.official_buying_power is not None and (
            not self.official_buying_power.is_finite() or self.official_buying_power < 0
        ):
            raise ValueError("Demo official buying power must be finite and non-negative")
        if self.official_truth_received_at is not None and (
            self.official_truth_received_at.tzinfo is None
            or self.official_truth_received_at.utcoffset() is None
        ):
            raise ValueError("Demo official truth timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class DemoIntent:
    model_id: str
    model_artifact_hash: str
    decision_id: str
    event_id: str
    opportunity_id: str
    ticker: str
    side: DemoBookSide
    count: Decimal
    price: Decimal
    probability: Decimal
    edge: Decimal
    decision_timestamp: datetime
    purpose: DemoIntentPurpose

    def __post_init__(self) -> None:
        if not all(
            (
                self.model_id,
                self.model_artifact_hash,
                self.decision_id,
                self.event_id,
                self.opportunity_id,
                self.ticker,
            )
        ):
            raise ValueError("Demo intent identifiers must not be empty")
        if len(self.model_artifact_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.model_artifact_hash.lower()
        ):
            raise ValueError("Demo model artifact hash must be SHA-256 hex")
        if self.decision_timestamp.tzinfo is None or self.decision_timestamp.utcoffset() is None:
            raise ValueError("Demo decision timestamp must be timezone-aware")
        DemoOrderRequest(
            ticker=self.ticker,
            client_order_id=stable_client_order_id(self),
            side=self.side,
            count=self.count,
            price=self.price,
        )
        if (
            not self.probability.is_finite()
            or not Decimal(0) <= self.probability <= Decimal(1)
            or not self.edge.is_finite()
        ):
            raise ValueError("Demo probability/edge must be finite and probability within [0, 1]")


@dataclass(frozen=True, slots=True)
class DemoSynchronizedQuote:
    """Latest executable single-book truth read immediately before a Demo submit."""

    ticker: str
    received_timestamp: datetime
    synchronized: bool
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None
    source: str = "KALSHI_WS_SYNCHRONIZED"
    book_received_timestamp: datetime | None = None
    live_book_read_at: datetime | None = None
    subscription_id: int | None = None
    sequence: int | None = None

    def __post_init__(self) -> None:
        if not self.ticker or self.received_timestamp.tzinfo is None:
            raise ValueError("pre-submit quote identity/timestamp is invalid")
        if self.received_timestamp.utcoffset() is None:
            raise ValueError("pre-submit quote timestamp must be timezone-aware")
        if self.source not in {"KALSHI_WS_SYNCHRONIZED", "LIVE_KALSHI_WS"}:
            raise ValueError("pre-submit execution quotes must come from synchronized Kalshi WS")
        for timestamp in (self.book_received_timestamp, self.live_book_read_at):
            if timestamp is not None and (
                timestamp.tzinfo is None or timestamp.utcoffset() is None
            ):
                raise ValueError("live quote diagnostic timestamps must be timezone-aware")
        if self.source == "LIVE_KALSHI_WS" and (
            self.subscription_id is None
            or self.subscription_id < 1
            or self.sequence is None
            or self.sequence < 1
        ):
            raise ValueError("live synchronized quote requires positive sid/seq")
        prices = (self.yes_bid, self.yes_ask, self.no_bid, self.no_ask)
        if any(
            value is not None and (not value.is_finite() or not Decimal(0) < value < Decimal(1))
            for value in prices
        ):
            raise ValueError("pre-submit quote prices must be finite and strictly within (0,1)")
        if self.yes_bid is not None and self.yes_ask is not None:
            if self.yes_bid > self.yes_ask:
                raise ValueError("pre-submit YES book is crossed")
        if self.no_bid is not None and self.no_ask is not None:
            if self.no_bid > self.no_ask:
                raise ValueError("pre-submit NO book is crossed")
        if self.yes_bid is not None and self.no_ask is not None:
            if self.yes_bid + self.no_ask != Decimal(1):
                raise ValueError("pre-submit YES bid/NO ask are not complementary")
        if self.no_bid is not None and self.yes_ask is not None:
            if self.no_bid + self.yes_ask != Decimal(1):
                raise ValueError("pre-submit NO bid/YES ask are not complementary")


class DemoPreSubmitQuoteSource(Protocol):
    def latest_quote(self, ticker: str) -> DemoSynchronizedQuote | None: ...

    def last_unavailable_reason(self, ticker: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class DemoPriceEVPolicy:
    max_quote_age: timedelta = timedelta(seconds=2)
    max_adverse_price_move: Decimal = Decimal("0.03")
    safety_margin: Decimal = Decimal("0.005")
    minimum_required_edge: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        decimals = (
            self.max_adverse_price_move,
            self.safety_margin,
            self.minimum_required_edge,
        )
        if self.max_quote_age <= timedelta(0):
            raise ValueError("pre-submit quote age must be positive")
        if any(not value.is_finite() or value < 0 for value in decimals):
            raise ValueError("pre-submit price/EV policy values must be finite and non-negative")
        if self.max_adverse_price_move > Decimal("0.25"):
            raise ValueError("pre-submit adverse move budget is unreasonably large")


@dataclass(frozen=True, slots=True)
class DemoPreSubmitGuardResult:
    code: DemoExecutionResultCode
    allowed: bool
    ticker: str
    side: DemoBookSide
    decision_price: Decimal
    pre_submit_price: Decimal | None
    submitted_limit: Decimal | None
    max_acceptable_price: Decimal | None
    decision_edge: Decimal
    pre_submit_net_edge: Decimal | None
    estimated_fee: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    spread: Decimal | None
    quote_timestamp: datetime | None
    quote_age_seconds: Decimal | None
    price_source: str | None
    data_unavailable_reason: str | None = None
    live_book_read_at: datetime | None = None
    pre_submit_ready_at: datetime | None = None
    book_received_timestamp: datetime | None = None
    subscription_id: int | None = None
    sequence: int | None = None

    def diagnostics(self) -> dict[str, object]:
        def decimal(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "ticker": self.ticker,
            "side": self.side.value,
            "decision_price": str(self.decision_price),
            "pre_submit_price": decimal(self.pre_submit_price),
            "submitted_limit": decimal(self.submitted_limit),
            "max_acceptable_price": decimal(self.max_acceptable_price),
            "decision_edge": str(self.decision_edge),
            "pre_submit_net_edge": decimal(self.pre_submit_net_edge),
            "estimated_fee": decimal(self.estimated_fee),
            "best_bid": decimal(self.best_bid),
            "best_ask": decimal(self.best_ask),
            "spread": decimal(self.spread),
            "quote_timestamp": (
                None if self.quote_timestamp is None else _timestamp(self.quote_timestamp)
            ),
            "quote_age_seconds": decimal(self.quote_age_seconds),
            "price_source": self.price_source,
            "data_unavailable_reason": self.data_unavailable_reason,
            "live_book_read_at": (
                None if self.live_book_read_at is None else _timestamp(self.live_book_read_at)
            ),
            "pre_submit_ready_at": (
                None if self.pre_submit_ready_at is None else _timestamp(self.pre_submit_ready_at)
            ),
            "book_received_timestamp": (
                None
                if self.book_received_timestamp is None
                else _timestamp(self.book_received_timestamp)
            ),
            "subscription_id": self.subscription_id,
            "sequence": self.sequence,
        }


class PreSubmitPriceEVGuard:
    """Fail closed when a fresh executable quote no longer supports the frozen intent."""

    def __init__(self, policy: DemoPriceEVPolicy | None = None) -> None:
        self.policy = policy or DemoPriceEVPolicy()

    @staticmethod
    def _fee(quantity: Decimal, price: Decimal) -> Decimal:
        model = KalshiTakerFeeModel()
        computation = model.compute(
            order_id="pre-submit-quote",
            quantity=quantity,
            price=price,
            action=ExecutionAction.BUY,
        )
        model.finish_order("pre-submit-quote")
        return computation.net_fee / quantity

    def evaluate(
        self,
        intent: DemoIntent,
        quote: DemoSynchronizedQuote | None,
        *,
        evaluated_at: datetime,
        data_unavailable_reason: str | None = None,
    ) -> DemoPreSubmitGuardResult:
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ValueError("pre-submit evaluation timestamp must be timezone-aware")
        unavailable = quote is None or quote.ticker != intent.ticker or not quote.synchronized
        unavailable_reason = data_unavailable_reason
        if quote is not None and quote.ticker != intent.ticker:
            unavailable_reason = DemoDataUnavailableReason.BOOK_PROVENANCE_MISMATCH.value
        elif quote is not None and not quote.synchronized:
            unavailable_reason = DemoDataUnavailableReason.BOOK_UNSYNCHRONIZED.value
        if quote is None:
            age = None
        else:
            age = Decimal(str((evaluated_at - quote.received_timestamp).total_seconds()))
            unavailable = (
                unavailable
                or age < 0
                or age > Decimal(str(self.policy.max_quote_age.total_seconds()))
            )
            if age < 0 or age > Decimal(str(self.policy.max_quote_age.total_seconds())):
                unavailable_reason = DemoDataUnavailableReason.QUOTE_STALE.value
        selected_bid = None
        selected_ask = None
        submitted_limit = None
        if quote is not None:
            if intent.side is DemoBookSide.BID:
                selected_bid, selected_ask = quote.yes_bid, quote.yes_ask
                submitted_limit = selected_ask
            else:
                selected_bid, selected_ask = quote.no_bid, quote.no_ask
                submitted_limit = None if selected_ask is None else Decimal(1) - selected_ask
            unavailable = unavailable or selected_bid is None or selected_ask is None
            if selected_ask is None:
                unavailable_reason = DemoDataUnavailableReason.NO_EXECUTABLE_ASK.value
            elif selected_bid is None:
                unavailable_reason = DemoDataUnavailableReason.NO_EXECUTABLE_BID.value
        spread = (
            None if selected_bid is None or selected_ask is None else selected_ask - selected_bid
        )
        base = {
            "ticker": intent.ticker,
            "side": intent.side,
            "decision_price": intent.price,
            "decision_edge": intent.edge,
            "best_bid": selected_bid,
            "best_ask": selected_ask,
            "spread": spread,
            "quote_timestamp": None if quote is None else quote.received_timestamp,
            "quote_age_seconds": age,
            "price_source": None if quote is None else quote.source,
            "live_book_read_at": None if quote is None else quote.live_book_read_at,
            "pre_submit_ready_at": evaluated_at,
            "book_received_timestamp": (None if quote is None else quote.book_received_timestamp),
            "subscription_id": None if quote is None else quote.subscription_id,
            "sequence": None if quote is None else quote.sequence,
        }
        if unavailable or selected_ask is None or submitted_limit is None:
            return DemoPreSubmitGuardResult(
                DemoExecutionResultCode.DATA_UNAVAILABLE,
                False,
                pre_submit_price=selected_ask,
                submitted_limit=submitted_limit,
                max_acceptable_price=None,
                pre_submit_net_edge=None,
                estimated_fee=None,
                **base,
                data_unavailable_reason=(
                    unavailable_reason or DemoDataUnavailableReason.SNAPSHOT_NOT_READY.value
                ),
            )
        side_probability = (
            intent.probability
            if intent.side is DemoBookSide.BID
            else Decimal(1) - intent.probability
        )
        decision_fee = self._fee(intent.count, intent.price)
        current_fee = self._fee(intent.count, selected_ask)
        edge_cap = (
            intent.price
            + intent.edge
            - decision_fee
            - self.policy.safety_margin
            - self.policy.minimum_required_edge
        )
        probability_cap = (
            side_probability
            - decision_fee
            - self.policy.safety_margin
            - self.policy.minimum_required_edge
        )
        move_cap = intent.price + self.policy.max_adverse_price_move
        max_acceptable = min(edge_cap, probability_cap, move_cap, Decimal("0.9999"))
        net_edge = side_probability - selected_ask - current_fee - self.policy.safety_margin
        if selected_ask > max_acceptable:
            code = DemoExecutionResultCode.PRICE_MOVED_TOO_FAR
            allowed = False
        elif net_edge <= self.policy.minimum_required_edge:
            code = DemoExecutionResultCode.EDGE_DECAYED_BEFORE_SUBMIT
            allowed = False
        else:
            code = DemoExecutionResultCode.PRE_SUBMIT_ALLOWED
            allowed = True
        return DemoPreSubmitGuardResult(
            code,
            allowed,
            pre_submit_price=selected_ask,
            submitted_limit=submitted_limit,
            max_acceptable_price=max_acceptable,
            pre_submit_net_edge=net_edge,
            estimated_fee=current_fee,
            **base,
        )


class SqliteKalshiWsQuoteSource:
    """Bounded read of a materialized synchronized WS checkpoint.

    Raw delta rows intentionally do not repeat reconstructed depth. Reading the
    last raw event as if it were a full book would therefore manufacture an
    unavailable quote. This source accepts only an explicit full checkpoint,
    paired with the recorder's current synchronized-market health boundary.
    The pre-submit age policy still applies, so a sparse or old checkpoint fails
    closed; latency-sensitive execution should inject a live WS quote source.
    """

    def __init__(
        self,
        raw_path: Path,
        health_path: Path,
        *,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self._raw_path = raw_path
        self._health_path = health_path
        self._utc_now = utc_now or (lambda: datetime.now(UTC))
        self._last_reason_by_ticker: dict[str, str] = {}

    def _unavailable(self, ticker: str, reason: DemoDataUnavailableReason) -> None:
        self._last_reason_by_ticker[ticker] = reason.value

    def last_unavailable_reason(self, ticker: str) -> str | None:
        return self._last_reason_by_ticker.get(ticker)

    def latest_quote(self, ticker: str) -> DemoSynchronizedQuote | None:
        try:
            health = json.loads(self._health_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._unavailable(ticker, DemoDataUnavailableReason.HEALTH_UNAVAILABLE)
            return None
        if (
            not isinstance(health, dict)
            or health.get("kalshi_ws_connection_state") != "synchronized"
        ):
            self._unavailable(ticker, DemoDataUnavailableReason.BOOK_UNSYNCHRONIZED)
            return None
        synchronized = health.get("kalshi_ws_synchronized_markets")
        if not isinstance(synchronized, dict) or ticker not in synchronized.values():
            current = health.get("current_markets")
            self._unavailable(
                ticker,
                DemoDataUnavailableReason.MARKET_ROLLED
                if isinstance(current, dict) and ticker not in current.values()
                else DemoDataUnavailableReason.SNAPSHOT_NOT_READY,
            )
            return None
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"file:{self._raw_path.resolve().as_posix()}?mode=ro",
                uri=True,
                timeout=2,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                """SELECT ticker,received_timestamp,yes_bids,no_bids,provenance
                   FROM kalshi_ws_book_checkpoints
                   WHERE ticker=?
                   ORDER BY received_timestamp DESC,id DESC LIMIT 1""",
                (ticker,),
            ).fetchone()
        except (OSError, sqlite3.Error):
            self._unavailable(ticker, DemoDataUnavailableReason.CHECKPOINT_NOT_READY)
            return None
        finally:
            if connection is not None:
                connection.close()
        if row is None:
            self._unavailable(ticker, DemoDataUnavailableReason.SNAPSHOT_NOT_READY)
            return None
        try:
            if str(row["provenance"]) != "kalshi_ws":
                self._unavailable(ticker, DemoDataUnavailableReason.BOOK_PROVENANCE_MISMATCH)
                return None
            yes_bids = json.loads(str(row["yes_bids"]))
            no_bids = json.loads(str(row["no_bids"]))
            if not isinstance(yes_bids, list) or not isinstance(no_bids, list):
                return None
            yes = tuple((Decimal(str(level[0])), Decimal(str(level[1]))) for level in yes_bids)
            no = tuple((Decimal(str(level[0])), Decimal(str(level[1]))) for level in no_bids)
            if not yes or not no:
                self._unavailable(ticker, DemoDataUnavailableReason.CHECKPOINT_MALFORMED)
                return None
            received = datetime.fromisoformat(str(row["received_timestamp"])).astimezone(UTC)
            yes_bid = max(price for price, quantity in yes if quantity > 0)
            no_bid = max(price for price, quantity in no if quantity > 0)
            quote = DemoSynchronizedQuote(
                ticker=str(row["ticker"]),
                received_timestamp=received,
                synchronized=True,
                yes_bid=yes_bid,
                yes_ask=Decimal(1) - no_bid,
                no_bid=no_bid,
                no_ask=Decimal(1) - yes_bid,
            )
        except (InvalidOperation, IndexError, TypeError, ValueError, json.JSONDecodeError):
            self._unavailable(ticker, DemoDataUnavailableReason.CHECKPOINT_MALFORMED)
            return None
        if self._utc_now() < quote.received_timestamp:
            self._unavailable(ticker, DemoDataUnavailableReason.QUOTE_STALE)
            return None
        self._last_reason_by_ticker.pop(ticker, None)
        return quote


class LiveKalshiWsQuoteSource:
    """Read the Recorder-owned atomic live-book projection; never consult SQLite."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        path: Path,
        *,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = path
        self._utc_now = utc_now or (lambda: datetime.now(UTC))
        self._last_reason_by_ticker: dict[str, str] = {}

    def _unavailable(self, ticker: str, reason: DemoDataUnavailableReason) -> None:
        self._last_reason_by_ticker[ticker] = reason.value

    def last_unavailable_reason(self, ticker: str) -> str | None:
        return self._last_reason_by_ticker.get(ticker)

    def latest_quote(self, ticker: str) -> DemoSynchronizedQuote | None:
        read_at = self._utc_now().astimezone(UTC)
        try:
            payload = read_json(self._path, maximum_bytes=512 * 1024)
        except RuntimeStatusError:
            self._unavailable(ticker, DemoDataUnavailableReason.LIVE_WS_UNAVAILABLE)
            return None
        if payload is None or payload.get("schema_version") != self.SCHEMA_VERSION:
            self._unavailable(ticker, DemoDataUnavailableReason.LIVE_WS_UNAVAILABLE)
            return None
        if payload.get("state") != "SYNCHRONIZED":
            self._unavailable(ticker, DemoDataUnavailableReason.BOOK_UNSYNCHRONIZED)
            return None
        books = payload.get("books")
        if not isinstance(books, dict):
            self._unavailable(ticker, DemoDataUnavailableReason.LIVE_WS_UNAVAILABLE)
            return None
        raw = books.get(ticker)
        if not isinstance(raw, dict):
            current_tickers = payload.get("current_tickers")
            self._unavailable(
                ticker,
                DemoDataUnavailableReason.MARKET_ROLLED
                if isinstance(current_tickers, list) and ticker not in current_tickers
                else DemoDataUnavailableReason.SNAPSHOT_NOT_READY,
            )
            return None
        if raw.get("provenance") != "kalshi_ws" or raw.get("status") != "SYNCHRONIZED":
            reason = (
                DemoDataUnavailableReason.BOOK_PROVENANCE_MISMATCH
                if raw.get("provenance") != "kalshi_ws"
                else DemoDataUnavailableReason.BOOK_UNSYNCHRONIZED
            )
            self._unavailable(ticker, reason)
            return None
        try:
            yes = _projection_levels(raw.get("yes_bids"))
            no = _projection_levels(raw.get("no_bids"))
            if not yes or not no:
                self._unavailable(ticker, DemoDataUnavailableReason.NO_EXECUTABLE_ASK)
                return None
            transport_received = _parse_aware_timestamp(payload.get("transport_received_at"))
            published_at = _parse_aware_timestamp(payload.get("published_at"))
            book_received = _parse_aware_timestamp(raw.get("book_received_at"))
            subscription_id = int(raw["subscription_id"])
            sequence = int(raw["sequence"])
            yes_bid = max(price for price, quantity in yes if quantity > 0)
            no_bid = max(price for price, quantity in no if quantity > 0)
            quote = DemoSynchronizedQuote(
                ticker=str(raw["ticker"]),
                received_timestamp=transport_received,
                synchronized=True,
                yes_bid=yes_bid,
                yes_ask=Decimal(1) - no_bid,
                no_bid=no_bid,
                no_ask=Decimal(1) - yes_bid,
                source="LIVE_KALSHI_WS",
                book_received_timestamp=book_received,
                live_book_read_at=read_at,
                subscription_id=subscription_id,
                sequence=sequence,
            )
        except (KeyError, InvalidOperation, TypeError, ValueError):
            self._unavailable(ticker, DemoDataUnavailableReason.LIVE_WS_STATE_MALFORMED)
            return None
        if (
            quote.ticker != ticker
            or book_received > transport_received
            or transport_received > published_at
            or published_at > read_at
        ):
            self._unavailable(ticker, DemoDataUnavailableReason.BOOK_PROVENANCE_MISMATCH)
            return None
        self._last_reason_by_ticker.pop(ticker, None)
        return quote


def _projection_levels(value: object) -> tuple[tuple[Decimal, Decimal], ...]:
    if not isinstance(value, list):
        raise ValueError("live WS levels must be a list")
    levels: list[tuple[Decimal, Decimal]] = []
    for level in value:
        if not isinstance(level, list) or len(level) != 2:
            raise ValueError("live WS level shape is invalid")
        price, quantity = Decimal(str(level[0])), Decimal(str(level[1]))
        if not price.is_finite() or not quantity.is_finite() or quantity <= 0:
            raise ValueError("live WS level is invalid")
        levels.append((price, quantity))
    return tuple(levels)


def _parse_aware_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("live WS timestamp is missing")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("live WS timestamp must be timezone-aware")
    return timestamp.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DemoRiskDecision:
    allowed: bool
    reasons: tuple[DemoRiskReason, ...]


@dataclass(frozen=True, slots=True)
class DemoReconciliationResult:
    client_order_id: str
    state: DemoLifecycleState
    provider_order_id: str | None
    inserted_fills: int


def stable_client_order_id(intent: DemoIntent) -> str:
    """Stable UUID scoped by environment, frozen model, event, and opportunity."""

    identity = "|".join(
        (
            DemoEnvironment.KALSHI_DEMO.value,
            intent.model_id,
            intent.event_id,
            intent.opportunity_id,
        )
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"live15://demo-order/{identity}"))


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def evaluate_demo_risk(
    intent: DemoIntent,
    context: DemoRiskContext,
    limits: DemoRiskLimits,
    *,
    writes_enabled: bool,
    execution_smoke_approved: bool,
) -> DemoRiskDecision:
    reasons: list[DemoRiskReason] = []
    notional = intent.count * intent.price
    if not writes_enabled:
        reasons.append(DemoRiskReason.WRITES_DISABLED)
    if not execution_smoke_approved:
        reasons.append(DemoRiskReason.EXPLICIT_SMOKE_APPROVAL_REQUIRED)
    if intent.purpose is not DemoIntentPurpose.EXECUTION_SMOKE:
        reasons.append(DemoRiskReason.SMOKE_ONLY)
    if context.kill_switch:
        reasons.append(DemoRiskReason.KILL_SWITCH)
    if not context.reconciliation_certain:
        reasons.append(DemoRiskReason.RECONCILIATION_UNCERTAIN)
    if not (
        context.account_state_known
        and context.positions_state_known
        and context.open_orders_state_known
        and context.daily_pnl_known
    ):
        reasons.append(DemoRiskReason.REMOTE_RISK_STATE_UNKNOWN)
    if intent.count > limits.max_order_count:
        reasons.append(DemoRiskReason.MAX_ORDER_SIZE)
    if notional > limits.max_order_notional:
        reasons.append(DemoRiskReason.MAX_ORDER_NOTIONAL)
    if context.event_exposure + notional > limits.max_event_exposure:
        reasons.append(DemoRiskReason.MAX_EVENT_EXPOSURE)
    if context.total_exposure + notional > limits.max_total_exposure:
        reasons.append(DemoRiskReason.MAX_TOTAL_EXPOSURE)
    if context.open_positions >= limits.max_concurrent_positions:
        reasons.append(DemoRiskReason.MAX_CONCURRENT_POSITIONS)
    if context.daily_realized_pnl <= -limits.max_daily_loss:
        reasons.append(DemoRiskReason.MAX_DAILY_LOSS)
    unique = tuple(dict.fromkeys(reasons))
    return DemoRiskDecision(not unique, unique)


def _valid_transition(previous: DemoLifecycleState | None, current: DemoLifecycleState) -> bool:
    """Allow idempotent observations and recovery, never a silent lifecycle regression."""

    if previous is None:
        return current in {
            DemoLifecycleState.INTENT,
            DemoLifecycleState.OPEN,
            DemoLifecycleState.PARTIALLY_FILLED,
            DemoLifecycleState.FILLED,
            DemoLifecycleState.CANCELED,
            DemoLifecycleState.REJECTED,
            DemoLifecycleState.RECONCILIATION_REQUIRED,
        }
    if previous is current:
        return True
    allowed: dict[DemoLifecycleState, set[DemoLifecycleState]] = {
        DemoLifecycleState.INTENT: {
            DemoLifecycleState.SUBMITTING,
            DemoLifecycleState.RECONCILIATION_REQUIRED,
        },
        DemoLifecycleState.SUBMITTING: {
            DemoLifecycleState.OPEN,
            DemoLifecycleState.PARTIALLY_FILLED,
            DemoLifecycleState.FILLED,
            DemoLifecycleState.REJECTED,
            DemoLifecycleState.RECONCILIATION_REQUIRED,
        },
        DemoLifecycleState.OPEN: {
            DemoLifecycleState.PARTIALLY_FILLED,
            DemoLifecycleState.FILLED,
            DemoLifecycleState.CANCEL_PENDING,
            DemoLifecycleState.CANCELED,
            DemoLifecycleState.RECONCILIATION_REQUIRED,
        },
        DemoLifecycleState.PARTIALLY_FILLED: {
            DemoLifecycleState.FILLED,
            DemoLifecycleState.CANCEL_PENDING,
            DemoLifecycleState.CANCELED,
            DemoLifecycleState.SETTLED,
            DemoLifecycleState.RECONCILIATION_REQUIRED,
        },
        DemoLifecycleState.FILLED: {
            DemoLifecycleState.SETTLED,
            DemoLifecycleState.RECONCILIATION_REQUIRED,
        },
        DemoLifecycleState.CANCEL_PENDING: {
            DemoLifecycleState.PARTIALLY_FILLED,
            DemoLifecycleState.FILLED,
            DemoLifecycleState.CANCELED,
            DemoLifecycleState.RECONCILIATION_REQUIRED,
        },
        DemoLifecycleState.CANCELED: {
            DemoLifecycleState.SETTLED,
            DemoLifecycleState.RECONCILIATION_REQUIRED,
        },
        DemoLifecycleState.RECONCILIATION_REQUIRED: {
            DemoLifecycleState.OPEN,
            DemoLifecycleState.PARTIALLY_FILLED,
            DemoLifecycleState.FILLED,
            DemoLifecycleState.CANCELED,
            DemoLifecycleState.REJECTED,
        },
        DemoLifecycleState.REJECTED: set(),
        DemoLifecycleState.SETTLED: set(),
    }
    return current in allowed[previous]


_SCHEMA = """
CREATE TABLE demo_metadata(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
CREATE TABLE demo_intents(
    client_order_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    model_artifact_hash TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    count TEXT NOT NULL,
    price TEXT NOT NULL,
    probability TEXT NOT NULL,
    edge TEXT NOT NULL,
    decision_timestamp TEXT NOT NULL,
    purpose TEXT NOT NULL,
    intent_hash TEXT NOT NULL UNIQUE,
    UNIQUE(model_id,event_id,opportunity_id)
) STRICT;
CREATE TABLE demo_risk_facts(
    client_order_id TEXT NOT NULL REFERENCES demo_intents(client_order_id),
    allowed INTEGER NOT NULL CHECK(allowed IN (0,1)),
    reasons_json TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    fact_hash TEXT NOT NULL,
    PRIMARY KEY(client_order_id,fact_hash)
) STRICT;
CREATE TABLE demo_execution_diagnostics(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL REFERENCES demo_intents(client_order_id),
    result_code TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    fact_hash TEXT NOT NULL,
    UNIQUE(client_order_id,result_code,fact_hash)
) STRICT;
CREATE INDEX demo_execution_diagnostics_latest_idx
ON demo_execution_diagnostics(client_order_id,id DESC);
CREATE TABLE demo_order_facts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL REFERENCES demo_intents(client_order_id),
    provider_order_id TEXT,
    state TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    remote_hash TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    UNIQUE(client_order_id,state,remote_hash)
) STRICT;
CREATE INDEX demo_order_facts_latest_idx
ON demo_order_facts(client_order_id,id DESC);
CREATE TABLE demo_fill_facts(
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    count TEXT NOT NULL,
    price TEXT NOT NULL,
    fee TEXT NOT NULL,
    created_time TEXT NOT NULL,
    fact_hash TEXT NOT NULL
) STRICT;
CREATE TABLE demo_position_facts(
    fact_hash TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    quantity TEXT NOT NULL,
    exposure TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    fees TEXT NOT NULL,
    resting_orders INTEGER NOT NULL,
    remote_updated_timestamp TEXT NOT NULL,
    observed_at TEXT NOT NULL
) STRICT;
CREATE TABLE demo_settlement_facts(
    event_id TEXT PRIMARY KEY,
    outcome_yes INTEGER NOT NULL CHECK(outcome_yes IN (0,1)),
    settlement_timestamp TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    fees TEXT NOT NULL,
    fact_hash TEXT NOT NULL UNIQUE
) STRICT;
INSERT INTO demo_metadata(key,value) VALUES('environment','KALSHI_DEMO');
"""

_MIGRATE_V1_TO_V2 = """
CREATE TABLE demo_execution_diagnostics(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL REFERENCES demo_intents(client_order_id),
    result_code TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    fact_hash TEXT NOT NULL,
    UNIQUE(client_order_id,result_code,fact_hash)
) STRICT;
CREATE INDEX demo_execution_diagnostics_latest_idx
ON demo_execution_diagnostics(client_order_id,id DESC);
"""


class DemoExecutionStore:
    """Independent append-only Demo ledger; it never shares Paper or recorder storage."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=10000")
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            existing_tables = self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' LIMIT 1"
            ).fetchone()
            if existing_tables is not None:
                self._connection.close()
                raise DemoExecutionError("database is not an empty Demo ledger path")
            self._connection.executescript(
                "BEGIN IMMEDIATE;" + _SCHEMA + "PRAGMA user_version=2;COMMIT;"
            )
        elif version == 1:
            self._connection.executescript(
                "BEGIN IMMEDIATE;" + _MIGRATE_V1_TO_V2 + "PRAGMA user_version=2;COMMIT;"
            )
        elif version != 2:
            raise DemoExecutionError("unsupported Demo ledger schema version")
        try:
            environment = self._connection.execute(
                "SELECT value FROM demo_metadata WHERE key='environment'"
            ).fetchone()
        except sqlite3.DatabaseError:
            self._connection.close()
            raise DemoExecutionError("database is not a valid KALSHI_DEMO ledger") from None
        if environment != (DemoEnvironment.KALSHI_DEMO.value,):
            self._connection.close()
            raise DemoExecutionError("Demo ledger environment does not match KALSHI_DEMO")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> DemoExecutionStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @staticmethod
    def _intent_payload(intent: DemoIntent) -> dict[str, str]:
        return {
            "client_order_id": stable_client_order_id(intent),
            "model_id": intent.model_id,
            "model_artifact_hash": intent.model_artifact_hash,
            "decision_id": intent.decision_id,
            "event_id": intent.event_id,
            "opportunity_id": intent.opportunity_id,
            "ticker": intent.ticker,
            "side": intent.side.value,
            "count": str(intent.count),
            "price": str(intent.price),
            "probability": str(intent.probability),
            "edge": str(intent.edge),
            "decision_timestamp": _timestamp(intent.decision_timestamp),
            "purpose": intent.purpose.value,
        }

    def append_intent(self, intent: DemoIntent) -> str:
        payload = self._intent_payload(intent)
        fact_hash = _digest(payload)
        client_id = payload["client_order_id"]
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._connection.execute(
                "SELECT intent_hash FROM demo_intents WHERE client_order_id=?", (client_id,)
            ).fetchone()
            if existing is not None:
                if existing != (fact_hash,):
                    raise DemoExecutionError("Demo idempotency key conflicts with immutable intent")
                self._connection.commit()
                return client_id
            self._connection.execute(
                """INSERT INTO demo_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    client_id,
                    intent.model_id,
                    intent.model_artifact_hash,
                    intent.decision_id,
                    intent.event_id,
                    intent.opportunity_id,
                    intent.ticker,
                    intent.side.value,
                    str(intent.count),
                    str(intent.price),
                    str(intent.probability),
                    str(intent.edge),
                    payload["decision_timestamp"],
                    intent.purpose.value,
                    fact_hash,
                ),
            )
            self._connection.commit()
            return client_id
        except BaseException:
            self._connection.rollback()
            raise

    def append_risk(
        self,
        client_order_id: str,
        decision: DemoRiskDecision,
        context: DemoRiskContext,
        policy_hash: str,
    ) -> None:
        context_payload = {
            "event_exposure": str(context.event_exposure),
            "total_exposure": str(context.total_exposure),
            "open_positions": context.open_positions,
            "daily_realized_pnl": str(context.daily_realized_pnl),
            "reconciliation_certain": context.reconciliation_certain,
            "kill_switch": context.kill_switch,
            "account_state_known": context.account_state_known,
            "positions_state_known": context.positions_state_known,
            "open_orders_state_known": context.open_orders_state_known,
            "daily_pnl_known": context.daily_pnl_known,
            "official_buying_power": (
                None
                if context.official_buying_power is None
                else str(context.official_buying_power)
            ),
            "official_exchange_active": context.official_exchange_active,
            "official_trading_active": context.official_trading_active,
            "official_market_ticker": context.official_market_ticker,
            "official_market_status": context.official_market_status,
            "official_truth_received_at": (
                None
                if context.official_truth_received_at is None
                else _timestamp(context.official_truth_received_at)
            ),
        }
        context_hash = _digest(context_payload)
        reasons_json = _canonical([reason.value for reason in decision.reasons])
        fact_hash = _digest(
            {
                "allowed": decision.allowed,
                "reasons": reasons_json,
                "context_hash": context_hash,
                "policy_hash": policy_hash,
            }
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO demo_risk_facts VALUES(?,?,?,?,?,?,?)",
            (
                client_order_id,
                int(decision.allowed),
                reasons_json,
                context_hash,
                policy_hash,
                _timestamp(datetime.now(UTC)),
                fact_hash,
            ),
        )

    def append_execution_diagnostic(
        self,
        client_order_id: str,
        result_code: DemoExecutionResultCode,
        detail: dict[str, object],
        *,
        observed_at: datetime | None = None,
    ) -> None:
        detail_json = _canonical(detail)
        fact_hash = _digest({"result_code": result_code.value, "detail": detail})
        self._connection.execute(
            """INSERT OR IGNORE INTO demo_execution_diagnostics(
                   client_order_id,result_code,detail_json,observed_at,fact_hash
               ) VALUES(?,?,?,?,?)""",
            (
                client_order_id,
                result_code.value,
                detail_json,
                _timestamp(observed_at or datetime.now(UTC)),
                fact_hash,
            ),
        )

    def latest_execution_diagnostic(
        self, client_order_id: str
    ) -> tuple[DemoExecutionResultCode, dict[str, object]] | None:
        row = self._connection.execute(
            """SELECT result_code,detail_json FROM demo_execution_diagnostics
               WHERE client_order_id=? ORDER BY id DESC LIMIT 1""",
            (client_order_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            detail = json.loads(str(row[1]))
        except json.JSONDecodeError:
            raise DemoExecutionError("Demo execution diagnostic is malformed") from None
        if not isinstance(detail, dict):
            raise DemoExecutionError("Demo execution diagnostic is malformed")
        return DemoExecutionResultCode(str(row[0])), detail

    def append_state(
        self,
        client_order_id: str,
        state: DemoLifecycleState,
        *,
        provider_order_id: str | None = None,
        detail: dict[str, object] | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        previous = self.latest_state(client_order_id)
        if not _valid_transition(previous, state):
            raise DemoExecutionError(
                f"illegal Demo lifecycle transition {previous or 'NONE'} -> {state}"
            )
        detail_json = _canonical(detail or {})
        remote_hash = _digest(
            {
                "provider_order_id": provider_order_id,
                "state": state.value,
                "detail": detail or {},
            }
        )
        self._connection.execute(
            """INSERT OR IGNORE INTO demo_order_facts(
                   client_order_id,provider_order_id,state,observed_at,remote_hash,detail_json
               ) VALUES(?,?,?,?,?,?)""",
            (
                client_order_id,
                provider_order_id,
                state.value,
                _timestamp(observed_at or datetime.now(UTC)),
                remote_hash,
                detail_json,
            ),
        )

    def intent_ticker(self, client_order_id: str) -> str:
        row = self._connection.execute(
            "SELECT ticker FROM demo_intents WHERE client_order_id=?", (client_order_id,)
        ).fetchone()
        if row is None:
            raise DemoExecutionError("remote Demo order has no matching local intent")
        return str(row[0])

    def latest_state(self, client_order_id: str) -> DemoLifecycleState | None:
        row = self._connection.execute(
            "SELECT state FROM demo_order_facts WHERE client_order_id=? ORDER BY id DESC LIMIT 1",
            (client_order_id,),
        ).fetchone()
        return None if row is None else DemoLifecycleState(row[0])

    def pending_client_order_ids(self, *, limit: int = 1000) -> tuple[str, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("Demo reconciliation limit must be within 1..1000")
        terminal = (
            DemoLifecycleState.CANCELED.value,
            DemoLifecycleState.REJECTED.value,
            DemoLifecycleState.SETTLED.value,
        )
        rows = self._connection.execute(
            """SELECT intent.client_order_id
               FROM demo_intents AS intent
               LEFT JOIN demo_order_facts AS fact
                 ON fact.id=(SELECT latest.id FROM demo_order_facts AS latest
                             WHERE latest.client_order_id=intent.client_order_id
                             ORDER BY latest.id DESC LIMIT 1)
               WHERE fact.state IS NULL OR fact.state NOT IN (?,?,?)
               ORDER BY intent.rowid
               LIMIT ?""",
            (*terminal, limit),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def append_fill(self, fill: DemoRemoteFill) -> bool:
        payload = {
            "fill_id": fill.fill_id,
            "order_id": fill.order_id,
            "ticker": fill.ticker,
            "count": str(fill.count),
            "price": str(fill.price),
            "fee": str(fill.fee),
            "created_time": fill.created_time,
        }
        fact_hash = _digest(payload)
        existing = self._connection.execute(
            "SELECT fact_hash FROM demo_fill_facts WHERE fill_id=?", (fill.fill_id,)
        ).fetchone()
        if existing is not None:
            if existing != (fact_hash,):
                raise DemoExecutionError("Demo fill ID conflicts with immutable remote truth")
            return False
        self._connection.execute(
            "INSERT INTO demo_fill_facts VALUES(?,?,?,?,?,?,?,?)",
            (
                fill.fill_id,
                fill.order_id,
                fill.ticker,
                str(fill.count),
                str(fill.price),
                str(fill.fee),
                fill.created_time,
                fact_hash,
            ),
        )
        return True

    def append_position(self, position: object, *, observed_at: datetime | None = None) -> bool:
        from live15_quant.providers.kalshi_demo_execution import DemoRemotePosition

        if not isinstance(position, DemoRemotePosition):
            raise TypeError("position must be DemoRemotePosition")
        payload = {
            "ticker": position.ticker,
            "quantity": str(position.quantity),
            "exposure": str(position.exposure),
            "realized_pnl": str(position.realized_pnl),
            "fees": str(position.fees),
            "resting_orders": position.resting_orders,
            "remote_updated_timestamp": position.updated_timestamp,
        }
        fact_hash = _digest(payload)
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO demo_position_facts VALUES(?,?,?,?,?,?,?,?,?)",
            (
                fact_hash,
                position.ticker,
                str(position.quantity),
                str(position.exposure),
                str(position.realized_pnl),
                str(position.fees),
                position.resting_orders,
                position.updated_timestamp,
                _timestamp(observed_at or datetime.now(UTC)),
            ),
        )
        return cursor.rowcount == 1

    def append_settlement(
        self,
        *,
        event_id: str,
        outcome_yes: bool,
        settlement_timestamp: datetime,
        realized_pnl: Decimal,
        fees: Decimal,
    ) -> bool:
        if (
            not event_id
            or settlement_timestamp.tzinfo is None
            or settlement_timestamp.utcoffset() is None
        ):
            raise ValueError("Demo settlement identity/timestamp is invalid")
        if any(not value.is_finite() for value in (realized_pnl, fees)) or fees < 0:
            raise ValueError("Demo settlement PnL/fees are invalid")
        payload = {
            "event_id": event_id,
            "outcome_yes": outcome_yes,
            "settlement_timestamp": _timestamp(settlement_timestamp),
            "realized_pnl": str(realized_pnl),
            "fees": str(fees),
        }
        fact_hash = _digest(payload)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._connection.execute(
                "SELECT fact_hash FROM demo_settlement_facts WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                if existing != (fact_hash,):
                    raise DemoExecutionError(
                        "Demo settlement conflicts with immutable official truth"
                    )
                self._connection.commit()
                return False
            self._connection.execute(
                "INSERT INTO demo_settlement_facts VALUES(?,?,?,?,?,?)",
                (
                    event_id,
                    int(outcome_yes),
                    payload["settlement_timestamp"],
                    str(realized_pnl),
                    str(fees),
                    fact_hash,
                ),
            )
            latest_facts = self._connection.execute(
                """SELECT intent.client_order_id, fact.detail_json
                   FROM demo_intents AS intent
                   JOIN demo_order_facts AS fact
                     ON fact.id=(SELECT latest.id FROM demo_order_facts AS latest
                                 WHERE latest.client_order_id=intent.client_order_id
                                 ORDER BY latest.id DESC LIMIT 1)
                   WHERE intent.event_id=?""",
                (event_id,),
            ).fetchall()
            for client_order_id, detail_json in latest_facts:
                try:
                    filled_count = Decimal(json.loads(str(detail_json)).get("filled_count", "0"))
                except (InvalidOperation, TypeError, ValueError, json.JSONDecodeError):
                    raise DemoExecutionError("Demo order fact has malformed fill detail") from None
                if filled_count <= 0:
                    continue
                self.append_state(
                    str(client_order_id),
                    DemoLifecycleState.SETTLED,
                    detail={
                        "outcome_yes": outcome_yes,
                        "realized_pnl": str(realized_pnl),
                        "fees": str(fees),
                        "settlement_timestamp": payload["settlement_timestamp"],
                    },
                    observed_at=settlement_timestamp,
                )
            self._connection.commit()
            return True
        except BaseException:
            self._connection.rollback()
            raise

    def counts(self) -> dict[str, int]:
        return {
            "intents": int(
                self._connection.execute("SELECT COUNT(*) FROM demo_intents").fetchone()[0]
            ),
            "order_facts": int(
                self._connection.execute("SELECT COUNT(*) FROM demo_order_facts").fetchone()[0]
            ),
            "fills": int(
                self._connection.execute("SELECT COUNT(*) FROM demo_fill_facts").fetchone()[0]
            ),
            "positions": int(
                self._connection.execute("SELECT COUNT(*) FROM demo_position_facts").fetchone()[0]
            ),
            "settlements": int(
                self._connection.execute("SELECT COUNT(*) FROM demo_settlement_facts").fetchone()[0]
            ),
            "execution_diagnostics": int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM demo_execution_diagnostics"
                ).fetchone()[0]
            ),
        }


def _local_state(order: DemoRemoteOrder) -> DemoLifecycleState:
    mapping = {
        DemoRemoteOrderState.OPEN: DemoLifecycleState.OPEN,
        DemoRemoteOrderState.PARTIALLY_FILLED: DemoLifecycleState.PARTIALLY_FILLED,
        DemoRemoteOrderState.FILLED: DemoLifecycleState.FILLED,
        DemoRemoteOrderState.CANCELED: DemoLifecycleState.CANCELED,
        DemoRemoteOrderState.REJECTED: DemoLifecycleState.REJECTED,
        DemoRemoteOrderState.RECONCILIATION_REQUIRED: DemoLifecycleState.RECONCILIATION_REQUIRED,
    }
    return mapping[order.state]


class DemoExecutionCoordinator:
    """Separate model intent, hard risk, remote execution, and append-only reconciliation."""

    _MAX_DECISION_AGE = timedelta(seconds=30)

    def __init__(
        self,
        client: KalshiDemoExecutionClient,
        store: DemoExecutionStore,
        *,
        limits: DemoRiskLimits | None = None,
        sizing: DemoSizingPolicy | None = None,
        quote_source: DemoPreSubmitQuoteSource | None = None,
        price_ev_guard: PreSubmitPriceEVGuard | None = None,
        utc_now: Callable[[], datetime] | None = None,
        writes_enabled: bool = False,
        execution_smoke_approved: bool = False,
    ) -> None:
        self._client = client
        self._store = store
        self._limits = limits or DemoRiskLimits()
        self._sizing = sizing or DemoSizingPolicy()
        client_quote_source = getattr(client, "latest_quote", None)
        self._quote_source = (
            quote_source
            if quote_source is not None
            else cast(DemoPreSubmitQuoteSource, client)
            if callable(client_quote_source)
            else None
        )
        self._price_ev_guard = price_ev_guard or PreSubmitPriceEVGuard()
        self._utc_now = utc_now or (lambda: datetime.now(UTC))
        self._writes_enabled = writes_enabled
        self._execution_smoke_approved = execution_smoke_approved

    def submit(
        self, intent: DemoIntent, context: DemoRiskContext
    ) -> DemoRiskDecision | DemoReconciliationResult:
        client_id = self._store.append_intent(intent)
        effective_context = context
        risk = evaluate_demo_risk(
            intent,
            effective_context,
            self._limits,
            writes_enabled=self._writes_enabled,
            execution_smoke_approved=self._execution_smoke_approved,
        )
        if intent.count != self._sizing.fixed_order_count:
            risk = DemoRiskDecision(
                False,
                tuple(dict.fromkeys((*risk.reasons, DemoRiskReason.FIXED_SIZING_POLICY))),
            )
        policy_hash = _digest(
            {
                "limits": {
                    "max_order_count": str(self._limits.max_order_count),
                    "max_order_notional": str(self._limits.max_order_notional),
                    "max_event_exposure": str(self._limits.max_event_exposure),
                    "max_total_exposure": str(self._limits.max_total_exposure),
                    "max_concurrent_positions": self._limits.max_concurrent_positions,
                    "max_daily_loss": str(self._limits.max_daily_loss),
                },
                "sizing": {
                    "mode": self._sizing.mode.value,
                    "fixed_order_count": str(self._sizing.fixed_order_count),
                    "equity_sizing_enabled": self._sizing.equity_sizing_enabled,
                },
                "writes_enabled": self._writes_enabled,
                "execution_smoke_approved": self._execution_smoke_approved,
                "price_ev_guard": {
                    "max_quote_age_seconds": (
                        self._price_ev_guard.policy.max_quote_age.total_seconds()
                    ),
                    "max_adverse_price_move": str(
                        self._price_ev_guard.policy.max_adverse_price_move
                    ),
                    "safety_margin": str(self._price_ev_guard.policy.safety_margin),
                    "minimum_required_edge": str(self._price_ev_guard.policy.minimum_required_edge),
                },
            }
        )
        pre_remote_blockers = {
            DemoRiskReason.WRITES_DISABLED,
            DemoRiskReason.EXPLICIT_SMOKE_APPROVAL_REQUIRED,
            DemoRiskReason.SMOKE_ONLY,
            DemoRiskReason.KILL_SWITCH,
            DemoRiskReason.MAX_ORDER_SIZE,
            DemoRiskReason.MAX_ORDER_NOTIONAL,
            DemoRiskReason.FIXED_SIZING_POLICY,
        }
        if any(reason in pre_remote_blockers for reason in risk.reasons):
            self._store.append_risk(client_id, risk, effective_context, policy_hash)
            return risk
        existing = self._client.find_order_by_client_id(client_id)
        if existing is not None:
            self._store.append_risk(client_id, risk, effective_context, policy_hash)
            return self._record_remote(existing)
        current = self._store.latest_state(client_id)
        if current in {
            DemoLifecycleState.FILLED,
            DemoLifecycleState.CANCELED,
            DemoLifecycleState.REJECTED,
            DemoLifecycleState.SETTLED,
        }:
            self._store.append_risk(client_id, risk, effective_context, policy_hash)
            return DemoReconciliationResult(client_id, current, None, 0)
        if current in {
            DemoLifecycleState.SUBMITTING,
            DemoLifecycleState.CANCEL_PENDING,
            DemoLifecycleState.RECONCILIATION_REQUIRED,
        }:
            self._store.append_risk(client_id, risk, effective_context, policy_hash)
            self._store.append_state(
                client_id,
                DemoLifecycleState.RECONCILIATION_REQUIRED,
                detail={"reason": "remote_order_not_found_after_uncertain_write"},
            )
            return DemoReconciliationResult(
                client_id, DemoLifecycleState.RECONCILIATION_REQUIRED, None, 0
            )
        guard_result: DemoPreSubmitGuardResult | None = None
        submit_intent = intent
        try:
            exchange = self._client.exchange_status()
            market = self._client.market(intent.ticker)
            account = self._client.balance()
            positions = self._client.positions()
            remote_orders = self._client.orders()
            self._client.fills()
        except KalshiDemoExecutionError:
            risk = DemoRiskDecision(False, (DemoRiskReason.OFFICIAL_TRUTH_UNAVAILABLE,))
        else:
            if market.ticker != intent.ticker:
                raise DemoExecutionError(
                    "official Demo market identity conflicts with immutable intent"
                )
            reasons: list[DemoRiskReason] = []
            if not exchange.exchange_active or not exchange.trading_active:
                reasons.append(DemoRiskReason.EXCHANGE_UNAVAILABLE)
            if (
                market.status.lower() != "active"
                or market.result not in {None, ""}
                or (market.close_time is not None and market.close_time <= market.received_at)
            ):
                reasons.append(DemoRiskReason.MARKET_NOT_TRADEABLE)
            open_orders = tuple(
                order
                for order in remote_orders
                if order.state in {DemoRemoteOrderState.OPEN, DemoRemoteOrderState.PARTIALLY_FILLED}
            )
            remote_uncertain = any(
                order.state is DemoRemoteOrderState.RECONCILIATION_REQUIRED
                for order in remote_orders
            )
            position_exposure = sum((position.exposure for position in positions), Decimal(0))
            order_exposure = sum(
                (order.remaining_count * order.price for order in open_orders), Decimal(0)
            )
            event_exposure = sum(
                (position.exposure for position in positions if position.ticker == intent.ticker),
                Decimal(0),
            ) + sum(
                (
                    order.remaining_count * order.price
                    for order in open_orders
                    if order.ticker == intent.ticker
                ),
                Decimal(0),
            )
            official_received_at = max(exchange.received_at, market.received_at)
            effective_context = replace(
                context,
                event_exposure=event_exposure,
                total_exposure=position_exposure + order_exposure,
                open_positions=sum(position.quantity != 0 for position in positions),
                reconciliation_certain=context.reconciliation_certain and not remote_uncertain,
                account_state_known=True,
                positions_state_known=True,
                open_orders_state_known=True,
                official_buying_power=account.buying_power,
                official_exchange_active=exchange.exchange_active,
                official_trading_active=exchange.trading_active,
                official_market_ticker=market.ticker,
                official_market_status=market.status,
                official_truth_received_at=official_received_at,
            )
            quote = (
                None
                if self._quote_source is None
                else self._quote_source.latest_quote(intent.ticker)
            )
            unavailable_reason = None
            if quote is None and self._quote_source is not None:
                reason_reader = getattr(self._quote_source, "last_unavailable_reason", None)
                if callable(reason_reader):
                    unavailable_reason = reason_reader(intent.ticker)
            guard_result = self._price_ev_guard.evaluate(
                intent,
                quote,
                evaluated_at=self._utc_now(),
                data_unavailable_reason=unavailable_reason,
            )
            guard_result = replace(guard_result, pre_submit_ready_at=self._utc_now())
            self._store.append_execution_diagnostic(
                client_id,
                guard_result.code,
                guard_result.diagnostics(),
            )
            if guard_result.allowed:
                assert guard_result.pre_submit_price is not None
                submit_intent = replace(intent, price=guard_result.pre_submit_price)
            risk = evaluate_demo_risk(
                submit_intent,
                effective_context,
                self._limits,
                writes_enabled=self._writes_enabled,
                execution_smoke_approved=self._execution_smoke_approved,
            )
            if account.buying_power < submit_intent.count * submit_intent.price:
                risk = DemoRiskDecision(
                    False,
                    tuple(dict.fromkeys((*risk.reasons, DemoRiskReason.INSUFFICIENT_BUYING_POWER))),
                )
            truth_age = official_received_at - intent.decision_timestamp
            if truth_age < timedelta(0):
                risk = DemoRiskDecision(
                    False,
                    tuple(dict.fromkeys((*risk.reasons, DemoRiskReason.CLOCK_UNCERTAIN))),
                )
            elif truth_age > self._MAX_DECISION_AGE:
                risk = DemoRiskDecision(
                    False,
                    tuple(dict.fromkeys((*risk.reasons, DemoRiskReason.DECISION_STALE))),
                )
            if reasons:
                risk = DemoRiskDecision(False, tuple(dict.fromkeys((*risk.reasons, *reasons))))
            if not guard_result.allowed:
                guard_reason = {
                    DemoExecutionResultCode.DATA_UNAVAILABLE: (
                        DemoRiskReason.PRE_SUBMIT_DATA_UNAVAILABLE
                    ),
                    DemoExecutionResultCode.PRICE_MOVED_TOO_FAR: (
                        DemoRiskReason.PRICE_MOVED_TOO_FAR
                    ),
                    DemoExecutionResultCode.EDGE_DECAYED_BEFORE_SUBMIT: (
                        DemoRiskReason.EDGE_DECAYED_BEFORE_SUBMIT
                    ),
                }[guard_result.code]
                risk = DemoRiskDecision(
                    False,
                    tuple(dict.fromkeys((*risk.reasons, guard_reason))),
                )
        self._store.append_risk(client_id, risk, effective_context, policy_hash)
        if not risk.allowed:
            return guard_result if guard_result is not None and not guard_result.allowed else risk
        self._store.append_state(client_id, DemoLifecycleState.INTENT)
        self._store.append_state(client_id, DemoLifecycleState.SUBMITTING)
        try:
            assert guard_result is not None and guard_result.submitted_limit is not None
            remote = self._client.create_order(
                DemoOrderRequest(
                    ticker=intent.ticker,
                    client_order_id=client_id,
                    side=intent.side,
                    count=intent.count,
                    price=guard_result.submitted_limit,
                )
            )
        except KalshiDemoWriteRejectedError as error:
            detail = {
                "reason": "submit_rejected",
                "reason_code": error.reason_code,
                **error.diagnostic,
            }
            self._store.append_execution_diagnostic(
                client_id,
                {
                    "http_400": DemoExecutionResultCode.HTTP_400,
                    "http_401": DemoExecutionResultCode.HTTP_401,
                    "http_403": DemoExecutionResultCode.HTTP_403,
                    "http_404": DemoExecutionResultCode.HTTP_404,
                    "http_422": DemoExecutionResultCode.HTTP_422,
                }.get(error.reason_code, DemoExecutionResultCode.HTTP_4XX),
                detail,
            )
            self._store.append_state(
                client_id,
                DemoLifecycleState.REJECTED,
                detail=detail,
            )
            return DemoReconciliationResult(client_id, DemoLifecycleState.REJECTED, None, 0)
        except KalshiDemoAmbiguousWriteError as error:
            result_code = {
                "http_404": DemoExecutionResultCode.HTTP_404,
                "transport_failure": DemoExecutionResultCode.TRANSPORT_ERROR,
                "http_409": DemoExecutionResultCode.HTTP_409,
                "http_429": DemoExecutionResultCode.HTTP_429,
                "malformed_response": DemoExecutionResultCode.MALFORMED_ACK,
                "compact_ack_invalid": DemoExecutionResultCode.MALFORMED_ACK,
            }.get(error.reason_code)
            if result_code is None:
                result_code = (
                    DemoExecutionResultCode.HTTP_5XX
                    if error.reason_code.startswith("http_5")
                    else DemoExecutionResultCode.RECONCILIATION_REQUIRED
                )
            self._store.append_execution_diagnostic(
                client_id,
                result_code,
                {"reason_code": error.reason_code, **error.diagnostic},
            )
            self._store.append_state(
                client_id,
                DemoLifecycleState.RECONCILIATION_REQUIRED,
                detail={
                    "reason": "submit_outcome_ambiguous",
                    "reason_code": error.reason_code,
                    **error.diagnostic,
                },
            )
            return DemoReconciliationResult(
                client_id, DemoLifecycleState.RECONCILIATION_REQUIRED, None, 0
            )
        if (
            remote.client_order_id != client_id
            or remote.ticker != intent.ticker
            or remote.initial_count != intent.count
            or remote.price != guard_result.pre_submit_price
        ):
            raise DemoExecutionError("Demo create ACK conflicts with submitted order identity")
        result_code = (
            DemoExecutionResultCode.HTTP_SUCCESS_FILLED
            if remote.filled_count > 0
            else DemoExecutionResultCode.HTTP_SUCCESS_NO_FILL
        )
        self._store.append_execution_diagnostic(
            client_id,
            result_code,
            {
                "provider_order_id": remote.order_id,
                "fill_count": str(remote.filled_count),
                "remaining_count": str(remote.remaining_count),
                "submitted_limit": str(guard_result.submitted_limit),
            },
        )
        return self._record_remote(remote)

    def _record_remote(self, remote: DemoRemoteOrder) -> DemoReconciliationResult:
        if self._store.intent_ticker(remote.client_order_id) != remote.ticker:
            raise DemoExecutionError("remote Demo order ticker conflicts with immutable intent")
        state = _local_state(remote)
        detail = {
            "ticker": remote.ticker,
            "initial_count": str(remote.initial_count),
            "filled_count": str(remote.filled_count),
            "remaining_count": str(remote.remaining_count),
            "price": str(remote.price),
            "fees": str(remote.fees),
            "remote_status": remote.raw_status,
        }
        self._store.append_state(
            remote.client_order_id,
            state,
            provider_order_id=remote.order_id,
            detail=detail,
        )
        inserted = sum(
            self._store.append_fill(fill) for fill in self._client.fills(order_id=remote.order_id)
        )
        return DemoReconciliationResult(remote.client_order_id, state, remote.order_id, inserted)

    def reconcile(self, client_order_id: str) -> DemoReconciliationResult:
        remote = self._client.find_order_by_client_id(client_order_id)
        if remote is None:
            self._store.append_state(
                client_order_id,
                DemoLifecycleState.RECONCILIATION_REQUIRED,
                detail={"reason": "remote_order_not_found"},
            )
            return DemoReconciliationResult(
                client_order_id, DemoLifecycleState.RECONCILIATION_REQUIRED, None, 0
            )
        return self._record_remote(remote)

    def reconcile_positions(self) -> int:
        return sum(self._store.append_position(position) for position in self._client.positions())

    def reconcile_pending(self, *, limit: int = 1000) -> tuple[DemoReconciliationResult, ...]:
        pending = self._store.pending_client_order_ids(limit=limit)
        if not pending:
            return ()
        remote_by_client: dict[str, DemoRemoteOrder] = {}
        for remote in self._client.orders():
            if remote.client_order_id in remote_by_client:
                raise DemoExecutionError("duplicate remote client order ID")
            remote_by_client[remote.client_order_id] = remote
        results: list[DemoReconciliationResult] = []
        for client_order_id in pending:
            remote = remote_by_client.get(client_order_id)
            if remote is None:
                self._store.append_state(
                    client_order_id,
                    DemoLifecycleState.RECONCILIATION_REQUIRED,
                    detail={"reason": "remote_order_not_found"},
                )
                results.append(
                    DemoReconciliationResult(
                        client_order_id,
                        DemoLifecycleState.RECONCILIATION_REQUIRED,
                        None,
                        0,
                    )
                )
            else:
                results.append(self._record_remote(remote))
        self.reconcile_positions()
        return tuple(results)

    def cancel(self, client_order_id: str, provider_order_id: str) -> DemoReconciliationResult:
        if not (self._writes_enabled and self._execution_smoke_approved):
            raise DemoExecutionError("Demo writes are disabled")
        remote = self._client.order(provider_order_id)
        if remote.client_order_id != client_order_id:
            raise DemoExecutionError("remote order identity conflicts with Demo ledger")
        if self._store.latest_state(client_order_id) in {
            None,
            DemoLifecycleState.RECONCILIATION_REQUIRED,
        }:
            self._record_remote(remote)
        if remote.state in {
            DemoRemoteOrderState.CANCELED,
            DemoRemoteOrderState.FILLED,
            DemoRemoteOrderState.REJECTED,
        }:
            return self._record_remote(remote)
        self._store.append_state(
            client_order_id,
            DemoLifecycleState.CANCEL_PENDING,
            provider_order_id=provider_order_id,
        )
        try:
            self._client.cancel_order(provider_order_id)
        except KalshiDemoAmbiguousWriteError as error:
            self._store.append_state(
                client_order_id,
                DemoLifecycleState.RECONCILIATION_REQUIRED,
                provider_order_id=provider_order_id,
                detail={
                    "reason": "cancel_outcome_ambiguous",
                    "reason_code": error.reason_code,
                },
            )
            return DemoReconciliationResult(
                client_order_id,
                DemoLifecycleState.RECONCILIATION_REQUIRED,
                provider_order_id,
                0,
            )
        return self._record_remote(self._client.order(provider_order_id))
