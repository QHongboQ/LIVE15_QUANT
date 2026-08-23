from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from live15_quant.demo_execution import (
    DemoExecutionCoordinator,
    DemoExecutionError,
    DemoExecutionResultCode,
    DemoExecutionStore,
    DemoIntent,
    DemoIntentPurpose,
    DemoLifecycleState,
    DemoPriceEVPolicy,
    DemoRiskContext,
    DemoRiskLimits,
    DemoRiskReason,
    DemoSizingMode,
    DemoSizingPolicy,
    DemoSynchronizedQuote,
    PreSubmitPriceEVGuard,
    SqliteKalshiWsQuoteSource,
    stable_client_order_id,
)
from live15_quant.providers.kalshi_demo_execution import (
    DemoAccountSnapshot,
    DemoBookSide,
    DemoExchangeStatus,
    DemoMarketTruth,
    DemoRemoteFill,
    DemoRemoteOrder,
    DemoRemoteOrderState,
    DemoRemotePosition,
    KalshiDemoAmbiguousWriteError,
    KalshiDemoWriteRejectedError,
)


def _intent(**changes: object) -> DemoIntent:
    values: dict[str, object] = {
        "model_id": "paper_logistic_l2",
        "model_artifact_hash": "a" * 64,
        "decision_id": "decision-1",
        "event_id": "event-1",
        "opportunity_id": "decision-60",
        "ticker": "KXBTC15M-TEST",
        "side": DemoBookSide.BID,
        "count": Decimal("1"),
        "price": Decimal("0.51"),
        "probability": Decimal("0.70"),
        "edge": Decimal("0.19"),
        "decision_timestamp": datetime(2026, 8, 23, tzinfo=UTC),
        "purpose": DemoIntentPurpose.EXECUTION_SMOKE,
    }
    values.update(changes)
    return DemoIntent(**values)  # type: ignore[arg-type]


def _remote(
    intent: DemoIntent,
    state: DemoRemoteOrderState = DemoRemoteOrderState.OPEN,
    *,
    price: Decimal | None = None,
):
    filled = Decimal("0.5") if state is DemoRemoteOrderState.PARTIALLY_FILLED else Decimal(0)
    if state is DemoRemoteOrderState.FILLED:
        filled = Decimal(1)
    return DemoRemoteOrder(
        "remote-1",
        stable_client_order_id(intent),
        intent.ticker,
        state,
        Decimal(1),
        filled,
        Decimal(1) - filled,
        intent.price if price is None else price,
        Decimal("0.01"),
        state.value,
    )


class FakeClient:
    def __init__(self) -> None:
        self.remote: DemoRemoteOrder | None = None
        self.create_response: DemoRemoteOrder | None = None
        self.create_calls = 0
        self.last_request = None
        self.cancel_calls = 0
        self.raise_submit = False
        self.submit_error: Exception | None = None
        self.raise_cancel = False
        self.remote_fills: tuple[DemoRemoteFill, ...] = ()
        self.remote_positions: tuple[DemoRemotePosition, ...] = ()
        self.exchange = DemoExchangeStatus(
            True, True, None, datetime(2026, 8, 23, 0, 0, 1, tzinfo=UTC)
        )
        self.market_truth = DemoMarketTruth(
            "KXBTC15M-TEST",
            "active",
            None,
            datetime(2026, 8, 23, 0, 15, tzinfo=UTC),
            datetime(2026, 8, 23, 0, 0, 1, tzinfo=UTC),
        )
        self.exchange_status_calls = 0
        self.market_calls = 0
        self.account = DemoAccountSnapshot(Decimal("100"), Decimal(0), 1_700_000_000)

    def exchange_status(self):
        self.exchange_status_calls += 1
        return self.exchange

    def market(self, ticker: str):
        self.market_calls += 1
        assert ticker == "KXBTC15M-TEST"
        return self.market_truth

    def latest_quote(self, ticker: str):
        assert ticker == "KXBTC15M-TEST"
        return DemoSynchronizedQuote(
            ticker=ticker,
            received_timestamp=datetime.now(UTC),
            synchronized=True,
            yes_bid=Decimal("0.50"),
            yes_ask=Decimal("0.51"),
            no_bid=Decimal("0.49"),
            no_ask=Decimal("0.50"),
        )

    def balance(self):
        return self.account

    def find_order_by_client_id(self, client_order_id: str):
        if self.remote is not None and self.remote.client_order_id == client_order_id:
            return self.remote
        return None

    def create_order(self, request):
        self.create_calls += 1
        self.last_request = request
        if self.submit_error is not None:
            raise self.submit_error
        if self.raise_submit:
            raise KalshiDemoAmbiguousWriteError("ambiguous")
        result = self.create_response or self.remote
        assert result is not None
        assert request.client_order_id == result.client_order_id
        return result

    def fills(self, *, order_id: str | None = None):
        if order_id is not None:
            assert order_id == "remote-1"
        return self.remote_fills

    def order(self, order_id: str):
        assert order_id == "remote-1"
        assert self.remote is not None
        return self.remote

    def cancel_order(self, order_id: str):
        self.cancel_calls += 1
        if self.raise_cancel:
            raise KalshiDemoAmbiguousWriteError("ambiguous")
        assert self.remote is not None
        self.remote = _remote(_intent(), DemoRemoteOrderState.CANCELED)
        return {"order_id": order_id}

    def positions(self):
        return self.remote_positions

    def orders(self):
        return () if self.remote is None else (self.remote,)


def _safe_context(**changes: object) -> DemoRiskContext:
    values: dict[str, object] = {
        "event_exposure": Decimal(0),
        "total_exposure": Decimal(0),
        "open_positions": 0,
        "daily_realized_pnl": Decimal(0),
        "reconciliation_certain": True,
        "kill_switch": False,
        "account_state_known": True,
        "positions_state_known": True,
        "open_orders_state_known": True,
        "daily_pnl_known": True,
    }
    values.update(changes)
    return DemoRiskContext(**values)  # type: ignore[arg-type]


class FixedQuoteSource:
    def __init__(self, quote: DemoSynchronizedQuote | None) -> None:
        self.quote = quote
        self.calls = 0

    def latest_quote(self, ticker: str):
        self.calls += 1
        if self.quote is not None:
            assert self.quote.ticker == ticker
        return self.quote


def _quote(*, ask: str = "0.51", age_seconds: int = 0, synchronized: bool = True):
    received = datetime(2026, 8, 23, 0, 0, 2, tzinfo=UTC)
    received -= timedelta(seconds=age_seconds)
    yes_ask = Decimal(ask)
    return DemoSynchronizedQuote(
        ticker="KXBTC15M-TEST",
        received_timestamp=received,
        synchronized=synchronized,
        yes_bid=yes_ask - Decimal("0.01"),
        yes_ask=yes_ask,
        no_bid=Decimal(1) - yes_ask,
        no_ask=Decimal(1) - yes_ask + Decimal("0.01"),
    )


def test_demo_writes_default_disabled_and_hard_risk_cannot_be_bypassed(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        coordinator = DemoExecutionCoordinator(client, store)
        decision = coordinator.submit(intent, _safe_context())
        assert decision.allowed is False  # type: ignore[union-attr]
        assert decision.reasons == (  # type: ignore[union-attr]
            DemoRiskReason.WRITES_DISABLED,
            DemoRiskReason.EXPLICIT_SMOKE_APPROVAL_REQUIRED,
        )
        assert client.create_calls == 0


def test_unknown_remote_risk_state_and_disabled_cancel_fail_closed(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        coordinator = DemoExecutionCoordinator(client, store)
        blocked = coordinator.submit(intent, _safe_context(account_state_known=False))
        assert DemoRiskReason.REMOTE_RISK_STATE_UNKNOWN in blocked.reasons  # type: ignore[union-attr]
        with pytest.raises(DemoExecutionError, match="writes are disabled"):
            coordinator.cancel(stable_client_order_id(intent), "remote-1")
        assert client.cancel_calls == 0
        unknown_pnl = coordinator.submit(intent, _safe_context(daily_pnl_known=False))
        assert DemoRiskReason.REMOTE_RISK_STATE_UNKNOWN in unknown_pnl.reasons  # type: ignore[union-attr]

        enabled = DemoExecutionCoordinator(
            client, store, writes_enabled=True, execution_smoke_approved=True
        )
        decision = enabled.submit(intent, _safe_context(kill_switch=True))
        assert DemoRiskReason.KILL_SWITCH in decision.reasons  # type: ignore[union-attr]
        assert client.create_calls == 0


@pytest.mark.parametrize(
    ("context", "limits", "reason"),
    [
        (
            _safe_context(open_positions=3),
            DemoRiskLimits(),
            DemoRiskReason.MAX_CONCURRENT_POSITIONS,
        ),
        (
            _safe_context(daily_realized_pnl=Decimal("-2")),
            DemoRiskLimits(),
            DemoRiskReason.MAX_DAILY_LOSS,
        ),
        (
            _safe_context(total_exposure=Decimal("4.8")),
            DemoRiskLimits(),
            DemoRiskReason.MAX_TOTAL_EXPOSURE,
        ),
        (
            _safe_context(event_exposure=Decimal("1.8")),
            DemoRiskLimits(),
            DemoRiskReason.MAX_EVENT_EXPOSURE,
        ),
    ],
)
def test_demo_hard_risk_caps(context, limits, reason, tmp_path: Path) -> None:
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        result = DemoExecutionCoordinator(
            FakeClient(), store, limits=limits, writes_enabled=True
        ).submit(_intent(), context)
    assert result.allowed is False  # type: ignore[union-attr]
    assert reason in result.reasons  # type: ignore[union-attr]


def test_demo_v1_requires_same_fixed_size_and_equity_sizing_stays_disabled(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="not enabled"):
        DemoSizingPolicy(
            mode=DemoSizingMode.BOUNDED_EQUITY_FUTURE,
            equity_sizing_enabled=True,
        )
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        result = DemoExecutionCoordinator(
            store=store, client=FakeClient(), writes_enabled=True
        ).submit(_intent(count=Decimal("0.5")), _safe_context())
        assert result.allowed is False  # type: ignore[union-attr]
        assert DemoRiskReason.FIXED_SIZING_POLICY in result.reasons  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="never expanded"):
        DemoRiskLimits(max_total_exposure=Decimal("6"))
    with pytest.raises(ValueError, match="may not exceed"):
        DemoSizingPolicy(fixed_order_count=Decimal("2"))


def test_only_explicitly_approved_execution_smoke_can_submit(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    client.remote = _remote(intent)
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        no_approval = DemoExecutionCoordinator(client, store, writes_enabled=True).submit(
            intent, _safe_context()
        )
        assert DemoRiskReason.EXPLICIT_SMOKE_APPROVAL_REQUIRED in no_approval.reasons  # type: ignore[union-attr]
        forward = DemoExecutionCoordinator(
            client, store, writes_enabled=True, execution_smoke_approved=True
        ).submit(
            _intent(
                opportunity_id="decision-120",
                decision_id="decision-2",
                purpose=DemoIntentPurpose.MODEL_FORWARD,
            ),
            _safe_context(),
        )
        assert DemoRiskReason.SMOKE_ONLY in forward.reasons  # type: ignore[union-attr]
        approved = DemoExecutionCoordinator(
            client, store, writes_enabled=True, execution_smoke_approved=True
        ).submit(intent, _safe_context())
        assert approved.state is DemoLifecycleState.OPEN  # type: ignore[union-attr]


def test_submit_is_idempotent_and_reconciles_existing_remote_order(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    client.remote = _remote(intent)
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        coordinator = DemoExecutionCoordinator(
            client, store, writes_enabled=True, execution_smoke_approved=True
        )
        first = coordinator.submit(intent, _safe_context())
        second = coordinator.submit(intent, _safe_context())

        assert first.state is DemoLifecycleState.OPEN  # type: ignore[union-attr]
        assert second == first
        assert client.create_calls == 0
        assert store.counts()["intents"] == 1
        assert client.exchange_status_calls == 0
        assert client.market_calls == 0


def test_official_exchange_and_market_truth_gate_new_submit(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    client.exchange = DemoExchangeStatus(
        True, False, "2026-08-23T01:00:00Z", datetime(2026, 8, 23, tzinfo=UTC)
    )
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        coordinator = DemoExecutionCoordinator(
            client, store, writes_enabled=True, execution_smoke_approved=True
        )
        result = coordinator.submit(intent, _safe_context())

    assert result.allowed is False  # type: ignore[union-attr]
    assert result.reasons == (DemoRiskReason.EXCHANGE_UNAVAILABLE,)  # type: ignore[union-attr]
    assert client.create_calls == 0


def test_official_truth_is_checked_immediately_before_new_submit(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    client.create_response = _remote(intent)
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        result = DemoExecutionCoordinator(
            client, store, writes_enabled=True, execution_smoke_approved=True
        ).submit(intent, _safe_context())

    assert result.state is DemoLifecycleState.OPEN  # type: ignore[union-attr]
    assert client.exchange_status_calls == 1
    assert client.market_calls == 1
    assert client.create_calls == 1


@pytest.mark.parametrize("ask", ("0.51", "0.52"))
def test_pre_submit_guard_allows_unchanged_or_small_move_with_positive_ev(
    ask: str, tmp_path: Path
) -> None:
    intent = _intent()
    client = FakeClient()
    client.create_response = _remote(intent, price=Decimal(ask))
    source = FixedQuoteSource(_quote(ask=ask))
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        result = DemoExecutionCoordinator(
            client,
            store,
            quote_source=source,
            utc_now=lambda: datetime(2026, 8, 23, 0, 0, 2, tzinfo=UTC),
            writes_enabled=True,
            execution_smoke_approved=True,
        ).submit(intent, _safe_context())
        diagnostic = store.latest_execution_diagnostic(stable_client_order_id(intent))

    assert result.state is DemoLifecycleState.OPEN  # type: ignore[union-attr]
    assert client.create_calls == 1
    assert client.last_request.price == Decimal(ask)
    assert source.calls == 1
    assert diagnostic is not None
    assert diagnostic[0] is DemoExecutionResultCode.HTTP_SUCCESS_NO_FILL


def test_buy_no_uses_no_ask_for_ev_and_complementary_yes_ask_limit(tmp_path: Path) -> None:
    intent = _intent(
        side=DemoBookSide.ASK,
        price=Decimal("0.50"),
        probability=Decimal("0.30"),
        edge=Decimal("0.20"),
    )
    client = FakeClient()
    # V2 asks are quoted on the YES book at 1 - executable NO ask. Remote
    # reconciliation exposes the acquired NO contract cost instead.
    client.create_response = _remote(intent, price=Decimal("0.50"))
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        result = DemoExecutionCoordinator(
            client,
            store,
            quote_source=FixedQuoteSource(_quote()),
            utc_now=lambda: datetime(2026, 8, 23, 0, 0, 2, tzinfo=UTC),
            writes_enabled=True,
            execution_smoke_approved=True,
        ).submit(intent, _safe_context())

    assert result.state is DemoLifecycleState.OPEN  # type: ignore[union-attr]
    assert client.create_calls == 1
    assert client.last_request.side is DemoBookSide.ASK
    assert client.last_request.price == Decimal("0.50")


def test_pre_submit_guard_blocks_price_above_maximum_without_submit(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    source = FixedQuoteSource(_quote(ask="0.55"))
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        result = DemoExecutionCoordinator(
            client,
            store,
            quote_source=source,
            utc_now=lambda: datetime(2026, 8, 23, 0, 0, 2, tzinfo=UTC),
            writes_enabled=True,
            execution_smoke_approved=True,
        ).submit(intent, _safe_context())
        diagnostic = store.latest_execution_diagnostic(stable_client_order_id(intent))

    assert result.code is DemoExecutionResultCode.PRICE_MOVED_TOO_FAR  # type: ignore[union-attr]
    assert result.allowed is False  # type: ignore[union-attr]
    assert client.create_calls == 0
    assert diagnostic is not None
    assert diagnostic[0] is DemoExecutionResultCode.PRICE_MOVED_TOO_FAR


def test_pre_submit_guard_blocks_decayed_edge_without_submit(tmp_path: Path) -> None:
    intent = _intent(probability=Decimal("0.58"), edge=Decimal("0.07"))
    client = FakeClient()
    policy = DemoPriceEVPolicy(
        max_adverse_price_move=Decimal("0.25"),
        safety_margin=Decimal(0),
        minimum_required_edge=Decimal("0.05"),
    )
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        result = DemoExecutionCoordinator(
            client,
            store,
            quote_source=FixedQuoteSource(_quote()),
            price_ev_guard=PreSubmitPriceEVGuard(policy),
            utc_now=lambda: datetime(2026, 8, 23, 0, 0, 2, tzinfo=UTC),
            writes_enabled=True,
            execution_smoke_approved=True,
        ).submit(intent, _safe_context())

    assert result.code is DemoExecutionResultCode.EDGE_DECAYED_BEFORE_SUBMIT  # type: ignore[union-attr]
    assert client.create_calls == 0


@pytest.mark.parametrize(
    "quote",
    (
        None,
        _quote(age_seconds=3),
        _quote(synchronized=False),
    ),
)
def test_pre_submit_guard_fails_closed_for_missing_stale_or_unsynchronized_book(
    quote: DemoSynchronizedQuote | None, tmp_path: Path
) -> None:
    intent = _intent()
    client = FakeClient()
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        result = DemoExecutionCoordinator(
            client,
            store,
            quote_source=FixedQuoteSource(quote),
            utc_now=lambda: datetime(2026, 8, 23, 0, 0, 2, tzinfo=UTC),
            writes_enabled=True,
            execution_smoke_approved=True,
        ).submit(intent, _safe_context())

    assert result.code is DemoExecutionResultCode.DATA_UNAVAILABLE  # type: ignore[union-attr]
    assert client.create_calls == 0


def test_sqlite_quote_source_requires_current_synchronized_ws_checkpoint(tmp_path: Path) -> None:
    raw = tmp_path / "raw.sqlite3"
    health = tmp_path / "health.json"
    with sqlite3.connect(raw) as connection:
        connection.executescript(
            """CREATE TABLE kalshi_ws_book_checkpoints(
                   id INTEGER PRIMARY KEY,
                   ticker TEXT,
                   received_timestamp TEXT NOT NULL,
                   yes_bids TEXT NOT NULL,
                   no_bids TEXT NOT NULL,
                   provenance TEXT NOT NULL
               );
               CREATE INDEX idx_ws_ticker
               ON kalshi_ws_book_checkpoints(ticker,received_timestamp,id);"""
        )
        connection.execute(
            "INSERT INTO kalshi_ws_book_checkpoints VALUES(1,?,?,?,?,?)",
            (
                "KXBTC15M-TEST",
                "2026-08-23T00:00:02+00:00",
                '[["0.50","2"]]',
                '[["0.49","3"]]',
                "kalshi_ws",
            ),
        )
    health.write_text(
        json.dumps(
            {
                "kalshi_ws_connection_state": "synchronized",
                "kalshi_ws_synchronized_markets": {"BTC": "KXBTC15M-TEST"},
            }
        ),
        encoding="utf-8",
    )
    source = SqliteKalshiWsQuoteSource(
        raw,
        health,
        utc_now=lambda: datetime(2026, 8, 23, 0, 0, 2, tzinfo=UTC),
    )
    quote = source.latest_quote("KXBTC15M-TEST")
    assert quote is not None
    assert quote.yes_bid == Decimal("0.50")
    assert quote.yes_ask == Decimal("0.51")
    assert quote.source == "KALSHI_WS_SYNCHRONIZED"

    with sqlite3.connect(raw) as connection:
        connection.execute(
            "UPDATE kalshi_ws_book_checkpoints SET provenance='kalshi_rest' WHERE id=1"
        )
    assert source.latest_quote("KXBTC15M-TEST") is None


def test_synchronized_quote_rejects_crossed_or_non_complementary_book() -> None:
    with pytest.raises(ValueError, match="crossed"):
        replace(_quote(), yes_bid=Decimal("0.52"), yes_ask=Decimal("0.51"))
    with pytest.raises(ValueError, match="complementary"):
        replace(_quote(), yes_bid=Decimal("0.49"), no_ask=Decimal("0.52"))


def test_remote_account_truth_overrides_stale_local_exposure(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    client.create_response = _remote(intent)
    stale_local = _safe_context(
        event_exposure=Decimal("2"),
        total_exposure=Decimal("5"),
        open_positions=3,
        account_state_known=False,
        positions_state_known=False,
        open_orders_state_known=False,
    )
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        result = DemoExecutionCoordinator(
            client, store, writes_enabled=True, execution_smoke_approved=True
        ).submit(intent, stale_local)

    assert result.state is DemoLifecycleState.OPEN  # type: ignore[union-attr]
    assert client.create_calls == 1


def test_remote_exposure_and_buying_power_fail_closed(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    client.remote_positions = (
        DemoRemotePosition(
            intent.ticker,
            Decimal("1"),
            Decimal("1.8"),
            Decimal(0),
            Decimal(0),
            0,
            "2026-08-23T00:00:00Z",
        ),
    )
    client.account = DemoAccountSnapshot(Decimal("0.25"), Decimal("1.8"), 1_700_000_000)
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        result = DemoExecutionCoordinator(
            client, store, writes_enabled=True, execution_smoke_approved=True
        ).submit(intent, _safe_context())

    assert result.allowed is False  # type: ignore[union-attr]
    assert DemoRiskReason.MAX_EVENT_EXPOSURE in result.reasons  # type: ignore[union-attr]
    assert DemoRiskReason.INSUFFICIENT_BUYING_POWER in result.reasons  # type: ignore[union-attr]
    assert client.create_calls == 0


def test_future_official_response_cannot_execute_a_past_decision(tmp_path: Path) -> None:
    intent = _intent(decision_timestamp=datetime(2026, 8, 22, 23, 58, tzinfo=UTC))
    client = FakeClient()
    client.create_response = _remote(intent)
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        result = DemoExecutionCoordinator(
            client, store, writes_enabled=True, execution_smoke_approved=True
        ).submit(intent, _safe_context())

    assert result.allowed is False  # type: ignore[union-attr]
    assert DemoRiskReason.DECISION_STALE in result.reasons  # type: ignore[union-attr]
    assert client.create_calls == 0


def test_official_market_truth_overrides_stale_local_assumption(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    client.market_truth = DemoMarketTruth(
        intent.ticker,
        "closed",
        "yes",
        datetime(2026, 8, 22, 23, 59, tzinfo=UTC),
        datetime(2026, 8, 23, tzinfo=UTC),
    )
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        result = DemoExecutionCoordinator(
            client, store, writes_enabled=True, execution_smoke_approved=True
        ).submit(intent, _safe_context())

    assert result.allowed is False  # type: ignore[union-attr]
    assert result.reasons == (DemoRiskReason.MARKET_NOT_TRADEABLE,)  # type: ignore[union-attr]
    assert client.create_calls == 0


def test_official_market_identity_conflict_fails_loud(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    client.market_truth = DemoMarketTruth(
        "KXETH15M-OTHER",
        "active",
        None,
        datetime(2026, 8, 23, 0, 15, tzinfo=UTC),
        datetime(2026, 8, 23, tzinfo=UTC),
    )
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        with pytest.raises(DemoExecutionError, match="market identity"):
            DemoExecutionCoordinator(
                client, store, writes_enabled=True, execution_smoke_approved=True
            ).submit(intent, _safe_context())
    assert client.create_calls == 0


def test_response_lost_after_submit_never_blindly_retries(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    client.raise_submit = True
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        coordinator = DemoExecutionCoordinator(
            client, store, writes_enabled=True, execution_smoke_approved=True
        )
        first = coordinator.submit(intent, _safe_context())
        second = coordinator.submit(intent, _safe_context())

        assert first.state is DemoLifecycleState.RECONCILIATION_REQUIRED  # type: ignore[union-attr]
        assert second.state is DemoLifecycleState.RECONCILIATION_REQUIRED  # type: ignore[union-attr]
        assert client.create_calls == 1


def test_ambiguous_submit_persists_safe_typed_reason(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    client.raise_submit = True
    path = tmp_path / "demo.sqlite3"
    with DemoExecutionStore(path) as store:
        DemoExecutionCoordinator(
            client, store, writes_enabled=True, execution_smoke_approved=True
        ).submit(intent, _safe_context())
    connection = sqlite3.connect(path)
    try:
        detail = connection.execute(
            "SELECT detail_json FROM demo_order_facts ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        connection.close()
    assert json.loads(detail) == {
        "reason": "submit_outcome_ambiguous",
        "reason_code": "write_outcome_ambiguous",
    }


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            KalshiDemoAmbiguousWriteError("safe", reason_code="transport_failure"),
            DemoExecutionResultCode.TRANSPORT_ERROR,
        ),
        (
            KalshiDemoAmbiguousWriteError("safe", reason_code="http_409"),
            DemoExecutionResultCode.HTTP_409,
        ),
        (
            KalshiDemoAmbiguousWriteError("safe", reason_code="http_429"),
            DemoExecutionResultCode.HTTP_429,
        ),
        (
            KalshiDemoAmbiguousWriteError("safe", reason_code="http_503"),
            DemoExecutionResultCode.HTTP_5XX,
        ),
        (
            KalshiDemoAmbiguousWriteError("safe", reason_code="compact_ack_invalid"),
            DemoExecutionResultCode.MALFORMED_ACK,
        ),
    ],
)
def test_submit_ambiguity_has_typed_non_sensitive_diagnostic(
    error: Exception, expected: DemoExecutionResultCode, tmp_path: Path
) -> None:
    intent = _intent()
    client = FakeClient()
    client.submit_error = error
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        result = DemoExecutionCoordinator(
            client,
            store,
            writes_enabled=True,
            execution_smoke_approved=True,
        ).submit(intent, _safe_context())
        diagnostic = store.latest_execution_diagnostic(stable_client_order_id(intent))
    assert result.state is DemoLifecycleState.RECONCILIATION_REQUIRED  # type: ignore[union-attr]
    assert diagnostic is not None
    assert diagnostic[0] is expected


def test_conclusive_submit_rejection_is_typed_and_not_retried(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    client.submit_error = KalshiDemoWriteRejectedError(
        "safe",
        reason_code="http_404",
        diagnostic={
            "http_status": 404,
            "provider_error_code": "market_not_found",
            "sanitized_provider_message": "safe diagnostic",
            "request_method": "POST",
            "request_path": "/trade-api/v2/portfolio/events/orders",
            "environment": "DEMO",
        },
    )
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        coordinator = DemoExecutionCoordinator(
            client,
            store,
            writes_enabled=True,
            execution_smoke_approved=True,
        )
        first = coordinator.submit(intent, _safe_context())
        second = coordinator.submit(intent, _safe_context())
        diagnostic = store.latest_execution_diagnostic(stable_client_order_id(intent))
    assert first.state is DemoLifecycleState.REJECTED  # type: ignore[union-attr]
    assert second.state is DemoLifecycleState.REJECTED  # type: ignore[union-attr]
    assert client.create_calls == 1
    assert diagnostic is not None
    assert diagnostic[0] is DemoExecutionResultCode.HTTP_404
    assert diagnostic[1] == {
        "environment": "DEMO",
        "http_status": 404,
        "provider_error_code": "market_not_found",
        "reason": "submit_rejected",
        "reason_code": "http_404",
        "request_method": "POST",
        "request_path": "/trade-api/v2/portfolio/events/orders",
        "sanitized_provider_message": "safe diagnostic",
    }


def test_partial_fill_and_duplicate_fill_poll_are_restart_safe(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    client.remote = _remote(intent, DemoRemoteOrderState.PARTIALLY_FILLED)
    client.remote_fills = (
        DemoRemoteFill(
            "fill-1",
            "remote-1",
            intent.ticker,
            Decimal("0.5"),
            Decimal("0.51"),
            Decimal("0.01"),
            "2026-08-23T00:00:00Z",
        ),
    )
    path = tmp_path / "demo.sqlite3"
    with DemoExecutionStore(path) as store:
        store.append_intent(intent)
        first = DemoExecutionCoordinator(
            client, store, writes_enabled=True, execution_smoke_approved=True
        ).reconcile(stable_client_order_id(intent))
        assert first.inserted_fills == 1
    with DemoExecutionStore(path) as store:
        second = DemoExecutionCoordinator(
            client, store, writes_enabled=True, execution_smoke_approved=True
        ).reconcile(stable_client_order_id(intent))
        assert second.state is DemoLifecycleState.PARTIALLY_FILLED
        assert second.inserted_fills == 0
        assert store.counts()["fills"] == 1


def test_restart_reconciles_pending_remote_truth_and_position_once(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    client.remote = _remote(intent, DemoRemoteOrderState.PARTIALLY_FILLED)
    client.remote_positions = (
        DemoRemotePosition(
            intent.ticker,
            Decimal("0.5"),
            Decimal("0.255"),
            Decimal(0),
            Decimal("0.01"),
            1,
            "2026-08-23T00:00:01Z",
        ),
    )
    path = tmp_path / "demo.sqlite3"
    with DemoExecutionStore(path) as store:
        store.append_intent(intent)
    with DemoExecutionStore(path) as store:
        results = DemoExecutionCoordinator(client, store).reconcile_pending()
        assert results[0].state is DemoLifecycleState.PARTIALLY_FILLED
        assert store.counts()["positions"] == 1
        assert DemoExecutionCoordinator(client, store).reconcile_positions() == 0


def test_conflicting_immutable_intent_and_fill_fail_loud(tmp_path: Path) -> None:
    intent = _intent()
    fill = DemoRemoteFill(
        "fill-1",
        "remote-1",
        intent.ticker,
        Decimal("1"),
        Decimal("0.51"),
        Decimal("0.01"),
        "2026-08-23T00:00:00Z",
    )
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        store.append_intent(intent)
        with pytest.raises(DemoExecutionError, match="immutable intent"):
            store.append_intent(_intent(price=Decimal("0.52")))
        assert store.append_fill(fill) is True
        with pytest.raises(DemoExecutionError, match="fill ID"):
            store.append_fill(
                DemoRemoteFill(
                    "fill-1",
                    "remote-1",
                    intent.ticker,
                    Decimal("1"),
                    Decimal("0.52"),
                    Decimal("0.01"),
                    "2026-08-23T00:00:00Z",
                )
            )


def test_cancel_response_lost_requires_reconciliation(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    client.remote = _remote(intent)
    client.raise_cancel = True
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        store.append_intent(intent)
        store.append_state(
            stable_client_order_id(intent),
            DemoLifecycleState.OPEN,
            provider_order_id="remote-1",
            detail={"filled_count": "0"},
        )
        result = DemoExecutionCoordinator(
            client, store, writes_enabled=True, execution_smoke_approved=True
        ).cancel(stable_client_order_id(intent), "remote-1")
        assert result.state is DemoLifecycleState.RECONCILIATION_REQUIRED
        assert client.cancel_calls == 1


def test_demo_ledger_rejects_wrong_environment(tmp_path: Path) -> None:
    path = tmp_path / "demo.sqlite3"
    with DemoExecutionStore(path):
        pass
    import sqlite3

    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE demo_metadata SET value='PRODUCTION' WHERE key='environment'")
    with pytest.raises(DemoExecutionError, match="KALSHI_DEMO"):
        DemoExecutionStore(path)


def test_demo_store_will_not_open_a_paper_or_raw_role_database(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "paper.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE paper_metadata(key TEXT PRIMARY KEY,value TEXT)")
        connection.execute("PRAGMA user_version=1")
    with pytest.raises(DemoExecutionError, match="KALSHI_DEMO"):
        DemoExecutionStore(path)


def test_demo_risk_rejects_unknown_numeric_state() -> None:
    with pytest.raises(ValueError, match="finite"):
        _safe_context(total_exposure=Decimal("NaN"))
    with pytest.raises(ValueError, match="non-negative"):
        _safe_context(open_positions=-1)


def test_remote_order_identity_conflict_fails_loud(tmp_path: Path) -> None:
    intent = _intent()
    client = FakeClient()
    client.remote = DemoRemoteOrder(
        "remote-1",
        stable_client_order_id(intent),
        "WRONG-TICKER",
        DemoRemoteOrderState.OPEN,
        Decimal(1),
        Decimal(0),
        Decimal(1),
        Decimal("0.51"),
        Decimal(0),
        "resting",
    )
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        with pytest.raises(DemoExecutionError, match="ticker"):
            DemoExecutionCoordinator(
                client, store, writes_enabled=True, execution_smoke_approved=True
            ).submit(intent, _safe_context())


def test_illegal_lifecycle_regression_fails_loud(tmp_path: Path) -> None:
    intent = _intent()
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        client_id = store.append_intent(intent)
        store.append_state(client_id, DemoLifecycleState.INTENT)
        store.append_state(client_id, DemoLifecycleState.SUBMITTING)
        store.append_state(client_id, DemoLifecycleState.FILLED)
        with pytest.raises(DemoExecutionError, match="illegal Demo lifecycle transition"):
            store.append_state(client_id, DemoLifecycleState.OPEN)


def test_nonempty_zero_version_database_is_not_initialized_as_demo(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "raw.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE raw_truth(id INTEGER PRIMARY KEY)")
    with pytest.raises(DemoExecutionError, match="not an empty"):
        DemoExecutionStore(path)


def test_official_settlement_is_idempotent_and_conflicts_fail_loud(tmp_path: Path) -> None:
    timestamp = datetime(2026, 8, 23, 0, 15, tzinfo=UTC)
    with DemoExecutionStore(tmp_path / "demo.sqlite3") as store:
        client_id = store.append_intent(_intent())
        store.append_state(
            client_id,
            DemoLifecycleState.FILLED,
            provider_order_id="remote-1",
            detail={"filled_count": "1"},
        )
        assert store.append_settlement(
            event_id="event-1",
            outcome_yes=True,
            settlement_timestamp=timestamp,
            realized_pnl=Decimal("0.48"),
            fees=Decimal("0.02"),
        )
        assert store.latest_state(client_id) is DemoLifecycleState.SETTLED
        assert not store.append_settlement(
            event_id="event-1",
            outcome_yes=True,
            settlement_timestamp=timestamp,
            realized_pnl=Decimal("0.48"),
            fees=Decimal("0.02"),
        )
        with pytest.raises(DemoExecutionError, match="official truth"):
            store.append_settlement(
                event_id="event-1",
                outcome_yes=False,
                settlement_timestamp=timestamp,
                realized_pnl=Decimal("-0.52"),
                fees=Decimal("0.02"),
            )
