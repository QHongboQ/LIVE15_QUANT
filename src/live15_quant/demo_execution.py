"""Fail-closed Kalshi Demo order intent, risk, and reconciliation infrastructure."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

from live15_quant.providers.kalshi_demo_execution import (
    DemoBookSide,
    DemoOrderRequest,
    DemoRemoteFill,
    DemoRemoteOrder,
    DemoRemoteOrderState,
    KalshiDemoAmbiguousWriteError,
    KalshiDemoExecutionClient,
    KalshiDemoExecutionError,
)


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
                "BEGIN IMMEDIATE;" + _SCHEMA + "PRAGMA user_version=1;COMMIT;"
            )
        elif version != 1:
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
        writes_enabled: bool = False,
        execution_smoke_approved: bool = False,
    ) -> None:
        self._client = client
        self._store = store
        self._limits = limits or DemoRiskLimits()
        self._sizing = sizing or DemoSizingPolicy()
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
            risk = evaluate_demo_risk(
                intent,
                effective_context,
                self._limits,
                writes_enabled=self._writes_enabled,
                execution_smoke_approved=self._execution_smoke_approved,
            )
            if account.buying_power < intent.count * intent.price:
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
        self._store.append_risk(client_id, risk, effective_context, policy_hash)
        if not risk.allowed:
            return risk
        self._store.append_state(client_id, DemoLifecycleState.INTENT)
        self._store.append_state(client_id, DemoLifecycleState.SUBMITTING)
        try:
            remote = self._client.create_order(
                DemoOrderRequest(
                    ticker=intent.ticker,
                    client_order_id=client_id,
                    side=intent.side,
                    count=intent.count,
                    price=intent.price,
                )
            )
        except KalshiDemoAmbiguousWriteError as error:
            self._store.append_state(
                client_id,
                DemoLifecycleState.RECONCILIATION_REQUIRED,
                detail={
                    "reason": "submit_outcome_ambiguous",
                    "reason_code": error.reason_code,
                },
            )
            return DemoReconciliationResult(
                client_id, DemoLifecycleState.RECONCILIATION_REQUIRED, None, 0
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
