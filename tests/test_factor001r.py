from tools.run_factor001r import (
    DATASET_BUILD_HASH,
    DATASET_V2_ID,
    FDR_ALPHA,
    MAX_CANDIDATES,
    _bh_fdr,
    _split_token,
    build_candidates,
    experiment_identity,
)


def test_candidate_generation_is_frozen_and_deterministic() -> None:
    first = build_candidates(experiment_id="exp")
    second = build_candidates(experiment_id="exp")
    assert len(first) == MAX_CANDIDATES == 96
    assert [candidate.spec.factor_id for candidate in first] == [
        candidate.spec.factor_id for candidate in second
    ]
    assert {candidate.family for candidate in first} == {"F0", "F1", "F2", "F3", "F4"}
    assert all(candidate.spec.required_lookback_seconds <= 300 for candidate in first)


def test_benjamini_hochberg_is_deterministic_and_bounded() -> None:
    qvalues = _bh_fdr({"a": 0.001, "b": 0.02, "c": 0.8, "missing": None})
    assert qvalues["a"] <= qvalues["b"] <= qvalues["c"]
    assert qvalues["missing"] is None
    assert qvalues["a"] <= FDR_ALPHA


def test_experiment_identity_carries_frozen_lineage() -> None:
    manifest = {"registered_cutoff": "2026-08-25T19:35:14.898895+00:00"}
    first = experiment_identity(manifest=manifest, code_sha="abc")
    second = experiment_identity(manifest=manifest, code_sha="abc")
    assert first == second
    experiment_id, contract = first
    assert len(experiment_id) == 24
    assert contract["dataset_id"] == DATASET_V2_ID
    assert contract["build_hash"] == DATASET_BUILD_HASH
    assert contract["max_candidates"] == MAX_CANDIDATES


def test_holdout_split_token_is_checked_without_decoding_payload() -> None:
    assert _split_token('{"asset":"BTC","split":"test","payload":"not decoded"}') == "test"
