from datetime import UTC, datetime, timedelta

import pytest

from live15_quant.factor_factory import (
    DATASET_V2_HOLDOUT_STATE,
    DATASET_V2_ID,
    ComplexityBudget,
    EvaluationPlan,
    FactorComplexityExceeded,
    FactorContext,
    FactorEvaluationBlocked,
    FactorRecord,
    FactorRow,
    FactorValue,
    FactorZoo,
    MissingReason,
    SafeFactorVM,
    SearchBudget,
    TimedValue,
    canonical_expression,
    constant,
    demo_factor_candidates,
    evaluate_factor,
    make_factor,
    operation,
    parse_expression,
    primitive,
    redundancy_diagnostic,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def point(
    timestamp: datetime,
    value: float | None,
    *,
    reason: str | None = None,
    received: datetime | None = None,
    source: datetime | None = None,
) -> TimedValue:
    return TimedValue(timestamp, value, received or timestamp, source or timestamp, reason)


def make_context(
    value: float = 1.0, *, history: tuple[TimedValue, ...] = (), current_name: str = "return_15s"
) -> FactorContext:
    return FactorContext(NOW, {current_name: point(NOW, value)}, {current_name: history})


def test_parse_canonical_and_identity_are_deterministic() -> None:
    expression = parse_expression('{"op":"ADD","args":[{"feature":"return_15s"},{"const":1}]}')
    assert (
        canonical_expression(expression)
        == '{"args":[{"feature":"return_15s"},{"const":1.0}],"op":"ADD"}'
    )
    first = make_factor(expression, experiment_id="exp")
    second = make_factor(parse_expression(canonical_expression(expression)), experiment_id="other")
    assert first.factor_id == second.factor_id
    assert first.dataset_id == DATASET_V2_ID


def test_invalid_expression_is_rejected() -> None:
    with pytest.raises(ValueError, match="UNKNOWN_FACTOR_OPERATOR"):
        parse_expression('{"op":"POW","args":[{"const":2}]}')
    with pytest.raises(ValueError, match="UNKNOWN_PRIMITIVE_FEATURE"):
        parse_expression('{"feature":"not_a_feature"}')
    with pytest.raises(ValueError, match="arity"):
        parse_expression('{"op":"ADD","args":[{"const":1}]}')


def test_complexity_budget_has_exact_failure_code() -> None:
    expression = operation(
        "NEG", operation("ABS", operation("SIGN", operation("NEG", primitive("return_15s"))))
    )
    with pytest.raises(FactorComplexityExceeded, match="FACTOR_COMPLEXITY_EXCEEDED"):
        make_factor(
            expression,
            experiment_id="exp",
            budget=ComplexityBudget(
                max_depth=3, max_operators=5, max_primitives=6, max_lookback_seconds=300
            ),
        )


def test_safe_div_zero_and_missing_propagation() -> None:
    vm = SafeFactorVM()
    zero = make_factor(operation("SAFE_DIV", constant(1), constant(0)), experiment_id="exp")
    assert vm.evaluate(zero, make_context()).missing_reason == MissingReason.DIVISION_BY_ZERO
    missing = make_factor(operation("ABS", primitive("return_15s")), experiment_id="exp")
    result = vm.evaluate(
        missing, FactorContext(NOW, {"return_15s": point(NOW, None, reason="stale")})
    )
    assert result == FactorValue.missing("stale")


def test_temporal_no_lookahead_and_delay_are_deterministic() -> None:
    future = make_factor(primitive("return_15s"), experiment_id="exp")
    future_context = FactorContext(NOW, {"return_15s": point(NOW + timedelta(seconds=1), 2.0)})
    with pytest.raises(FactorEvaluationBlocked, match="FACTOR_LOOKAHEAD_DETECTED"):
        SafeFactorVM().evaluate(future, future_context)

    history = (point(NOW - timedelta(seconds=2), 2.0), point(NOW - timedelta(seconds=1), 3.0))
    delayed = make_factor(operation("DELAY1", primitive("return_15s")), experiment_id="exp")
    assert SafeFactorVM().evaluate(delayed, make_context(4.0, history=history)).value == 3.0


def test_lookback_and_purge_contract() -> None:
    factor = make_factor(primitive("return_300s"), experiment_id="exp")
    assert factor.required_lookback_seconds == 300
    with pytest.raises(FactorComplexityExceeded, match="FACTOR_COMPLEXITY_EXCEEDED"):
        make_factor(
            primitive("return_300s"),
            experiment_id="exp",
            budget=ComplexityBudget(max_lookback_seconds=60),
        )


def test_v2_identity_holdout_guard_and_leakage_checker() -> None:
    factor = make_factor(primitive("return_15s"), experiment_id="exp")
    plan = EvaluationPlan("exp")
    row = FactorRow(make_context(), 0.1, "train", "event-1", "BTC", "2026-08-26")
    result = evaluate_factor(factor, [row], plan)
    assert result.dataset_id == DATASET_V2_ID
    assert plan.holdout_state == DATASET_V2_HOLDOUT_STATE

    future_row = FactorRow(
        make_context(),
        0.1,
        "train",
        "event-future",
        "BTC",
        "2026-08-26",
        target_timestamp=NOW + timedelta(seconds=15),
        window_end=NOW + timedelta(seconds=30),
    )
    assert evaluate_factor(factor, [future_row], plan).train.examples == 1
    past_row = FactorRow(
        make_context(),
        0.1,
        "train",
        "event-past",
        "BTC",
        "2026-08-26",
        target_timestamp=NOW,
    )
    with pytest.raises(FactorEvaluationBlocked, match="TARGET_NOT_IN_FUTURE"):
        evaluate_factor(factor, [past_row], plan)

    holdout_row = FactorRow(make_context(), 0.1, "test", "event-2", "BTC", "2026-08-26")
    with pytest.raises(FactorEvaluationBlocked, match="HOLDOUT_ACCESS_PROHIBITED"):
        evaluate_factor(factor, [holdout_row], plan)

    split_crossing = [
        row,
        FactorRow(make_context(), 0.2, "validation", "event-1", "BTC", "2026-08-26"),
    ]
    with pytest.raises(FactorEvaluationBlocked, match="EVENT_CROSSES_SPLIT"):
        evaluate_factor(factor, split_crossing, plan)

    bad_provenance = FactorContext(
        NOW,
        {"return_15s": TimedValue(NOW, 1.0, NOW + timedelta(seconds=1), NOW)},
    )
    with pytest.raises(FactorEvaluationBlocked, match="FACTOR_LEAKAGE_CHECK_FAILED"):
        SafeFactorVM().evaluate(factor, bad_provenance)


def test_search_budget_zoo_lineage_and_redundancy() -> None:
    candidates = demo_factor_candidates()
    assert len(candidates) == 6
    assert len({candidate.factor_id for candidate in candidates}) == len(candidates)
    budget = SearchBudget(max_candidates=2)
    budget.claim("demo")
    budget.claim("demo")
    with pytest.raises(FactorEvaluationBlocked, match="FACTOR_SEARCH_BUDGET_EXCEEDED"):
        budget.claim("demo")

    zoo = FactorZoo("exp")
    zoo.add(FactorRecord(candidates[0], "PROPOSED", code_sha="abc"))
    manifest = zoo.manifest(budget)
    assert manifest["factors"][0]["factor_id"] == candidates[0].factor_id

    other = make_factor(primitive("return_momentum"), experiment_id="exp")
    diagnostic = redundancy_diagnostic(candidates[0], [1.0, 2.0, 3.0], [(other, [1.0, 2.0, 3.0])])
    item = diagnostic[other.factor_id]
    assert item.preliminary_redundant is True
    assert "return_momentum" in item.primitive_overlap
