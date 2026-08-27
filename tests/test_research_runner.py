from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from live15_quant import research_runner as runner_module
from live15_quant.canonical_evidence import (
    CoverageScope,
    EvidenceRecord,
    build_canonical_evidence_snapshot,
)
from live15_quant.cli import research_preflight_main
from live15_quant.research_data_authority import (
    FeatureFreshnessPolicy,
    ForwardOosFreshnessPolicy,
    FrozenHoldoutMetadata,
    ResearchFreshnessPolicy,
    ResearchObservation,
    ResearchSourceManifest,
    ResearchSourceType,
    ResearchUniverseBuilder,
    SessionSemantics,
    TrainingRecencyPolicy,
    TrustTier,
)
from live15_quant.research_runner import (
    CheckpointCorruptError,
    ExperimentConflictError,
    ExperimentLockError,
    HashWorkAdapter,
    ResearchRunConfig,
    ResearchRunInput,
    ResearchRunner,
    ResearchRunnerError,
    ResearchRunnerPathError,
    ResumeMismatchError,
    load_research_input_snapshot,
    write_research_input_snapshot,
)

START = datetime(2026, 8, 1, tzinfo=UTC)
CUTOFF = START + timedelta(days=1)


def approved_input() -> ResearchRunInput:
    source = ResearchSourceManifest(
        source_id="h0-test",
        source_type=ResearchSourceType.OWN_RECORDER,
        trust_tier=TrustTier.H0,
        provider_version="test",
        schema_version="test-v1",
        earliest_timestamp=START,
        latest_timestamp=CUTOFF,
        utc_calendar_days=("2026-08-01",),
        market_session_days=("2026-08-01",),
        assets=("BTC",),
        eligible_events=1,
        eligible_observations=1,
        availability_semantics="strict_as_of",
        verification_state="VERIFIED_TEST",
        provenance="test fixture",
        content_identity="h0-content",
        capability_days={"PATH_TERMINAL_DAYS": ("2026-08-01",)},
    )
    universe = ResearchUniverseBuilder(
        ResearchFreshnessPolicy(
            FeatureFreshnessPolicy(timedelta(minutes=1)),
            TrainingRecencyPolicy.expanding(),
            ForwardOosFreshnessPolicy(START),
        ),
        SessionSemantics(),
        (source,),
        (
            ResearchObservation(
                "h0-test",
                ResearchSourceType.OWN_RECORDER,
                TrustTier.H0,
                "event-1",
                "observation-1",
                "equivalent-1",
                "market-1",
                "BTC",
                START,
                START,
                "2026-08-01",
                "2026-08-01",
                "content-1",
                "value-1",
                "HEALTHY",
            ),
        ),
        FrozenHoldoutMetadata.unrevealed("dataset-v2", excluded_event_ids=("holdout-event",)),
    ).build(cutoff_timestamp=CUTOFF, code_git_sha="test-code-sha")
    canonical = build_canonical_evidence_snapshot(
        experiment_id="fixture-evidence",
        experiment_cutoff=CUTOFF,
        records=(
            EvidenceRecord(
                source_id="h0-test",
                provenance_tier="H0_LIVE_NATIVE",
                coverage_scope=CoverageScope.FULL_SOURCE,
                earliest_timestamp=START,
                latest_timestamp=CUTOFF,
                independent_utc_days=1,
                independent_events=1,
                assets=("BTC",),
                per_day_counts={"2026-08-01": 1},
                per_asset_counts={"BTC": 1},
                row_count=1,
                artifact_id="h0-artifact",
                cutoff=CUTOFF,
                sampling_policy="deterministic fixture",
                capped=False,
                cap_size=None,
                full_source=True,
                data_quality_status="HEALTHY",
                gap_quarantine_state="NONE",
                sequence_availability={"available": True, "days": 1},
                microstructure_availability={
                    "snapshot": {"available": False, "days": 0},
                    "delta": {"available": False, "days": 0},
                },
                target_availability={"available": True, "days": 1},
            ),
        ),
    )
    return ResearchRunInput(universe, canonical, model_family="path_expert")


def runner(tmp_path, *, config: ResearchRunConfig | None = None) -> ResearchRunner:
    return ResearchRunner(
        output_root=tmp_path / "research-output",
        protected_roots=(tmp_path / "production-data", tmp_path / "archive", tmp_path / "source"),
        code_identity="test-code-sha",
        config=config or ResearchRunConfig(max_work_units=20, checkpoint_interval_units=2),
    )


def test_fresh_experiment_creates_only_attributed_isolated_output(tmp_path) -> None:
    result = runner(tmp_path).run("experiment-a", approved_input(), HashWorkAdapter(4), seed=7)

    assert result.status == "COMPLETE"
    assert result.completed_units == ("unit-000", "unit-001", "unit-002", "unit-003")
    assert result.experiment_dir == tmp_path / "research-output" / "experiments" / "experiment-a"
    assert (result.experiment_dir / "metrics").is_dir()
    assert (result.experiment_dir / "logs").is_dir()
    manifest = json.loads((result.experiment_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["input"]["research_universe_id"].startswith("research-universe-")
    assert manifest["holdout_accessed"] is False
    assert not list((tmp_path / "production-data").glob("**/*"))


def test_interrupted_resume_is_deterministic_and_never_duplicates_work(tmp_path) -> None:
    input_contract = approved_input()
    config = ResearchRunConfig(max_work_units=2, checkpoint_interval_units=1)
    partial = runner(tmp_path, config=config).run(
        "resume-a", input_contract, HashWorkAdapter(5), seed=11
    )
    resumed_once = runner(tmp_path, config=config).run(
        "resume-a", input_contract, HashWorkAdapter(5), seed=11, resume=True
    )
    resumed = runner(tmp_path, config=config).run(
        "resume-a", input_contract, HashWorkAdapter(5), seed=11, resume=True
    )
    uninterrupted = runner(tmp_path / "clean").run(
        "resume-a", input_contract, HashWorkAdapter(5), seed=11
    )

    assert partial.status == "PAUSED"
    assert resumed_once.status == "PAUSED"
    assert resumed.status == "COMPLETE"
    assert resumed.completed_units == uninterrupted.completed_units
    assert resumed.result_digest == uninterrupted.result_digest


def test_resume_rejects_conflicting_input_or_config(tmp_path) -> None:
    input_contract = approved_input()
    initial = runner(
        tmp_path,
        config=ResearchRunConfig(max_work_units=1, checkpoint_interval_units=1),
    )
    initial.run("conflict-a", input_contract, HashWorkAdapter(3), seed=5)

    with pytest.raises(ResumeMismatchError, match="configuration"):
        runner(
            tmp_path, config=ResearchRunConfig(max_work_units=2, checkpoint_interval_units=1)
        ).run("conflict-a", input_contract, HashWorkAdapter(3), seed=5, resume=True)
    changed = replace(
        input_contract,
        canonical_evidence=build_canonical_evidence_snapshot(
            experiment_id="other-evidence",
            experiment_cutoff=CUTOFF,
            records=input_contract.canonical_evidence.records,
        ),
    )
    with pytest.raises(ResumeMismatchError, match="canonical"):
        initial.run("conflict-a", changed, HashWorkAdapter(3), seed=5, resume=True)


def test_corrupt_checkpoint_and_conflicting_experiment_id_fail_closed(tmp_path) -> None:
    input_contract = approved_input()
    partial_runner = runner(
        tmp_path,
        config=ResearchRunConfig(max_work_units=1, checkpoint_interval_units=1),
    )
    partial = partial_runner.run("corrupt-a", input_contract, HashWorkAdapter(3), seed=5)
    checkpoint = partial.experiment_dir / "checkpoints" / "current.json"
    checkpoint.write_text("{not-json", encoding="utf-8")
    with pytest.raises(CheckpointCorruptError):
        partial_runner.run("corrupt-a", input_contract, HashWorkAdapter(3), seed=5, resume=True)

    complete = runner(tmp_path).run("identity-a", input_contract, HashWorkAdapter(2), seed=1)
    assert complete.status == "COMPLETE"
    with pytest.raises(ExperimentConflictError, match="already complete"):
        runner(tmp_path).run("identity-a", input_contract, HashWorkAdapter(2), seed=1)


def test_semantically_invalid_checkpoint_and_budget_exhaustion_fail_closed(
    tmp_path, monkeypatch
) -> None:
    input_contract = approved_input()
    subject = runner(
        tmp_path, config=ResearchRunConfig(max_work_units=1, checkpoint_interval_units=1)
    )
    partial = subject.run("invalid-checkpoint", input_contract, HashWorkAdapter(2), seed=3)
    checkpoint_path = partial.experiment_dir / "checkpoints" / "current.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["payload"]["results"] = []
    checkpoint["checksum"] = runner_module._hash(checkpoint["payload"])
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    with pytest.raises(CheckpointCorruptError, match="results"):
        subject.run("invalid-checkpoint", input_contract, HashWorkAdapter(2), seed=3, resume=True)

    monkeypatch.setattr(runner_module, "_current_python_memory_bytes", lambda: 2)
    memory_limited = runner(
        tmp_path / "memory", config=ResearchRunConfig(max_work_units=1, max_memory_bytes=1)
    )
    with pytest.raises(ResearchRunnerError, match="memory"):
        memory_limited.run("memory", input_contract, HashWorkAdapter(1), seed=3)

    output_limited = runner(
        tmp_path / "output", config=ResearchRunConfig(max_work_units=1, max_output_bytes=1)
    )
    with pytest.raises(ResearchRunnerError, match="output"):
        output_limited.run("output", input_contract, HashWorkAdapter(1), seed=3)


def test_lock_rejects_live_writer_and_recovers_stale_owner(tmp_path) -> None:
    subject = runner(tmp_path)
    paths = subject.paths_for("lock-a")
    paths.experiment_dir.mkdir(parents=True)
    paths.lock_path.write_text(json.dumps({"pid": __import__("os").getpid()}), encoding="utf-8")
    with pytest.raises(ExperimentLockError, match="active"):
        subject.run("lock-a", approved_input(), HashWorkAdapter(1), seed=1)
    paths.lock_path.write_text(json.dumps({"pid": 999_999_999}), encoding="utf-8")
    assert subject.run("lock-a", approved_input(), HashWorkAdapter(1), seed=1).status == "COMPLETE"


def test_windows_stale_pid_probe_never_calls_os_kill(tmp_path, monkeypatch) -> None:
    def unexpected_kill(*_args: object) -> None:
        raise AssertionError("Windows liveness probe must not signal a process")

    monkeypatch.setattr(runner_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(runner_module, "_windows_process_identity", lambda _pid: None)
    monkeypatch.setattr(runner_module.os, "kill", unexpected_kill)

    assert runner_module._ExperimentLease._is_live(999_999_999) is False


def test_own_live_pid_is_recognized() -> None:
    assert runner_module._ExperimentLease._is_live(__import__("os").getpid()) is True


def test_lease_release_never_deletes_a_replaced_owner_lock(tmp_path) -> None:
    paths = runner(tmp_path).paths_for("token-lock")
    lease = runner_module._ExperimentLease(paths.lock_path, {"experiment_id": "token-lock"})
    lease.acquire()
    paths.lock_path.write_text(
        json.dumps({"pid": 999_999_999, "token": "new-owner"}), encoding="utf-8"
    )
    lease.release()
    assert paths.lock_path.exists()


def test_protected_output_and_holdout_access_are_rejected(tmp_path) -> None:
    with pytest.raises(ResearchRunnerPathError, match="protected"):
        ResearchRunner(
            output_root=tmp_path / "production-data" / "research",
            protected_roots=(tmp_path / "production-data",),
            code_identity="test-code-sha",
            config=ResearchRunConfig(max_work_units=1),
        )
    with pytest.raises(ExperimentConflictError, match="holdout"):
        replace(approved_input(), holdout_accessed=True)


def test_typed_snapshot_round_trip_never_uses_dataset_inputs(tmp_path) -> None:
    snapshot = tmp_path / "approved-research-input.json"
    original = approved_input()
    write_research_input_snapshot(snapshot, original)

    loaded = load_research_input_snapshot(snapshot)

    assert loaded.identity == original.identity
    assert loaded.research_universe.holdout_accessed is False
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert "payload" not in payload["payload"]["research_universe"]["frozen_holdout_metadata"]
    payload["payload"]["dataset_path"] = "data/features.sqlite3"
    payload["checksum"] = sha256(
        json.dumps(payload["payload"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ExperimentConflictError, match="Dataset"):
        load_research_input_snapshot(snapshot)


def test_typed_snapshot_rejects_tampered_evidence_and_frozen_payload(tmp_path) -> None:
    snapshot = tmp_path / "approved-research-input.json"
    write_research_input_snapshot(snapshot, approved_input())
    envelope = json.loads(snapshot.read_text(encoding="utf-8"))
    envelope["payload"]["canonical_evidence"]["records"][0]["row_count"] = 2
    envelope["checksum"] = sha256(
        json.dumps(envelope["payload"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    snapshot.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ResearchRunnerError, match="identity"):
        load_research_input_snapshot(snapshot)

    write_research_input_snapshot(snapshot, approved_input())
    envelope = json.loads(snapshot.read_text(encoding="utf-8"))
    envelope["payload"]["canonical_evidence"]["frozen_datasets"] = [
        {"dataset_id": "frozen", "payload": "forbidden"}
    ]
    envelope["checksum"] = sha256(
        json.dumps(envelope["payload"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    snapshot.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ExperimentConflictError, match="forbidden"):
        load_research_input_snapshot(snapshot)


def test_cli_runs_only_from_typed_snapshot_into_explicit_isolated_root(
    tmp_path, capsys, monkeypatch
) -> None:
    snapshot = tmp_path / "approved-research-input.json"
    output = tmp_path / "isolated-output"
    write_research_input_snapshot(snapshot, approved_input())
    monkeypatch.setattr(
        "live15_quant.cli.__file__", str(tmp_path / "project" / "src" / "live15_quant" / "cli.py")
    )

    research_preflight_main(
        [
            "--snapshot",
            str(snapshot),
            "--experiment",
            "cli-smoke",
            "--output",
            str(output),
            "--checkpoint",
            str(output / "experiments" / "cli-smoke" / "checkpoints" / "current.json"),
            "--code-revision",
            "test-code-sha",
            "--smoke-units",
            "1",
        ]
    )

    assert json.loads(capsys.readouterr().out)["status"] == "COMPLETE"


def test_cli_rejects_repository_root_output(tmp_path, monkeypatch) -> None:
    snapshot = tmp_path / "approved-research-input.json"
    write_research_input_snapshot(snapshot, approved_input())
    project_root = tmp_path / "project"
    monkeypatch.setattr(
        "live15_quant.cli.__file__", str(project_root / "src" / "live15_quant" / "cli.py")
    )
    with pytest.raises(ResearchRunnerPathError, match="protected"):
        research_preflight_main(
            [
                "--snapshot",
                str(snapshot),
                "--experiment",
                "repo-output",
                "--output",
                str(project_root),
                "--code-revision",
                "test-code-sha",
            ]
        )
