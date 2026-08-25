from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from live15_quant.model_vnext_contract import (
    DATASET_V1_ID,
    ContractSide,
    DecisionTimeContract,
    FinalTestGuardError,
    LeakageChecker,
    LeakageError,
    ObservationProvenance,
    PathObservation,
    PathTargetSpec,
    TargetUnavailableError,
    assert_final_test_not_consumed,
    build_path_target,
    contract_manifest,
    required_purge_embargo_seconds,
    select_path_observation,
    validate_event_group_splits,
)

NOW = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)
WINDOW_END = NOW + timedelta(minutes=15)


def _contract() -> DecisionTimeContract:
    return DecisionTimeContract(
        event_id="event-1",
        ticker="KXBTC15M-26AUG260000-00",
        window_start=NOW,
        window_end=WINDOW_END,
        decision_timestamp=NOW + timedelta(minutes=10),
        target_level=Decimal("100000"),
        side=ContractSide.YES,
        lookback_seconds=(15, 60, 300),
    )


@dataclass(frozen=True)
class Row:
    ticker: str


def test_feature_receive_timestamp_after_decision_is_rejected() -> None:
    observation = ObservationProvenance(
        "btc",
        _contract().decision_timestamp,
        _contract().decision_timestamp + timedelta(seconds=1),
    )
    with pytest.raises(LeakageError, match="received_timestamp"):
        _contract().validate_feature_observation(observation)


def test_source_timestamp_after_decision_is_rejected_even_when_received_early() -> None:
    observation = ObservationProvenance(
        "btc",
        _contract().decision_timestamp,
        _contract().decision_timestamp - timedelta(seconds=1),
        source_timestamp=_contract().decision_timestamp + timedelta(seconds=1),
    )
    with pytest.raises(LeakageError, match="source_timestamp"):
        _contract().validate_feature_observation(observation)


def test_backfill_and_synthetic_features_fail_closed() -> None:
    for kwargs, rule in (
        ({"backfilled": True}, "backfill"),
        ({"synthetic": True}, "rolling-window"),
    ):
        observation = ObservationProvenance("btc", NOW, NOW, **kwargs)
        with pytest.raises(LeakageError, match=rule):
            _contract().validate_feature_observation(observation)


def test_path_target_uses_exact_future_observation_and_formula() -> None:
    contract = _contract()
    target = build_path_target(
        contract,
        PathTargetSpec(30),
        base_value=Decimal("100"),
        observations=(
            PathObservation(contract.decision_timestamp + timedelta(seconds=31), Decimal("101")),
        ),
    )
    assert target.target_timestamp == contract.decision_timestamp + timedelta(seconds=31)
    assert target.return_value == Decimal("0.01")


def test_path_target_missing_observation_is_not_forward_filled() -> None:
    contract = _contract()
    with pytest.raises(TargetUnavailableError, match="no observed target"):
        select_path_observation(
            contract,
            PathTargetSpec(30, tolerance_seconds=1),
            (PathObservation(contract.decision_timestamp + timedelta(seconds=35), Decimal("101")),),
        )


def test_terminal_window_end_target_is_allowed_only_at_window_end() -> None:
    contract = _contract()
    target = build_path_target(
        contract,
        PathTargetSpec("window_end"),
        base_value=Decimal("100"),
        observations=(PathObservation(contract.window_end, Decimal("102")),),
    )
    assert target.target_timestamp == contract.window_end


def test_same_event_cannot_cross_formal_splits() -> None:
    with pytest.raises(LeakageError, match="crosses"):
        validate_event_group_splits({"train": (Row("event-a"),), "validation": (Row("event-a"),)})


def test_purge_embargo_is_derived_from_temporal_span() -> None:
    assert required_purge_embargo_seconds(max_lookback_seconds=300, max_horizon_seconds=300) == 600


def test_final_test_lineage_is_allowed_but_consumption_is_rejected() -> None:
    assert_final_test_not_consumed(DATASET_V1_ID, purpose="lineage", rows_consumed=False)
    with pytest.raises(FinalTestGuardError, match="revealed final test"):
        assert_final_test_not_consumed(
            DATASET_V1_ID, purpose="feature selection", rows_consumed=True
        )


def test_normalization_fit_must_be_train_only() -> None:
    with pytest.raises(LeakageError, match="train only"):
        LeakageChecker().check_normalization("validation")
    LeakageChecker().check_normalization("train")


def test_leakage_checker_rejects_settlement_derived_feature() -> None:
    with pytest.raises(LeakageError, match="settlement-derived"):
        LeakageChecker().check_feature_names(("return_30s", "settlement_result"))


def test_contract_manifest_freezes_horizons_and_final_test_policy() -> None:
    manifest = contract_manifest()
    assert manifest["contract"] == "MVN-001"
    assert manifest["path_targets"]["horizons_seconds"] == [5, 15, 30, 60, 120, 180, 300]
    assert manifest["frozen_final_test"]["dataset_id"] == DATASET_V1_ID
    assert manifest["frozen_final_test"]["vnext_consumption"] is False
