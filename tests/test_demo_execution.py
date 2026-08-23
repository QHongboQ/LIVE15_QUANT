from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from live15_quant.demo_execution import (
    DemoExecutionCoordinator,
    DemoExecutionError,
    DemoExecutionStore,
    DemoIntent,
    DemoIntentPurpose,
    DemoLifecycleState,
    DemoRiskContext,
    DemoRiskLimits,
    DemoRiskReason,
    DemoSizingMode,
    DemoSizingPolicy,
    stable_client_order_id,
)
from live15_quant.providers.kalshi_demo_execution import (
    DemoBookSide,
    DemoRemoteFill,
    DemoRemoteOrder,
    DemoRemoteOrderState,
    DemoRemotePosition,
    KalshiDemoAmbiguousWriteError,
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


def _remote(intent: DemoIntent, state: DemoRemoteOrderState = DemoRemoteOrderState.OPEN):
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
        intent.price,
        Decimal("0.01"),
        state.value,
    )


class FakeClient:
    def __init__(self) -> None:
        self.remote: DemoRemoteOrder | None = None
        self.create_calls = 0
        self.cancel_calls = 0
        self.raise_submit = False
        self.raise_cancel = False
        self.remote_fills: tuple[DemoRemoteFill, ...] = ()
        self.remote_positions: tuple[DemoRemotePosition, ...] = ()

    def find_order_by_client_id(self, client_order_id: str):
        if self.remote is not None and self.remote.client_order_id == client_order_id:
            return self.remote
        return None

    def create_order(self, request):
        self.create_calls += 1
        if self.raise_submit:
            raise KalshiDemoAmbiguousWriteError("ambiguous")
        assert self.remote is not None
        assert request.client_order_id == self.remote.client_order_id
        return self.remote

    def fills(self, *, order_id: str | None = None):
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
    client.remote = _remote(intent)
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
