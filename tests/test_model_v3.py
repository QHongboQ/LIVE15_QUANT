from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from live15_quant.execution import ContractOutcome
from live15_quant.model_v3 import (
    DEFAULT_MICROSTRUCTURE_HORIZONS,
    DEFAULT_PATH_HORIZONS,
    DynamicDecisionAction,
    DynamicDecisionContext,
    DynamicDecisionEngine,
    DynamicPosition,
    HorizonPrediction,
    MicrostructureExpertPrediction,
    ModelV3Error,
    PathExpertPrediction,
    RegimeInputs,
    RegimeLabel,
    RegimePrediction,
    RuleAssistedRegimeExpert,
    TerminalProbabilityPrediction,
    V3ForwardPredictionAdapter,
    architecture_manifest,
)
from live15_quant.model_zoo import DatasetExample
from live15_quant.models import Asset

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


def _horizon(*, up: str = "0.7", down: str = "0.2", reversal: str = "0.1") -> HorizonPrediction:
    return HorizonPrediction(
        timedelta(seconds=30),
        Decimal("0.01"),
        Decimal(up),
        Decimal(down),
        Decimal("0.8"),
        Decimal(reversal),
    )


def _context(
    *, position: DynamicPosition | None = None, reversal: str = "0.1"
) -> DynamicDecisionContext:
    path = PathExpertPrediction(Asset.BTC, NOW, (_horizon(reversal=reversal),), "path", "path-hash")
    book = MicrostructureExpertPrediction(
        Asset.BTC, "ticker", NOW, (_horizon(reversal=reversal),), "book", "book-hash"
    )
    regime = RegimePrediction(
        Asset.BTC,
        NOW,
        frozenset({RegimeLabel.TRENDING_UP}),
        Decimal("0.8"),
        "regime",
        "regime-hash",
    )
    terminal = TerminalProbabilityPrediction(
        Asset.BTC, "ticker", NOW, Decimal("0.75"), "terminal", "terminal-hash"
    )
    return DynamicDecisionContext(
        Asset.BTC,
        "ticker",
        NOW,
        NOW + timedelta(minutes=5),
        Decimal("0.60"),
        Decimal("0.61"),
        Decimal("0.39"),
        Decimal("0.40"),
        terminal,
        path,
        book,
        regime,
        position,
        Decimal("0.01"),
        Decimal("0.01"),
    )


def test_dynamic_engine_buys_only_when_after_cost_and_short_horizon_confirm() -> None:
    result = DynamicDecisionEngine().evaluate(_context())
    assert result.action is DynamicDecisionAction.BUY_YES
    assert result.outcome is ContractOutcome.YES
    assert result.expected_value_add == Decimal("0.13")


def test_dynamic_engine_compares_close_with_hold_to_settlement() -> None:
    position = DynamicPosition(ContractOutcome.YES, Decimal("1"), Decimal("0.50"), Decimal("0.10"))
    context = _context(position=position)
    # A current executable bid of 0.60 is worse than terminal EV 0.75, so do not
    # mechanically take profit merely because the position is green.
    result = DynamicDecisionEngine().evaluate(context)
    assert result.action is DynamicDecisionAction.ADD
    near_expiry = DynamicDecisionContext(
        **{
            name: getattr(context, name)
            for name in (
                "asset",
                "ticker",
                "as_of",
                "yes_bid",
                "yes_ask",
                "no_bid",
                "no_ask",
                "terminal",
                "path",
                "microstructure",
                "regime",
                "position",
                "estimated_entry_fee",
                "estimated_exit_fee",
            )
        },
        window_end=NOW + timedelta(seconds=30),
    )
    assert (
        DynamicDecisionEngine().evaluate(near_expiry).action
        is DynamicDecisionAction.HOLD_TO_SETTLEMENT
    )


def test_dynamic_engine_take_profit_requires_close_ev_or_reversal_risk() -> None:
    position = DynamicPosition(ContractOutcome.YES, Decimal("1"), Decimal("0.50"), Decimal("0.10"))
    context = _context(position=position, reversal="0.8")
    assert DynamicDecisionEngine().evaluate(context).action is DynamicDecisionAction.TAKE_PROFIT


def test_dynamic_engine_labels_ev_dominated_losing_exit_as_cut_loss() -> None:
    position = DynamicPosition(ContractOutcome.YES, Decimal("1"), Decimal("0.70"), Decimal("-0.10"))
    context = _context(position=position)
    weaker_terminal = TerminalProbabilityPrediction(
        Asset.BTC, "ticker", NOW, Decimal("0.40"), "terminal", "terminal-hash"
    )
    result = DynamicDecisionEngine().evaluate(
        DynamicDecisionContext(
            context.asset,
            context.ticker,
            context.as_of,
            context.window_end,
            context.yes_bid,
            context.yes_ask,
            context.no_bid,
            context.no_ask,
            weaker_terminal,
            context.path,
            context.microstructure,
            context.regime,
            position,
            context.estimated_entry_fee,
            context.estimated_exit_fee,
        )
    )
    assert result.action is DynamicDecisionAction.CUT_LOSS


def test_dynamic_engine_can_emit_neutral_close_when_ev_requires_exit() -> None:
    position = DynamicPosition(ContractOutcome.YES, Decimal("1"), Decimal("0.59"), Decimal("0"))
    context = _context(position=position)
    weaker_terminal = TerminalProbabilityPrediction(
        Asset.BTC, "ticker", NOW, Decimal("0.40"), "terminal", "terminal-hash"
    )
    result = DynamicDecisionEngine().evaluate(
        DynamicDecisionContext(
            context.asset,
            context.ticker,
            context.as_of,
            context.window_end,
            context.yes_bid,
            context.yes_ask,
            context.no_bid,
            context.no_ask,
            weaker_terminal,
            context.path,
            context.microstructure,
            context.regime,
            position,
            context.estimated_entry_fee,
            context.estimated_exit_fee,
        )
    )
    assert result.action is DynamicDecisionAction.CLOSE


def test_dynamic_engine_fails_closed_when_an_expert_is_unavailable() -> None:
    context = _context()
    unavailable = RegimePrediction(
        Asset.BTC,
        NOW,
        frozenset(),
        None,
        "regime",
        "hash",
        "data_unavailable",
        "as_of_input_missing",
    )
    blocked = DynamicDecisionContext(
        context.asset,
        context.ticker,
        context.as_of,
        context.window_end,
        context.yes_bid,
        context.yes_ask,
        context.no_bid,
        context.no_ask,
        context.terminal,
        context.path,
        context.microstructure,
        unavailable,
    )
    assert (
        DynamicDecisionEngine().evaluate(blocked).action is DynamicDecisionAction.DATA_UNAVAILABLE
    )


def test_dynamic_context_rejects_future_or_cross_ticker_expert_predictions() -> None:
    context = _context()
    future_terminal = TerminalProbabilityPrediction(
        Asset.BTC,
        "ticker",
        NOW + timedelta(microseconds=1),
        Decimal("0.75"),
        "terminal",
        "terminal-hash",
    )
    with pytest.raises(ModelV3Error, match="after decision time"):
        DynamicDecisionContext(
            context.asset,
            context.ticker,
            context.as_of,
            context.window_end,
            context.yes_bid,
            context.yes_ask,
            context.no_bid,
            context.no_ask,
            future_terminal,
            context.path,
            context.microstructure,
            context.regime,
        )
    wrong_ticker = MicrostructureExpertPrediction(
        Asset.BTC,
        "different-ticker",
        NOW,
        context.microstructure.horizons if context.microstructure else (),
        "book",
        "book-hash",
    )
    with pytest.raises(ModelV3Error, match="ticker must match"):
        DynamicDecisionContext(
            context.asset,
            context.ticker,
            context.as_of,
            context.window_end,
            context.yes_bid,
            context.yes_ask,
            context.no_bid,
            context.no_ask,
            context.terminal,
            context.path,
            wrong_ticker,
            context.regime,
        )


def test_dynamic_engine_fails_closed_for_unknown_regime() -> None:
    context = _context()
    unknown_regime = RegimePrediction(
        Asset.BTC,
        NOW,
        frozenset({RegimeLabel.UNKNOWN}),
        Decimal("0.5"),
        "regime",
        "regime-hash",
    )
    decision = DynamicDecisionEngine().evaluate(
        DynamicDecisionContext(
            context.asset,
            context.ticker,
            context.as_of,
            context.window_end,
            context.yes_bid,
            context.yes_ask,
            context.no_bid,
            context.no_ask,
            context.terminal,
            context.path,
            context.microstructure,
            unknown_regime,
        )
    )
    assert decision.action is DynamicDecisionAction.DATA_UNAVAILABLE


def test_dynamic_engine_fails_closed_when_required_short_horizon_field_is_unknown() -> None:
    context = _context()
    incomplete_path = PathExpertPrediction(
        Asset.BTC,
        NOW,
        (
            HorizonPrediction(
                timedelta(seconds=30),
                Decimal("0.01"),
                Decimal("0.7"),
                Decimal("0.2"),
                Decimal("0.8"),
                None,
            ),
        ),
        "path",
        "path-hash",
    )
    blocked = DynamicDecisionContext(
        context.asset,
        context.ticker,
        context.as_of,
        context.window_end,
        context.yes_bid,
        context.yes_ask,
        context.no_bid,
        context.no_ask,
        context.terminal,
        incomplete_path,
        context.microstructure,
        context.regime,
    )
    assert (
        DynamicDecisionEngine().evaluate(blocked).action is DynamicDecisionAction.DATA_UNAVAILABLE
    )


def test_regime_is_decision_time_only_and_typed() -> None:
    prediction = RuleAssistedRegimeExpert().predict(
        Asset.BTC,
        RegimeInputs(NOW, Decimal("0.01"), Decimal("0.02"), Decimal("0.003"), Decimal("0.7")),
    )
    assert RegimeLabel.TRENDING_UP in prediction.labels
    assert RegimeLabel.REVERSAL_RISK_ELEVATED in prediction.labels


def test_v3_adapter_is_forward_provider_compatible() -> None:
    class Provider:
        artifact_hash = "v3-artifact"

        def terminal(self, _row: DatasetExample) -> TerminalProbabilityPrediction:
            return TerminalProbabilityPrediction(
                Asset.BTC, "ticker", NOW, Decimal("0.63"), "v3", self.artifact_hash
            )

    row = DatasetExample("BTC", "ticker", NOW, NOW, NOW + timedelta(minutes=1), 30, 0, (), ())
    adapter = V3ForwardPredictionAdapter("v3_terminal", Provider())
    assert adapter.predict("v3_terminal", row) == Decimal("0.63")
    with pytest.raises(ModelV3Error, match="unowned"):
        adapter.predict("other", row)


def test_v3_adapter_rejects_future_or_wrong_identity_terminal_prediction() -> None:
    class FutureProvider:
        artifact_hash = "v3-artifact"

        def terminal(self, _row: DatasetExample) -> TerminalProbabilityPrediction:
            return TerminalProbabilityPrediction(
                Asset.BTC,
                "ticker",
                NOW + timedelta(microseconds=1),
                Decimal("0.63"),
                "v3",
                self.artifact_hash,
            )

    row = DatasetExample("BTC", "ticker", NOW, NOW, NOW + timedelta(minutes=1), 30, 0, (), ())
    with pytest.raises(ModelV3Error, match="after forward decision time"):
        V3ForwardPredictionAdapter("v3_terminal", FutureProvider()).predict("v3_terminal", row)

    class WrongIdentityProvider:
        artifact_hash = "v3-artifact"

        def terminal(self, _row: DatasetExample) -> TerminalProbabilityPrediction:
            return TerminalProbabilityPrediction(
                Asset.ETH, "other-ticker", NOW, Decimal("0.63"), "v3", self.artifact_hash
            )

    with pytest.raises(ModelV3Error, match="identity does not match"):
        V3ForwardPredictionAdapter("v3_terminal", WrongIdentityProvider()).predict(
            "v3_terminal", row
        )


def test_microstructure_horizons_and_decision_prices_are_validated() -> None:
    with pytest.raises(ModelV3Error, match="microstructure horizons"):
        MicrostructureExpertPrediction(
            Asset.BTC,
            "ticker",
            NOW,
            (_horizon(), _horizon()),
            "book",
            "book-hash",
        )
    context = _context()
    with pytest.raises(ModelV3Error, match="bid cannot exceed ask"):
        DynamicDecisionContext(
            context.asset,
            context.ticker,
            context.as_of,
            context.window_end,
            Decimal("0.62"),
            Decimal("0.61"),
            context.no_bid,
            context.no_ask,
            context.terminal,
            context.path,
            context.microstructure,
            context.regime,
        )


def test_architecture_manifest_freezes_source_license_and_commit_metadata() -> None:
    manifest = architecture_manifest()
    assert len(manifest["upstreams"]) == 4
    assert manifest["deterministic_hash"] == architecture_manifest()["deterministic_hash"]
    assert any(
        item["decision"] == "RESEARCH_ONLY_NO_CODE_VENDORING" for item in manifest["upstreams"]
    )
    assert all(item["training_data_requirement"] for item in manifest["upstreams"])
    assert all(item["expected_inference_latency"] for item in manifest["upstreams"])
    assert tuple(manifest["path_horizons_seconds"]) == tuple(
        int(item.total_seconds()) for item in DEFAULT_PATH_HORIZONS
    )
    assert tuple(manifest["microstructure_horizons_seconds"]) == tuple(
        int(item.total_seconds()) for item in DEFAULT_MICROSTRUCTURE_HORIZONS
    )
