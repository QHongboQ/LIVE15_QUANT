from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from live15_quant.execution import ContractOutcome
from live15_quant.forward_shadow import (
    FORWARD_CANDIDATES,
    ForwardShadowError,
    ForwardShadowStore,
    _has_required_gap,
    _verify_v2,
)
from live15_quant.model_zoo import CertifiedDataset
from live15_quant.models import (
    Asset,
    ExecutabilityClassification,
    FreshnessState,
    MappingConfidence,
    OrderBookLevel,
    PredictionMarketQuote,
    SourceTimestampKind,
    Venue,
)
from live15_quant.paper import PaperPortfolio
from live15_quant.paper_execution import KalshiPaperExecutionProvider, PaperExecutionError
from live15_quant.paper_storage import PaperStore
from live15_quant.risk import HardRiskLimits, ImmutableHardRiskLayer


def _payload(
    *,
    decision_timestamp: str = "2026-08-23T00:00:00.000000+00:00",
    created_at: str = "2026-08-23T00:00:00.000000+00:00",
) -> dict[str, object]:
    return {
        "model_id": "logistic_l2_identity",
        "opportunity_id": f"ticker:{decision_timestamp}",
        "decision_timestamp": decision_timestamp,
        "bucket_seconds": 120,
        "asset": "BTC",
        "ticker": "KXBTC15M-example",
        "feature_hash": "f" * 64,
        "action": "hold",
        "data_status": "data_unavailable",
        "data_reason": "ws_snapshot_missing",
        "model_artifact_hash": "m" * 64,
        "dataset_id": "dataset",
        "created_at": created_at,
    }


def test_forward_ledger_start_and_idempotent_immutable_decisions(tmp_path) -> None:
    lineage = {
        "dataset_id": "dataset",
        "model_artifact_hash": "m" * 64,
        "model_zoo_v2": "zoo",
    }
    with ForwardShadowStore(tmp_path / "forward.sqlite3", lineage=lineage) as store:
        assert store.started_at.tzinfo is not None
        timestamp = (store.started_at + timedelta(seconds=1)).isoformat()
        assert store.append(_payload(decision_timestamp=timestamp))
        # A retry has a different observation timestamp but identical immutable intent.
        assert not store.append(
            _payload(decision_timestamp=timestamp, created_at="2026-08-23T00:00:01.000000+00:00")
        )
        conflicting = _payload(decision_timestamp=timestamp)
        conflicting["data_reason"] = "other"
        with pytest.raises(ForwardShadowError, match="idempotency"):
            store.append(conflicting)
        quarantine = _payload(decision_timestamp=timestamp)
        quarantine["opportunity_id"] = "quarantine-opportunity"
        quarantine["data_reason"] = "paper_decision_conflict"
        assert store.append(quarantine)
        changed_quarantine = dict(quarantine)
        changed_quarantine["feature_hash"] = "g" * 64
        assert not store.append(changed_quarantine)


def test_forward_ledger_rejects_pre_start_decision(tmp_path) -> None:
    with ForwardShadowStore(
        tmp_path / "forward.sqlite3",
        lineage={"dataset_id": "d", "model_artifact_hash": "h", "model_zoo_v2": "z"},
    ) as store:
        with pytest.raises(ForwardShadowError, match="predates"):
            store.append(_payload(decision_timestamp="2020-01-01T00:00:00+00:00"))


def test_forward_metrics_count_only_officially_settled_predictions(tmp_path) -> None:
    with ForwardShadowStore(
        tmp_path / "forward.sqlite3",
        lineage={"dataset_id": "d", "model_artifact_hash": "h", "model_zoo_v2": "z"},
    ) as store:
        timestamp = store.started_at.isoformat()
        payload = _payload(decision_timestamp=timestamp)
        payload.update(
            {
                "action": "buy_yes",
                "prediction": "0.70",
                "yes_edge": "0.10",
                "no_edge": "-0.11",
                "fees": "0.02",
                "paper_order_id": "order",
                "fill_state": "filled",
                "filled_quantity": "1",
            }
        )
        assert store.append(payload)
        assert store.append_settlement(
            model_id="logistic_l2_identity",
            opportunity_id=str(payload["opportunity_id"]),
            ticker="KXBTC15M-example",
            outcome_yes=True,
            settlement_timestamp=store.started_at,
            realized_pnl=Decimal("0.28"),
        )
        metric = store.metrics()["logistic_l2_identity"]
        assert metric["settled_trades"] == 1
        assert metric["brier"] == pytest.approx(0.09)
        assert metric["net_pnl"] == "0.28"
        assert metric["gross_pnl"] == "0.30"


def test_append_only_gap_projection_blocks_only_required_recent_intervals(tmp_path) -> None:
    import sqlite3

    connection = sqlite3.connect(tmp_path / "gaps.sqlite3")
    connection.execute(
        """CREATE TABLE data_gaps(
        source TEXT,asset TEXT,instrument TEXT,gap_start TEXT,gap_end TEXT,recovered INTEGER)"""
    )
    start = datetime(2026, 8, 23, tzinfo=UTC)
    connection.executemany(
        "INSERT INTO data_gaps VALUES (?,?,?,?,?,?)",
        (
            ("kalshi_ws", "BTC", "KXBTC15M", start.isoformat(), None, 0),
            (
                "kalshi_ws",
                "BTC",
                "KXBTC15M",
                start.isoformat(),
                (start + timedelta(seconds=30)).isoformat(),
                1,
            ),
            ("kalshi_rest", "BTC", "KXBTC15M", start.isoformat(), None, 0),
            ("pyth", "BTC", "Crypto.BTC/USD", start.isoformat(), None, 0),
        ),
    )
    assert _has_required_gap(connection, Asset.BTC, start + timedelta(minutes=2))
    assert not _has_required_gap(connection, Asset.BTC, start + timedelta(minutes=6))
    connection.close()


def _forward_quote(asset: Asset, ticker: str, timestamp: datetime) -> PredictionMarketQuote:
    return PredictionMarketQuote(
        asset=asset,
        robinhood_event_id=ticker,
        robinhood_contract_id=ticker,
        venue=Venue.KALSHI,
        venue_series="KXBTC15M",
        venue_ticker=ticker,
        mapping_confidence=MappingConfidence.VERIFIED,
        source_timestamp=timestamp,
        source_timestamp_kind=SourceTimestampKind.EXCHANGE_EVENT_TIME,
        received_timestamp=timestamp,
        yes_bid=Decimal("0.59"),
        yes_ask=Decimal("0.60"),
        no_bid=Decimal("0.40"),
        no_ask=Decimal("0.41"),
        last_trade=None,
        volume=None,
        yes_bid_depth=(OrderBookLevel(Decimal("0.59"), Decimal("10")),),
        no_bid_depth=(OrderBookLevel(Decimal("0.40"), Decimal("10")),),
        source="kalshi_ws",
        freshness=FreshnessState.FRESH,
        executability=ExecutabilityClassification.OFFICIAL_VENUE_ORDER_BOOK,
        evidence_urls=(),
    )


def _ready_snapshot(asset: Asset, decision: datetime) -> object:
    from live15_quant.forward_shadow import LiveFeatureSnapshot

    ticker = f"{asset.value}-ticker"
    return LiveFeatureSnapshot(
        asset,
        ticker,
        "event",
        decision - timedelta(minutes=10),
        decision + timedelta(minutes=5),
        decision,
        (),
        (),
        _forward_quote(asset, ticker, decision),
        "ready",
        None,
    )


def test_three_forward_candidates_use_isolated_paper_portfolios_and_no_pyramiding(tmp_path) -> None:
    from live15_quant.forward_shadow import ForwardShadowRuntime

    class Models:
        artifact_hash = "model-hash"

        @staticmethod
        def predict(_model_id: str, _row: object) -> Decimal:
            return Decimal("0.70")

    runtime = object.__new__(ForwardShadowRuntime)
    runtime.settings = SimpleNamespace(forward_shadow_order_quantity=Decimal("1"))
    runtime.models = Models()
    runtime.dataset = SimpleNamespace(dataset_id="dataset")
    stores: list[PaperStore] = []
    runtime.executions = {}
    try:
        for model_id, _threshold in FORWARD_CANDIDATES:
            store = PaperStore(
                tmp_path / f"{model_id}.sqlite3",
                account_id=model_id,
                starting_cash=Decimal("100"),
            )
            stores.append(store)
            runtime.executions[model_id] = KalshiPaperExecutionProvider(
                store=store,
                account_id=model_id,
                starting_cash=Decimal("100"),
                risk=ImmutableHardRiskLayer(
                    HardRiskLimits(Decimal("10"), Decimal("10"), Decimal("20"), Decimal("50"), 3)
                ),
            )
        first = _ready_snapshot(Asset.BTC, datetime(2026, 8, 23, tzinfo=UTC))
        payloads = [
            runtime._payload(model_id, threshold, first)
            for model_id, threshold in FORWARD_CANDIDATES
        ]
        assert [payload["action"] for payload in payloads] == ["buy_yes"] * 3
        assert len({payload["paper_order_id"] for payload in payloads}) == 3
        for model_id, _threshold in FORWARD_CANDIDATES:
            assert (
                runtime.executions[model_id].get_position(
                    "BTC-ticker", "BTC-ticker", ContractOutcome.YES
                )
                is not None
            )
        second = _ready_snapshot(Asset.BTC, datetime(2026, 8, 23, 0, 1, tzinfo=UTC))
        assert (
            runtime._payload(FORWARD_CANDIDATES[0][0], FORWARD_CANDIDATES[0][1], second)["action"]
            == "hold"
        )
        commodity = _ready_snapshot(Asset.GOLD, datetime(2026, 8, 23, tzinfo=UTC))
        assert (
            runtime._payload(FORWARD_CANDIDATES[0][0], FORWARD_CANDIDATES[0][1], commodity)[
                "action"
            ]
            == "hold"
        )
    finally:
        for store in stores:
            store.close()


def test_paper_intent_conflict_is_quarantined_after_restart(tmp_path) -> None:
    """A paper commit may precede its forward-ledger commit; retries must not re-order."""

    from live15_quant.forward_shadow import ForwardShadowRuntime

    class Store:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def append(self, payload: dict[str, object]) -> bool:
            self.payloads.append(payload)
            return True

    runtime = object.__new__(ForwardShadowRuntime)
    runtime.candidates = (
        SimpleNamespace(model_id="logistic_l2_identity", threshold=Decimal("0.10")),
    )
    runtime.models = SimpleNamespace(artifact_hash="m" * 64)
    runtime.dataset = SimpleNamespace(dataset_id="dataset")
    runtime.store = Store()
    runtime._payload = lambda *_args: (_ for _ in ()).throw(
        PaperExecutionError("decision ID conflicts with persisted immutable intent")
    )

    runtime._process(_ready_snapshot(Asset.BTC, datetime(2026, 8, 23, tzinfo=UTC)))
    assert runtime.store.payloads[0]["action"] == "hold"
    assert runtime.store.payloads[0]["data_status"] == "data_unavailable"
    assert runtime.store.payloads[0]["data_reason"] == "paper_decision_conflict"


def test_forward_ledger_rejects_storage_role_collision(tmp_path) -> None:
    path = tmp_path / "collision.sqlite3"
    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE recorder_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    connection.commit()
    connection.close()
    with pytest.raises(ForwardShadowError, match="cannot share"):
        ForwardShadowStore(
            path,
            lineage={"dataset_id": "d", "model_artifact_hash": "h", "model_zoo_v2": "z"},
        )


def test_forward_ledger_accepts_injected_future_candidate_id(tmp_path) -> None:
    """Accounting/ledger storage is not coupled to the current v2 candidate set."""

    with ForwardShadowStore(
        tmp_path / "v3.sqlite3",
        lineage={"dataset_id": "d", "model_artifact_hash": "h", "model_zoo_v2": "provider"},
        candidate_ids=("v3_candidate",),
    ) as store:
        timestamp = (store.started_at + timedelta(seconds=1)).isoformat()
        payload = _payload(decision_timestamp=timestamp)
        payload.update(
            {
                "model_id": "v3_candidate",
                "opportunity_id": f"v3:ticker:{timestamp}",
            }
        )
        assert store.append(payload)
        assert store.metrics()["v3_candidate"]["predictions"] == 0


def test_official_settlement_realizes_pending_position_without_underlying_proxy() -> None:
    portfolio = PaperPortfolio("paper", Decimal("100"))
    # The accounting primitive only consumes official Boolean outcome, never a price feed.
    from live15_quant.execution import ContractOutcome, ExecutionAction
    from live15_quant.fees import FeeComputation
    from live15_quant.paper import SimulatedFill

    fill = SimulatedFill(
        fill_id="f",
        order_id="o",
        asset=Asset.BTC,
        event_id="ticker",
        contract_id="ticker",
        fill_timestamp=datetime(2026, 8, 23, tzinfo=UTC),
        outcome=ContractOutcome.YES,
        action=ExecutionAction.BUY,
        quantity=Decimal("1"),
        price=Decimal("0.40"),
        fee=FeeComputation(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), "test"),
        spread=Decimal("0.01"),
        slippage=Decimal("0"),
    )
    portfolio.apply_fill(fill)
    portfolio.mark_pending_settlement("ticker")
    settled = portfolio.settle_event(
        "ticker", outcome_yes=True, settlement_timestamp=datetime(2026, 8, 23, 0, 1, tzinfo=UTC)
    )
    assert settled.realized_pnl == Decimal("0.60")
    assert portfolio.position("ticker", ContractOutcome.YES) is None


def test_v2_verification_rejects_any_final_test_consumption() -> None:
    dataset = CertifiedDataset(
        root=Path("."),
        manifest={"dataset_id": "dataset", "deterministic_build_hash": "hash"},
        feature_names=(),
        splits={"train": ()},
        oos_assets=frozenset(),
        train_only_assets=frozenset(),
    )
    manifest: dict[str, object] = {
        "format": "live15-model-zoo-v2-development-v1",
        "status": "FORWARD_CANDIDATE",
        "final_test": {
            "state": "REVEALED_FINAL",
            "v2_test_rows_consumed_for_development": True,
        },
        "dataset": {"dataset_id": "dataset", "deterministic_build_hash": "hash"},
        "forward_candidates": [f"{name}@{threshold}" for name, threshold in FORWARD_CANDIDATES],
    }
    with pytest.raises(ForwardShadowError, match="final-test"):
        _verify_v2(manifest, dataset)
