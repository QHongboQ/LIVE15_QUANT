"""Independent append-oriented SQLite ledger for local paper execution."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from live15_quant.execution import ContractOutcome, ExecutionAction, ExecutionOrderState
from live15_quant.models import Asset
from live15_quant.paper import (
    PaperExecutionReason,
    PaperExecutionResult,
    PaperPortfolioState,
    StrategyDecision,
)
from live15_quant.risk import RiskDecision

PAPER_SCHEMA_VERSION = 1


class PaperStorageError(RuntimeError):
    pass


def _ts(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("paper timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _parse_ts(value: object) -> datetime:
    if not isinstance(value, str):
        raise PaperStorageError("malformed paper timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise PaperStorageError("malformed paper timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PaperStorageError("malformed paper timestamp")
    return parsed.astimezone(UTC)


def _parse_decimal(value: object, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise PaperStorageError("malformed paper decimal")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise PaperStorageError("malformed paper decimal") from error
    if not parsed.is_finite():
        raise PaperStorageError("malformed paper decimal")
    return parsed


def _required_decimal(value: object) -> Decimal:
    return cast(Decimal, _parse_decimal(value))


@dataclass(frozen=True, slots=True)
class PaperOrderRecord:
    row_id: int
    order_id: str
    decision_id: str
    signal_timestamp: datetime
    submit_timestamp: datetime
    quote_timestamp: datetime | None
    asset: Asset
    event_id: str
    contract_id: str
    decision: str
    outcome: ContractOutcome
    requested_quantity: Decimal
    requested_price: Decimal
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    fees: Decimal
    state: ExecutionOrderState
    reason: PaperExecutionReason
    venue_ticker: str | None
    quote_source: str | None


@dataclass(frozen=True, slots=True)
class PaperFillRecord:
    row_id: int
    fill_id: str
    order_id: str
    asset: Asset
    event_id: str
    contract_id: str
    fill_timestamp: datetime
    outcome: ContractOutcome
    action: ExecutionAction
    quantity: Decimal
    price: Decimal
    spread: Decimal
    slippage: Decimal
    trade_fee: Decimal
    rounding_fee: Decimal
    rebate: Decimal
    net_fee: Decimal
    fee_assumption: str


@dataclass(frozen=True, slots=True)
class PaperPortfolioRecord:
    row_id: int
    snapshot_timestamp: datetime
    account_id: str
    cash: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_paid: Decimal
    total_exposure: Decimal
    daily_realized_pnl: Decimal
    daily_pnl: Decimal
    consecutive_losses: int
    fill_state_certain: bool


_SCHEMA = """
CREATE TABLE paper_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE paper_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    signal_timestamp TEXT NOT NULL,
    asset TEXT NOT NULL,
    event_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    outcome TEXT,
    quantity TEXT NOT NULL,
    limit_price TEXT,
    time_in_force TEXT NOT NULL,
    target_order_id TEXT,
    data_role TEXT NOT NULL
) STRICT;

CREATE TABLE paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    order_id TEXT NOT NULL UNIQUE,
    decision_id TEXT NOT NULL UNIQUE,
    signal_timestamp TEXT NOT NULL,
    submit_timestamp TEXT NOT NULL,
    quote_timestamp TEXT,
    asset TEXT NOT NULL,
    event_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    outcome TEXT NOT NULL,
    requested_quantity TEXT NOT NULL,
    requested_price TEXT NOT NULL,
    time_in_force TEXT NOT NULL,
    venue_ticker TEXT,
    quote_source TEXT,
    data_role TEXT NOT NULL,
    FOREIGN KEY(decision_id) REFERENCES paper_decisions(decision_id)
) STRICT;

CREATE INDEX idx_paper_orders_event ON paper_orders(event_id, submit_timestamp, id);

CREATE TABLE paper_order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    order_id TEXT NOT NULL,
    event_timestamp TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT NOT NULL,
    filled_quantity TEXT NOT NULL,
    average_fill_price TEXT,
    fees TEXT NOT NULL,
    UNIQUE(order_id, event_timestamp, state, reason, filled_quantity),
    FOREIGN KEY(order_id) REFERENCES paper_orders(order_id)
) STRICT;

CREATE INDEX idx_paper_order_events ON paper_order_events(order_id, event_timestamp, id);

CREATE TABLE paper_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    fill_id TEXT NOT NULL UNIQUE,
    order_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    event_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    fill_timestamp TEXT NOT NULL,
    outcome TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity TEXT NOT NULL,
    price TEXT NOT NULL,
    spread TEXT NOT NULL,
    slippage TEXT NOT NULL,
    trade_fee TEXT NOT NULL,
    rounding_fee TEXT NOT NULL,
    rebate TEXT NOT NULL,
    net_fee TEXT NOT NULL,
    fee_assumption TEXT NOT NULL,
    data_role TEXT NOT NULL,
    FOREIGN KEY(order_id) REFERENCES paper_orders(order_id)
) STRICT;

CREATE INDEX idx_paper_fills_event ON paper_fills(event_id, fill_timestamp, id);

CREATE TABLE paper_risk_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    evaluated_timestamp TEXT NOT NULL,
    allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
    reasons TEXT NOT NULL,
    data_role TEXT NOT NULL,
    FOREIGN KEY(decision_id) REFERENCES paper_decisions(decision_id)
) STRICT;

CREATE TABLE paper_position_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    snapshot_timestamp TEXT NOT NULL,
    event_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    outcome TEXT NOT NULL,
    quantity TEXT NOT NULL,
    average_cost TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    fees_paid TEXT NOT NULL,
    status TEXT NOT NULL,
    data_role TEXT NOT NULL
) STRICT;

CREATE TABLE paper_portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    snapshot_timestamp TEXT NOT NULL,
    account_id TEXT NOT NULL,
    cash TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL,
    fees_paid TEXT NOT NULL,
    total_exposure TEXT NOT NULL,
    daily_realized_pnl TEXT NOT NULL,
    daily_pnl TEXT NOT NULL,
    consecutive_losses INTEGER NOT NULL,
    fill_state_certain INTEGER NOT NULL CHECK (fill_state_certain IN (0, 1)),
    data_role TEXT NOT NULL
) STRICT;
"""


class PaperStore:
    """Paper-only ledger; it never opens or mutates the raw recorder database."""

    def __init__(self, path: Path, *, account_id: str, starting_cash: Decimal) -> None:
        if not account_id or not starting_cash.is_finite() or starting_cash < 0:
            raise ValueError("invalid paper account configuration")
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA wal_autocheckpoint=1000")
            self._connection.execute("PRAGMA journal_size_limit=67108864")
            self._initialize(account_id, starting_cash)
        except Exception:
            self._connection.close()
            raise

    def _initialize(self, account_id: str, starting_cash: Decimal) -> None:
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_metadata'"
        ).fetchone()
        if exists is None:
            raw_store = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recorder_metadata'"
            ).fetchone()
            if raw_store is not None:
                raise PaperStorageError("paper ledger cannot share the raw recorder database")
            try:
                self._connection.executescript(_SCHEMA)
                self._connection.executemany(
                    "INSERT INTO paper_metadata(key,value) VALUES (?,?)",
                    (
                        ("schema_version", str(PAPER_SCHEMA_VERSION)),
                        ("account_id", account_id),
                        ("starting_cash", str(starting_cash)),
                    ),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            return
        metadata = {
            row["key"]: row["value"]
            for row in self._connection.execute("SELECT key,value FROM paper_metadata")
        }
        if metadata.get("schema_version") != str(PAPER_SCHEMA_VERSION):
            raise PaperStorageError("incompatible paper schema")
        if metadata.get("account_id") != account_id or metadata.get("starting_cash") != str(
            starting_cash
        ):
            raise PaperStorageError("paper account configuration does not match existing ledger")

    def decision_exists(self, decision_id: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM paper_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
            is not None
        )

    def decision_matches(self, decision: StrategyDecision) -> bool:
        row = self._connection.execute(
            "SELECT * FROM paper_decisions WHERE decision_id=?", (decision.decision_id,)
        ).fetchone()
        if row is None:
            return False
        return (
            row["signal_timestamp"] == _ts(decision.signal_timestamp)
            and row["asset"] == decision.asset.value
            and row["event_id"] == decision.event_id
            and row["contract_id"] == decision.contract_id
            and row["decision"] == decision.decision.value
            and row["outcome"] == (None if decision.outcome is None else decision.outcome.value)
            and row["quantity"] == str(decision.quantity)
            and row["limit_price"] == _decimal(decision.limit_price)
            and row["time_in_force"] == decision.time_in_force.value
            and row["target_order_id"] == decision.target_order_id
        )

    def append_decision(self, decision: StrategyDecision) -> None:
        self._insert_decision(decision)
        self._connection.commit()

    def _insert_decision(self, decision: StrategyDecision) -> None:
        self._connection.execute(
            """
            INSERT INTO paper_decisions (
                schema_version,decision_id,signal_timestamp,asset,event_id,contract_id,
                decision,outcome,quantity,limit_price,time_in_force,target_order_id,data_role
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                PAPER_SCHEMA_VERSION,
                decision.decision_id,
                _ts(decision.signal_timestamp),
                decision.asset.value,
                decision.event_id,
                decision.contract_id,
                decision.decision.value,
                None if decision.outcome is None else decision.outcome.value,
                str(decision.quantity),
                _decimal(decision.limit_price),
                decision.time_in_force.value,
                decision.target_order_id,
                decision.role.value,
            ),
        )

    def append_execution(
        self, result: PaperExecutionResult, risk: RiskDecision | None
    ) -> tuple[str, ...]:
        decision = result.decision
        inserted_fills: list[str] = []
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._insert_decision(decision)
            if risk is not None:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO paper_risk_decisions
                    (schema_version,decision_id,evaluated_timestamp,allowed,reasons,data_role)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        PAPER_SCHEMA_VERSION,
                        decision.decision_id,
                        _ts(result.submit_timestamp),
                        int(risk.allowed),
                        json.dumps([item.value for item in risk.reasons], separators=(",", ":")),
                        decision.role.value,
                    ),
                )
            if (
                result.order_id is not None
                and decision.outcome is not None
                and decision.limit_price is not None
            ):
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO paper_orders (
                        schema_version,order_id,decision_id,signal_timestamp,submit_timestamp,
                        quote_timestamp,asset,event_id,contract_id,decision,outcome,
                        requested_quantity,requested_price,time_in_force,venue_ticker,
                        quote_source,data_role
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        PAPER_SCHEMA_VERSION,
                        result.order_id,
                        decision.decision_id,
                        _ts(decision.signal_timestamp),
                        _ts(result.submit_timestamp),
                        None if result.quote_timestamp is None else _ts(result.quote_timestamp),
                        decision.asset.value,
                        decision.event_id,
                        decision.contract_id,
                        decision.decision.value,
                        decision.outcome.value,
                        str(result.requested_quantity),
                        str(decision.limit_price),
                        decision.time_in_force.value,
                        result.venue_ticker,
                        result.quote_source,
                        decision.role.value,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO paper_order_events (
                        schema_version,order_id,event_timestamp,state,reason,filled_quantity,
                        average_fill_price,fees
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        PAPER_SCHEMA_VERSION,
                        result.order_id,
                        _ts(result.submit_timestamp),
                        result.state.value,
                        result.reason.value,
                        str(result.filled_quantity),
                        _decimal(result.average_fill_price),
                        str(result.fees),
                    ),
                )
            for fill in result.fills:
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO paper_fills (
                        schema_version,fill_id,order_id,asset,event_id,contract_id,fill_timestamp,
                        outcome,action,quantity,price,spread,slippage,trade_fee,rounding_fee,
                        rebate,net_fee,fee_assumption,data_role
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        PAPER_SCHEMA_VERSION,
                        fill.fill_id,
                        fill.order_id,
                        fill.asset.value,
                        decision.event_id,
                        decision.contract_id,
                        _ts(fill.fill_timestamp),
                        fill.outcome.value,
                        fill.action.value,
                        str(fill.quantity),
                        str(fill.price),
                        str(fill.spread),
                        str(fill.slippage),
                        str(fill.fee.trade_fee),
                        str(fill.fee.rounding_fee),
                        str(fill.fee.rebate),
                        str(fill.fee.net_fee),
                        fill.fee.assumption,
                        decision.role.value,
                    ),
                )
                if cursor.rowcount == 1:
                    inserted_fills.append(fill.fill_id)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return tuple(inserted_fills)

    def append_order_event(
        self,
        *,
        order_id: str,
        timestamp: datetime,
        state: ExecutionOrderState,
        reason: PaperExecutionReason,
        filled_quantity: Decimal,
        average_fill_price: Decimal | None = None,
        fees: Decimal = Decimal(0),
    ) -> None:
        self._connection.execute(
            """INSERT OR IGNORE INTO paper_order_events
            (schema_version,order_id,event_timestamp,state,reason,filled_quantity,average_fill_price,fees)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                PAPER_SCHEMA_VERSION,
                order_id,
                _ts(timestamp),
                state.value,
                reason.value,
                str(filled_quantity),
                _decimal(average_fill_price),
                str(fees),
            ),
        )
        self._connection.commit()

    def append_portfolio(self, state: PaperPortfolioState) -> None:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            for position in state.positions:
                self._connection.execute(
                    """INSERT INTO paper_position_snapshots VALUES
                    (NULL,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        PAPER_SCHEMA_VERSION,
                        _ts(state.as_of),
                        position.event_id,
                        position.contract_id,
                        position.asset.value,
                        position.outcome.value,
                        str(position.quantity),
                        str(position.average_cost),
                        str(position.realized_pnl),
                        str(position.fees_paid),
                        position.status.value,
                        "paper_execution",
                    ),
                )
            self._connection.execute(
                """INSERT INTO paper_portfolio_snapshots VALUES
                (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    PAPER_SCHEMA_VERSION,
                    _ts(state.as_of),
                    state.account_id,
                    str(state.cash),
                    str(state.realized_pnl),
                    str(state.unrealized_pnl),
                    str(state.fees_paid),
                    str(state.total_exposure),
                    str(state.daily_realized_pnl),
                    str(state.daily_pnl),
                    state.consecutive_losses,
                    int(state.fill_state_certain),
                    "paper_execution",
                ),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def replay_fills(self) -> Iterator[PaperFillRecord]:
        for row in self._connection.execute("SELECT * FROM paper_fills ORDER BY fill_timestamp,id"):
            try:
                yield PaperFillRecord(
                    row_id=row["id"],
                    fill_id=row["fill_id"],
                    order_id=row["order_id"],
                    asset=Asset(row["asset"]),
                    event_id=row["event_id"],
                    contract_id=row["contract_id"],
                    fill_timestamp=_parse_ts(row["fill_timestamp"]),
                    outcome=ContractOutcome(row["outcome"]),
                    action=ExecutionAction(row["action"]),
                    quantity=_required_decimal(row["quantity"]),
                    price=_required_decimal(row["price"]),
                    spread=_required_decimal(row["spread"]),
                    slippage=_required_decimal(row["slippage"]),
                    trade_fee=_required_decimal(row["trade_fee"]),
                    rounding_fee=_required_decimal(row["rounding_fee"]),
                    rebate=_required_decimal(row["rebate"]),
                    net_fee=_required_decimal(row["net_fee"]),
                    fee_assumption=row["fee_assumption"],
                )
            except (ValueError, TypeError) as error:
                raise PaperStorageError("malformed paper fill") from error

    def latest_order(self, order_id: str) -> PaperOrderRecord | None:
        row = self._connection.execute(
            """
            SELECT orders.*,events.state,events.reason,events.filled_quantity,
                   events.average_fill_price,events.fees
            FROM paper_orders AS orders
            JOIN paper_order_events AS events ON events.order_id=orders.order_id
            WHERE orders.order_id=? ORDER BY events.event_timestamp DESC,events.id DESC LIMIT 1
            """,
            (order_id,),
        ).fetchone()
        if row is None:
            return None
        return PaperOrderRecord(
            row_id=row["id"],
            order_id=row["order_id"],
            decision_id=row["decision_id"],
            signal_timestamp=_parse_ts(row["signal_timestamp"]),
            submit_timestamp=_parse_ts(row["submit_timestamp"]),
            quote_timestamp=None
            if row["quote_timestamp"] is None
            else _parse_ts(row["quote_timestamp"]),
            asset=Asset(row["asset"]),
            event_id=row["event_id"],
            contract_id=row["contract_id"],
            decision=row["decision"],
            outcome=ContractOutcome(row["outcome"]),
            requested_quantity=_required_decimal(row["requested_quantity"]),
            requested_price=_required_decimal(row["requested_price"]),
            filled_quantity=_required_decimal(row["filled_quantity"]),
            average_fill_price=_parse_decimal(row["average_fill_price"], optional=True),
            fees=_required_decimal(row["fees"]),
            state=ExecutionOrderState(row["state"]),
            reason=PaperExecutionReason(row["reason"]),
            venue_ticker=row["venue_ticker"],
            quote_source=row["quote_source"],
        )

    def order_for_decision(self, decision_id: str) -> PaperOrderRecord | None:
        row = self._connection.execute(
            "SELECT order_id FROM paper_orders WHERE decision_id=?", (decision_id,)
        ).fetchone()
        return None if row is None else self.latest_order(row["order_id"])

    def counts(self) -> dict[str, int]:
        tables = (
            "paper_decisions",
            "paper_orders",
            "paper_order_events",
            "paper_fills",
            "paper_risk_decisions",
            "paper_position_snapshots",
            "paper_portfolio_snapshots",
        )
        return {
            table: int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }

    def decision_count(self, event_id: str) -> int:
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM paper_decisions WHERE event_id=?", (event_id,)
            ).fetchone()[0]
        )

    def filled_order_count(self, event_id: str) -> int:
        return int(
            self._connection.execute(
                "SELECT COUNT(DISTINCT order_id) FROM paper_fills WHERE event_id=?",
                (event_id,),
            ).fetchone()[0]
        )

    def has_hold(self, event_id: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM paper_decisions WHERE event_id=? AND decision='hold' LIMIT 1",
                (event_id,),
            ).fetchone()
            is not None
        )

    def pending_event_ids(self) -> tuple[str, ...]:
        rows = self._connection.execute(
            """
            SELECT DISTINCT current.event_id
            FROM paper_position_snapshots AS current
            WHERE current.status='pending_settlement'
              AND current.id=(
                SELECT MAX(candidate.id) FROM paper_position_snapshots AS candidate
                WHERE candidate.event_id=current.event_id
                  AND candidate.outcome=current.outcome
              )
            ORDER BY current.event_id
            """
        )
        return tuple(row["event_id"] for row in rows)

    def replay_orders(self) -> Iterator[PaperOrderRecord]:
        rows = self._connection.execute(
            "SELECT order_id FROM paper_orders ORDER BY submit_timestamp,id"
        )
        for row in rows:
            record = self.latest_order(row["order_id"])
            if record is None:
                raise PaperStorageError("paper order has no lifecycle event")
            yield record

    def replay_portfolios(self) -> Iterator[PaperPortfolioRecord]:
        for row in self._connection.execute(
            "SELECT * FROM paper_portfolio_snapshots ORDER BY snapshot_timestamp,id"
        ):
            yield PaperPortfolioRecord(
                row_id=row["id"],
                snapshot_timestamp=_parse_ts(row["snapshot_timestamp"]),
                account_id=row["account_id"],
                cash=_required_decimal(row["cash"]),
                realized_pnl=_required_decimal(row["realized_pnl"]),
                unrealized_pnl=_required_decimal(row["unrealized_pnl"]),
                fees_paid=_required_decimal(row["fees_paid"]),
                total_exposure=_required_decimal(row["total_exposure"]),
                daily_realized_pnl=_required_decimal(row["daily_realized_pnl"]),
                daily_pnl=_required_decimal(row["daily_pnl"]),
                consecutive_losses=int(row["consecutive_losses"]),
                fill_state_certain=bool(row["fill_state_certain"]),
            )

    def integrity_check(self) -> str:
        result = str(self._connection.execute("PRAGMA integrity_check").fetchone()[0])
        if result != "ok":
            return result
        violation = self._connection.execute("PRAGMA foreign_key_check").fetchone()
        if violation is not None:
            return "foreign_key_violation"
        return "ok"

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> PaperStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
