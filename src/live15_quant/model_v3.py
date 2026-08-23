"""Typed, leakage-safe Model Architecture v3 contracts.

Model Zoo v3 is deliberately an expert system boundary rather than a replacement
for the certified terminal-probability models.  It does not train, connect to a
venue, write a paper order, or read a recorder database.  Training and runtime
adapters must supply only decision-time inputs to the typed contracts here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from live15_quant.execution import ContractOutcome
from live15_quant.model_zoo import DatasetExample
from live15_quant.models import Asset

DEFAULT_PATH_HORIZONS = (
    timedelta(seconds=30),
    timedelta(minutes=1),
    timedelta(minutes=3),
    timedelta(minutes=5),
)
DEFAULT_MICROSTRUCTURE_HORIZONS = (
    timedelta(seconds=10),
    timedelta(seconds=30),
    timedelta(seconds=60),
)


class ModelV3Error(RuntimeError):
    """A v3 lineage, timestamp, or decision invariant was violated."""


class RegimeLabel(StrEnum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING_CHOPPY = "ranging_choppy"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    REVERSAL_RISK_ELEVATED = "reversal_risk_elevated"
    UNKNOWN = "unknown"


class DynamicDecisionAction(StrEnum):
    BUY_YES = "buy_yes"
    BUY_NO = "buy_no"
    HOLD = "hold"
    ADD = "add"
    REDUCE = "reduce"
    TAKE_PROFIT = "take_profit"
    CUT_LOSS = "cut_loss"
    CLOSE = "close"
    HOLD_TO_SETTLEMENT = "hold_to_settlement"
    DATA_UNAVAILABLE = "data_unavailable"


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ModelV3Error("v3 timestamps must be UTC-aware")
    return value.isoformat(timespec="microseconds")


def _probability(value: Decimal, field: str) -> None:
    if not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
        raise ModelV3Error(f"{field} must be a finite probability")


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class HorizonPrediction:
    """A path prediction calculated exclusively from information at ``as_of``."""

    horizon: timedelta
    expected_return: Decimal | None
    probability_up: Decimal | None
    probability_down: Decimal | None
    trend_strength: Decimal | None
    reversal_risk: Decimal | None

    def __post_init__(self) -> None:
        if self.horizon <= timedelta(0):
            raise ModelV3Error("prediction horizon must be positive")
        for name, value in (
            ("probability_up", self.probability_up),
            ("probability_down", self.probability_down),
            ("trend_strength", self.trend_strength),
            ("reversal_risk", self.reversal_risk),
        ):
            if value is not None:
                _probability(value, name)
        if (
            self.probability_up is not None
            and self.probability_down is not None
            and self.probability_up + self.probability_down > Decimal(1)
        ):
            raise ModelV3Error("path up/down probabilities cannot exceed one")
        if self.expected_return is not None and not self.expected_return.is_finite():
            raise ModelV3Error("expected return must be finite")


@dataclass(frozen=True, slots=True)
class PathExpertPrediction:
    asset: Asset
    as_of: datetime
    horizons: tuple[HorizonPrediction, ...]
    provider: str
    artifact_hash: str
    data_status: str = "ready"
    data_reason: str | None = None

    def __post_init__(self) -> None:
        _utc_timestamp(self.as_of)
        seconds = tuple(int(item.horizon.total_seconds()) for item in self.horizons)
        if seconds != tuple(sorted(set(seconds))):
            raise ModelV3Error("path horizons must be unique and ascending")
        if self.data_status == "ready" and not self.horizons:
            raise ModelV3Error("ready path prediction requires at least one horizon")

    def at(self, horizon: timedelta) -> HorizonPrediction | None:
        return next((item for item in self.horizons if item.horizon == horizon), None)


@dataclass(frozen=True, slots=True)
class MicrostructureExpertPrediction:
    asset: Asset
    ticker: str
    as_of: datetime
    horizons: tuple[HorizonPrediction, ...]
    provider: str
    artifact_hash: str
    data_status: str = "ready"
    data_reason: str | None = None

    def __post_init__(self) -> None:
        _utc_timestamp(self.as_of)
        if not self.ticker:
            raise ModelV3Error("microstructure prediction needs a ticker")
        seconds = tuple(int(item.horizon.total_seconds()) for item in self.horizons)
        if seconds != tuple(sorted(set(seconds))):
            raise ModelV3Error("microstructure horizons must be unique and ascending")
        if self.data_status == "ready" and not self.horizons:
            raise ModelV3Error("ready microstructure prediction requires horizons")

    def at(self, horizon: timedelta) -> HorizonPrediction | None:
        return next((item for item in self.horizons if item.horizon == horizon), None)


@dataclass(frozen=True, slots=True)
class RegimePrediction:
    asset: Asset
    as_of: datetime
    labels: frozenset[RegimeLabel]
    confidence: Decimal | None
    provider: str
    artifact_hash: str
    data_status: str = "ready"
    data_reason: str | None = None

    def __post_init__(self) -> None:
        _utc_timestamp(self.as_of)
        if self.confidence is not None:
            _probability(self.confidence, "regime confidence")
        if self.data_status == "ready" and not self.labels:
            raise ModelV3Error("ready regime prediction needs a label")


@dataclass(frozen=True, slots=True)
class TerminalProbabilityPrediction:
    asset: Asset
    ticker: str
    as_of: datetime
    probability_yes: Decimal
    provider: str
    artifact_hash: str
    data_status: str = "ready"
    data_reason: str | None = None

    def __post_init__(self) -> None:
        _utc_timestamp(self.as_of)
        _probability(self.probability_yes, "terminal yes probability")


@dataclass(frozen=True, slots=True)
class DynamicPosition:
    outcome: ContractOutcome
    quantity: Decimal
    average_cost: Decimal
    unrealized_pnl: Decimal

    def __post_init__(self) -> None:
        if self.quantity <= 0 or not self.quantity.is_finite():
            raise ModelV3Error("position quantity must be finite and positive")
        if not self.average_cost.is_finite() or not Decimal(0) <= self.average_cost <= Decimal(1):
            raise ModelV3Error("position cost must be in [0, 1]")
        if not self.unrealized_pnl.is_finite():
            raise ModelV3Error("position unrealized PnL must be finite")


@dataclass(frozen=True, slots=True)
class DynamicDecisionContext:
    asset: Asset
    ticker: str
    as_of: datetime
    window_end: datetime
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None
    terminal: TerminalProbabilityPrediction | None
    path: PathExpertPrediction | None
    microstructure: MicrostructureExpertPrediction | None
    regime: RegimePrediction | None
    position: DynamicPosition | None = None
    estimated_entry_fee: Decimal = Decimal("0")
    estimated_exit_fee: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        _utc_timestamp(self.as_of)
        _utc_timestamp(self.window_end)
        if self.window_end <= self.as_of:
            raise ModelV3Error("decision cannot be after contract expiry")
        for name, value in (
            ("yes_bid", self.yes_bid),
            ("yes_ask", self.yes_ask),
            ("no_bid", self.no_bid),
            ("no_ask", self.no_ask),
        ):
            if value is not None and (
                not value.is_finite() or not Decimal(0) <= value <= Decimal(1)
            ):
                raise ModelV3Error(f"{name} must be in [0, 1]")
        if (
            not self.estimated_entry_fee.is_finite()
            or not self.estimated_exit_fee.is_finite()
            or self.estimated_entry_fee < 0
            or self.estimated_exit_fee < 0
        ):
            raise ModelV3Error("estimated fees must be finite and non-negative")
        for bid, ask, name in (
            (self.yes_bid, self.yes_ask, "YES"),
            (self.no_bid, self.no_ask, "NO"),
        ):
            if bid is not None and ask is not None and bid > ask:
                raise ModelV3Error(f"{name} executable bid cannot exceed ask")
        for expert in (self.terminal, self.path, self.microstructure, self.regime):
            if expert is None:
                continue
            if expert.asset is not self.asset:
                raise ModelV3Error("v3 expert asset must match the decision asset")
            if expert.as_of > self.as_of:
                raise ModelV3Error("v3 expert cannot use information after decision time")
        if self.terminal is not None and self.terminal.ticker != self.ticker:
            raise ModelV3Error("terminal expert ticker must match the decision ticker")
        if self.microstructure is not None and self.microstructure.ticker != self.ticker:
            raise ModelV3Error("microstructure expert ticker must match the decision ticker")


@dataclass(frozen=True, slots=True)
class DynamicDecision:
    action: DynamicDecisionAction
    outcome: ContractOutcome | None
    expected_value_close_now: Decimal | None
    expected_value_hold: Decimal | None
    expected_value_add: Decimal | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DynamicDecisionConfig:
    minimum_entry_edge: Decimal = Decimal("0.075")
    minimum_add_edge: Decimal = Decimal("0.10")
    hold_to_settlement_seconds: int = 60
    exit_value_margin: Decimal = Decimal("0.01")
    elevated_reversal_risk: Decimal = Decimal("0.65")
    require_short_horizon_confirmation: bool = True

    def __post_init__(self) -> None:
        for value in (self.minimum_entry_edge, self.minimum_add_edge, self.exit_value_margin):
            if value < 0 or not value.is_finite():
                raise ValueError("dynamic decision edges must be finite and non-negative")
        if not self.elevated_reversal_risk.is_finite() or not Decimal(
            0
        ) <= self.elevated_reversal_risk <= Decimal(1):
            raise ValueError("elevated reversal risk must be a finite probability")
        if self.hold_to_settlement_seconds <= 0:
            raise ValueError("hold-to-settlement duration must be positive")


class DynamicDecisionEngine:
    """Explicit EV decision policy; it performs no execution or state mutation."""

    def __init__(self, config: DynamicDecisionConfig | None = None) -> None:
        self.config = config or DynamicDecisionConfig()

    def evaluate(self, context: DynamicDecisionContext) -> DynamicDecision:
        if not self._ready(context):
            return DynamicDecision(
                DynamicDecisionAction.DATA_UNAVAILABLE,
                None,
                None,
                None,
                None,
                ("required_expert_or_executable_book_unavailable",),
            )
        assert context.terminal is not None
        if context.position is None:
            return self._flat(context)
        return self._positioned(context)

    def _ready(self, context: DynamicDecisionContext) -> bool:
        experts = (context.terminal, context.path, context.microstructure, context.regime)
        if any(item is None or item.data_status != "ready" for item in experts):
            return False
        assert context.regime is not None
        if RegimeLabel.UNKNOWN in context.regime.labels:
            return False
        if not all(
            value is not None
            for value in (context.yes_bid, context.yes_ask, context.no_bid, context.no_ask)
        ):
            return False
        assert context.path is not None and context.microstructure is not None
        path = context.path.at(timedelta(seconds=30))
        book = context.microstructure.at(timedelta(seconds=30))
        return all(
            value is not None
            for value in (
                path,
                book,
                path.probability_up if path is not None else None,
                path.probability_down if path is not None else None,
                path.reversal_risk if path is not None else None,
                book.probability_up if book is not None else None,
                book.probability_down if book is not None else None,
                book.reversal_risk if book is not None else None,
            )
        )

    def _flat(self, context: DynamicDecisionContext) -> DynamicDecision:
        assert context.terminal is not None
        yes_value = context.terminal.probability_yes - context.yes_ask - context.estimated_entry_fee
        no_value = (
            Decimal(1)
            - context.terminal.probability_yes
            - context.no_ask
            - context.estimated_entry_fee
        )
        outcome = ContractOutcome.YES if yes_value >= no_value else ContractOutcome.NO
        value = max(yes_value, no_value)
        if value < self.config.minimum_entry_edge:
            return DynamicDecision(
                DynamicDecisionAction.HOLD, None, None, None, None, ("entry_edge_below_floor",)
            )
        if not self._short_horizon_confirms(context, outcome):
            return DynamicDecision(
                DynamicDecisionAction.HOLD,
                None,
                None,
                None,
                None,
                ("short_horizon_not_confirmed",),
            )
        return DynamicDecision(
            DynamicDecisionAction.BUY_YES
            if outcome is ContractOutcome.YES
            else DynamicDecisionAction.BUY_NO,
            outcome,
            None,
            None,
            value,
            ("after_cost_terminal_edge_and_path_confirm",),
        )

    def _positioned(self, context: DynamicDecisionContext) -> DynamicDecision:
        assert context.terminal is not None and context.position is not None
        probability = (
            context.terminal.probability_yes
            if context.position.outcome is ContractOutcome.YES
            else Decimal(1) - context.terminal.probability_yes
        )
        bid = context.yes_bid if context.position.outcome is ContractOutcome.YES else context.no_bid
        assert bid is not None
        close_value = bid - context.estimated_exit_fee
        hold_value = probability
        horizon = context.window_end - context.as_of
        reversal = self._reversal_risk(context)
        if (
            horizon <= timedelta(seconds=self.config.hold_to_settlement_seconds)
            and hold_value >= close_value + self.config.exit_value_margin
            and reversal < self.config.elevated_reversal_risk
        ):
            return DynamicDecision(
                DynamicDecisionAction.HOLD_TO_SETTLEMENT,
                context.position.outcome,
                close_value,
                hold_value,
                None,
                ("terminal_ev_dominates_close_near_expiry",),
            )
        if (
            reversal >= self.config.elevated_reversal_risk
            and close_value > context.position.average_cost
        ):
            return DynamicDecision(
                DynamicDecisionAction.TAKE_PROFIT,
                context.position.outcome,
                close_value,
                hold_value,
                None,
                ("reversal_risk_elevated_and_close_is_profitable",),
            )
        if hold_value + self.config.exit_value_margin < close_value:
            return DynamicDecision(
                self._close_action(close_value, context.position.average_cost),
                context.position.outcome,
                close_value,
                hold_value,
                None,
                ("close_ev_exceeds_continue_ev",),
            )
        if not self._short_horizon_confirms(context, context.position.outcome):
            action = (
                DynamicDecisionAction.CUT_LOSS
                if context.position.unrealized_pnl < 0
                else DynamicDecisionAction.REDUCE
            )
            return DynamicDecision(
                action,
                context.position.outcome,
                close_value,
                hold_value,
                None,
                ("short_horizon_reversal_or_edge_loss",),
            )
        add_value = (
            hold_value
            - (
                context.yes_ask
                if context.position.outcome is ContractOutcome.YES
                else context.no_ask
            )
            - context.estimated_entry_fee
        )
        if (
            add_value >= self.config.minimum_add_edge
            and reversal < self.config.elevated_reversal_risk
        ):
            return DynamicDecision(
                DynamicDecisionAction.ADD,
                context.position.outcome,
                close_value,
                hold_value,
                add_value,
                ("incremental_after_cost_ev_and_path_confirm",),
            )
        return DynamicDecision(
            DynamicDecisionAction.HOLD,
            context.position.outcome,
            close_value,
            hold_value,
            add_value,
            ("continue_ev_not_dominated",),
        )

    @staticmethod
    def _close_action(close_value: Decimal, average_cost: Decimal) -> DynamicDecisionAction:
        """Classify an EV-dominated exit without pretending every exit is profitable."""
        if close_value > average_cost:
            return DynamicDecisionAction.TAKE_PROFIT
        if close_value < average_cost:
            return DynamicDecisionAction.CUT_LOSS
        return DynamicDecisionAction.CLOSE

    def _short_horizon_confirms(
        self, context: DynamicDecisionContext, outcome: ContractOutcome
    ) -> bool:
        if not self.config.require_short_horizon_confirmation:
            return True
        assert context.path is not None and context.microstructure is not None
        horizon = timedelta(seconds=30)
        path = context.path.at(horizon)
        book = context.microstructure.at(horizon)
        assert path is not None and book is not None
        assert path.probability_up is not None and path.probability_down is not None
        assert book.probability_up is not None and book.probability_down is not None
        if outcome is ContractOutcome.YES:
            return path.probability_up >= Decimal("0.5") and book.probability_up >= Decimal("0.5")
        return path.probability_down >= Decimal("0.5") and book.probability_down >= Decimal("0.5")

    @staticmethod
    def _reversal_risk(context: DynamicDecisionContext) -> Decimal:
        assert (
            context.path is not None
            and context.microstructure is not None
            and context.regime is not None
        )
        predictions = tuple(context.path.horizons) + tuple(context.microstructure.horizons)
        values = [item.reversal_risk for item in predictions if item.reversal_risk is not None]
        if RegimeLabel.REVERSAL_RISK_ELEVATED in context.regime.labels:
            values.append(Decimal(1))
        return max(values, default=Decimal(0))


@dataclass(frozen=True, slots=True)
class RegimeInputs:
    as_of: datetime
    short_return: Decimal | None
    medium_return: Decimal | None
    realized_volatility: Decimal | None
    reversal_score: Decimal | None


class RuleAssistedRegimeExpert:
    """A transparent first v3 regime layer, usable before sequence-model evidence exists."""

    artifact_hash = "rule-assisted-regime-v1"

    def predict(self, asset: Asset, values: RegimeInputs) -> RegimePrediction:
        _utc_timestamp(values.as_of)
        if any(
            value is None
            for value in (
                values.short_return,
                values.medium_return,
                values.realized_volatility,
                values.reversal_score,
            )
        ):
            return RegimePrediction(
                asset,
                values.as_of,
                frozenset(),
                None,
                "rule_assisted_regime",
                self.artifact_hash,
                "data_unavailable",
                "required_as_of_regime_input_missing",
            )
        assert values.short_return is not None
        assert values.medium_return is not None
        assert values.realized_volatility is not None
        assert values.reversal_score is not None
        if not all(
            value.is_finite()
            for value in (
                values.short_return,
                values.medium_return,
                values.realized_volatility,
                values.reversal_score,
            )
        ):
            raise ModelV3Error("regime inputs must be finite decision-time values")
        labels: set[RegimeLabel] = set()
        if values.reversal_score >= Decimal("0.65"):
            labels.add(RegimeLabel.REVERSAL_RISK_ELEVATED)
        if values.realized_volatility >= Decimal("0.01"):
            labels.add(RegimeLabel.HIGH_VOLATILITY)
        elif values.realized_volatility <= Decimal("0.002"):
            labels.add(RegimeLabel.LOW_VOLATILITY)
        if values.short_return > 0 and values.medium_return > 0:
            labels.add(RegimeLabel.TRENDING_UP)
        elif values.short_return < 0 and values.medium_return < 0:
            labels.add(RegimeLabel.TRENDING_DOWN)
        else:
            labels.add(RegimeLabel.RANGING_CHOPPY)
        confidence = min(Decimal(1), abs(values.short_return) + abs(values.medium_return))
        return RegimePrediction(
            asset,
            values.as_of,
            frozenset(labels),
            confidence,
            "rule_assisted_regime",
            self.artifact_hash,
        )


class V3TerminalProvider(Protocol):
    """Future v3 plugins expose a terminal probability without owning execution."""

    artifact_hash: str

    def terminal(self, row: DatasetExample) -> TerminalProbabilityPrediction:
        """Return a decision-time terminal probability prediction."""


class V3ForwardPredictionAdapter:
    """Adapts a v3 terminal expert to Paper/Shadow's stable prediction protocol."""

    def __init__(self, candidate_id: str, provider: V3TerminalProvider) -> None:
        if not candidate_id:
            raise ValueError("v3 forward candidate id must not be empty")
        self.candidate_id = candidate_id
        self.provider = provider
        self.artifact_hash = provider.artifact_hash

    def predict(self, model_id: str, row: DatasetExample) -> Decimal:
        if model_id != self.candidate_id:
            raise ModelV3Error("v3 provider was called for an unowned candidate")
        result = self.provider.terminal(row)
        if result.data_status != "ready":
            raise ModelV3Error("v3 terminal expert is not ready for forward prediction")
        if result.asset.value != row.asset or result.ticker != row.ticker:
            raise ModelV3Error("v3 terminal provider identity does not match the forward row")
        if result.as_of > row.decision_timestamp:
            raise ModelV3Error("v3 terminal provider used information after forward decision time")
        return result.probability_yes


@dataclass(frozen=True, slots=True)
class UpstreamArchitecture:
    name: str
    paper_url: str
    source_url: str
    upstream_commit: str
    license_name: str
    original_task: str
    input_shape: str
    model_size: str
    training_data_requirement: str
    expected_inference_latency: str
    fit_for_live15: str
    decision: str


UPSTREAM_ARCHITECTURES = (
    UpstreamArchitecture(
        "DeepLOB (paper architecture only)",
        "https://arxiv.org/abs/1808.03668",
        "https://github.com/zcakhaa/DeepLOB-Deep-Convolutional-Neural-Networks-for-Limit-Order-Books",
        "ff14d7c2fd38bdfc143389786993d0f0236d4eb8",
        "UNVERIFIED (no repository LICENSE found at pinned commit)",
        "multi-class short-horizon equity LOB mid-price movement",
        "[batch, lookback, price/size levels]",
        "not measured locally; convolutional/inception/LSTM sequence network",
        "many independent, gap-free LOB event sequences with event-grouped temporal holdouts",
        "not benchmarked; deferred until a certified LIVE15 sequence dataset exists",
        (
            "Use only the documented CNN/inception/LSTM ideas after an independent "
            "LIVE15 sequence dataset exists."
        ),
        "RESEARCH_ONLY_NO_CODE_VENDORING",
    ),
    UpstreamArchitecture(
        "PatchTST",
        "https://arxiv.org/abs/2211.14730",
        "https://github.com/yuqinie98/PatchTST",
        "204c21efe0b39603ad6e2ca640ef5896646ab1a9",
        "Apache-2.0",
        "long-horizon multivariate time-series forecasting",
        "[batch, channels, context_length] patches",
        "configurable Transformer; no LIVE15 parameter count selected",
        "multiple independent temporal regimes beyond the current archive window",
        "not benchmarked; deferred because attention is not justified by current evidence",
        (
            "Useful challenger for underlying path forecasting, but over-parameterized until "
            "multiple days of independent sequence evidence exist."
        ),
        "DEFERRED_SEQUENCE_EVIDENCE",
    ),
    UpstreamArchitecture(
        "Temporal Convolutional Network",
        "https://arxiv.org/abs/1803.01271",
        "https://github.com/locuslab/TCN",
        "2f8c2b817050206397458dfd1f5a25ce8a32fe65",
        "MIT",
        "causal sequence modeling",
        "[batch, channels, time]",
        "small configurable causal convolution stack",
        "certified event-grouped path and order-book sequences with enough daily diversity",
        "expected low latency after local benchmark; no unverified latency claim",
        (
            "Selected future low-latency path/microstructure sequence backbone; causal "
            "receptive field and small inference footprint fit 10/30/60-second horizons."
        ),
        "SELECTED_WHEN_SEQUENCE_EVIDENCE_SUFFICIENT",
    ),
    UpstreamArchitecture(
        "XGBoost",
        "https://doi.org/10.1145/2939672.2939785",
        "https://github.com/dmlc/xgboost",
        "379b29f0836b9dbc313b993d8e5743bd452d4117",
        "Apache-2.0",
        "regularized gradient-boosted structured prediction",
        "[rows, structured as-of features]",
        "small tree ensemble; hyperparameters frozen only in a future development artifact",
        "structured chronological folds; available before deep sequence evidence",
        "already acceptable for structured development; v3 multi-horizon benchmark pending",
        (
            "Selected now for multi-horizon path/regime baselines because it is already pinned, "
            "deterministic, and needs materially less data than deep sequence models."
        ),
        "SELECTED_STRUCTURED_BASELINE",
    ),
)


def architecture_manifest() -> dict[str, object]:
    """Machine-readable, deterministic research and source-lineage manifest."""

    payload = {
        "format": "live15-model-architecture-v3-research-v1",
        "terminal_probability_role": "reuse frozen v2 terminal experts; do not retrain here",
        "forward_contract": "ForwardPredictionProvider-compatible V3ForwardPredictionAdapter",
        "path_horizons_seconds": [int(item.total_seconds()) for item in DEFAULT_PATH_HORIZONS],
        "microstructure_horizons_seconds": [
            int(item.total_seconds()) for item in DEFAULT_MICROSTRUCTURE_HORIZONS
        ],
        "upstreams": [
            {
                "name": item.name,
                "paper_url": item.paper_url,
                "source_url": item.source_url,
                "upstream_commit": item.upstream_commit,
                "license": item.license_name,
                "original_task": item.original_task,
                "input_shape": item.input_shape,
                "model_size": item.model_size,
                "training_data_requirement": item.training_data_requirement,
                "expected_inference_latency": item.expected_inference_latency,
                "fit_for_live15": item.fit_for_live15,
                "decision": item.decision,
            }
            for item in UPSTREAM_ARCHITECTURES
        ],
    }
    payload["deterministic_hash"] = _canonical_hash(payload)
    return payload
