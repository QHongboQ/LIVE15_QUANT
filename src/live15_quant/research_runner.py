"""Isolated, checkpointed infrastructure for future universe-backed research.

This module deliberately has no Recorder, Paper, execution, service-control, or Dataset loader
dependency.  Its small interface is a vetted research input plus a deterministic work adapter.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import random
import tempfile
import time
import tracemalloc
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from live15_quant.canonical_evidence import (
    CanonicalEvidenceSnapshot,
    CoverageScope,
    EvidenceRecord,
    PreflightStatus,
    build_canonical_evidence_snapshot,
    training_preflight,
)
from live15_quant.research_data_authority import (
    FeatureFreshnessPolicy,
    ForwardOosFreshnessPolicy,
    FrozenHoldoutMetadata,
    ResearchFreshnessPolicy,
    ResearchSourceManifest,
    ResearchSourceType,
    ResearchUniverseSnapshot,
    TrainingRecencyMode,
    TrainingRecencyPolicy,
    TrustTier,
)

CHECKPOINT_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
RESEARCH_INPUT_SNAPSHOT_SCHEMA_VERSION = 1
_IS_WINDOWS = os.name == "nt"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def _windows_process_identity(pid: int) -> str | None:
    """Return a live Windows process creation identity without delivering a signal."""
    if pid <= 0:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD)
    open_process.restype = ctypes.wintypes.HANDLE
    handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        if ctypes.get_last_error() == _ERROR_ACCESS_DENIED:
            return "ACCESS_DENIED"
        return None
    try:
        exit_code = ctypes.wintypes.DWORD()
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (ctypes.wintypes.HANDLE, ctypes.c_void_p)
        get_exit_code.restype = ctypes.wintypes.BOOL
        if not get_exit_code(handle, ctypes.byref(exit_code)) or exit_code.value != _STILL_ACTIVE:
            return None
        creation = ctypes.wintypes.FILETIME()
        exit_time = ctypes.wintypes.FILETIME()
        kernel_time = ctypes.wintypes.FILETIME()
        user_time = ctypes.wintypes.FILETIME()
        get_times = kernel32.GetProcessTimes
        get_times.argtypes = (
            ctypes.wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        get_times.restype = ctypes.wintypes.BOOL
        if not get_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return "ACCESS_DENIED"
        return str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


class ResearchRunnerError(RuntimeError):
    """Base error for the isolated research-runner seam."""


class ResearchRunnerPathError(ResearchRunnerError):
    """Raised when a runner would write in a protected production location."""


class ExperimentLockError(ResearchRunnerError):
    """Raised when another live local process owns an experiment."""


class ExperimentConflictError(ResearchRunnerError):
    """Raised when an immutable experiment identity conflicts with existing state."""


class ResumeMismatchError(ResearchRunnerError):
    """Raised when a checkpoint is not compatible with the requested resume."""


class CheckpointCorruptError(ResearchRunnerError):
    """Raised when a checkpoint cannot prove its own integrity."""


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _current_python_memory_bytes() -> int:
    """Return traced Python heap bytes; native process allocations are outside this MVP seam."""
    return tracemalloc.get_traced_memory()[0]


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(_canonical(payload), sort_keys=True, indent=2, default=str) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _decode_random_state(value: object) -> object:
    if isinstance(value, list):
        return tuple(_decode_random_state(item) for item in value)
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("snapshot timestamp must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("snapshot timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"snapshot {name} must be an object")
    return value


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"snapshot {name} must be a list of strings")
    return tuple(value)


def _source_from_json(value: object) -> ResearchSourceManifest:
    item = _object(value, "source")
    capability_days = _object(item.get("capability_days", {}), "source capability_days")
    return ResearchSourceManifest(
        source_id=str(item["source_id"]),
        source_type=ResearchSourceType(str(item["source_type"])),
        trust_tier=TrustTier(str(item["trust_tier"])),
        provider_version=item.get("provider_version")
        if isinstance(item.get("provider_version"), str)
        else None,
        schema_version=str(item["schema_version"]),
        earliest_timestamp=_timestamp(item["earliest_timestamp"])
        if item.get("earliest_timestamp")
        else None,
        latest_timestamp=_timestamp(item["latest_timestamp"])
        if item.get("latest_timestamp")
        else None,
        utc_calendar_days=_strings(item["utc_calendar_days"], "source utc days"),
        market_session_days=_strings(item["market_session_days"], "source session days"),
        assets=_strings(item["assets"], "source assets"),
        eligible_events=int(item["eligible_events"]),
        eligible_observations=int(item["eligible_observations"]),
        availability_semantics=str(item["availability_semantics"]),
        verification_state=str(item["verification_state"]),
        provenance=str(item["provenance"]),
        content_identity=str(item["content_identity"]),
        limitations=_strings(item.get("limitations", []), "source limitations"),
        capability_days={
            key: _strings(days, f"capability {key}") for key, days in capability_days.items()
        },
    )


def _universe_from_json(value: object) -> ResearchUniverseSnapshot:
    item = _object(value, "research_universe")
    freshness = _object(item["freshness_policy"], "freshness policy")
    feature = _object(freshness["feature_freshness"], "feature freshness")
    recency = _object(freshness["training_recency"], "training recency")
    forward = _object(freshness["forward_oos_freshness"], "forward OOS freshness")
    holdout = _object(item["frozen_holdout"], "frozen holdout")
    internal = _object(item["frozen_holdout_metadata"], "frozen holdout metadata")
    if holdout.get("payload_accessed") is not False or "payload" in internal:
        raise ExperimentConflictError("snapshot must not contain or access frozen holdout payload")
    sources = item.get("sources")
    if not isinstance(sources, list):
        raise ValueError("snapshot sources must be a list")
    capability_days = _object(item.get("capability_days", {}), "universe capability_days")
    ranges = internal.get("excluded_time_ranges", [])
    if not isinstance(ranges, list):
        raise ValueError("excluded_time_ranges must be a list")
    return ResearchUniverseSnapshot(
        universe_id=str(item["universe_id"]),
        content_hash=str(item["content_hash"]),
        cutoff_timestamp=_timestamp(item["cutoff_timestamp"]),
        code_git_sha=str(item["code_git_sha"]),
        freshness_policy=ResearchFreshnessPolicy(
            FeatureFreshnessPolicy(
                timedelta(seconds=float(feature["max_observation_age_seconds"]))
            ),
            TrainingRecencyPolicy(
                TrainingRecencyMode(str(recency["mode"])),
                recency.get("rolling_session_count"),
                recency.get("age_weight_half_life_days"),
            ),
            ForwardOosFreshnessPolicy(_timestamp(forward["specification_frozen_at"])),
            version=str(freshness["version"]),
        ),
        session_semantics_version=str(item["session_semantics_version"]),
        source_manifests=tuple(_source_from_json(source) for source in sources),
        earliest_timestamp=_timestamp(item["earliest_timestamp"])
        if item.get("earliest_timestamp")
        else None,
        latest_timestamp=_timestamp(item["latest_timestamp"])
        if item.get("latest_timestamp")
        else None,
        utc_calendar_days=_strings(item["utc_calendar_days"], "utc days"),
        market_session_days=_strings(item["market_session_days"], "market session days"),
        eligible_development_days=_strings(item["eligible_development_days"], "development days"),
        validation_days=_strings(item["validation_days"], "validation days"),
        assets=_strings(item["assets"], "assets"),
        eligible_events=int(item["eligible_events"]),
        eligible_observations=int(item["eligible_observations"]),
        deduplicated_observations=int(item["deduplicated_observations"]),
        conflicting_observations=int(item["conflicting_observations"]),
        quarantined_observations=int(item["quarantined_observations"]),
        holdout_excluded_observations=int(item["holdout_excluded_observations"]),
        selected_source_ids=_strings(item["selected_source_ids"], "selected source IDs"),
        frozen_holdout=FrozenHoldoutMetadata(
            dataset_id=str(internal["dataset_id"]),
            status=str(internal["status"]),
            excluded_event_ids=_strings(
                internal.get("excluded_event_ids", []), "excluded event IDs"
            ),
            excluded_time_ranges=tuple(
                (_timestamp(pair[0]), _timestamp(pair[1])) for pair in ranges
            ),
            validation_days=_strings(
                internal.get("validation_days", []), "holdout validation days"
            ),
        ),
        depthfeed_status=str(item["depthfeed_status"]),
        capability_days={
            key: _strings(days, f"universe capability {key}")
            for key, days in capability_days.items()
        },
    )


def _evidence_from_json(value: object) -> CanonicalEvidenceSnapshot:
    item = _object(value, "canonical_evidence")
    raw_records = item.get("records")
    if not isinstance(raw_records, list):
        raise ValueError("canonical evidence records must be a list")
    records: list[EvidenceRecord] = []
    for raw in raw_records:
        record = _object(raw, "evidence record")
        records.append(
            EvidenceRecord(
                source_id=str(record["source_id"]),
                provenance_tier=str(record["provenance_tier"]),
                coverage_scope=CoverageScope(str(record["coverage_scope"])),
                earliest_timestamp=_timestamp(record["earliest_timestamp"]),
                latest_timestamp=_timestamp(record["latest_timestamp"]),
                independent_utc_days=int(record["independent_utc_days"]),
                independent_events=int(record["independent_events"]),
                assets=_strings(record["assets"], "evidence assets"),
                per_day_counts=_object(record["per_day_counts"], "per-day counts"),
                per_asset_counts=_object(record["per_asset_counts"], "per-asset counts"),
                row_count=int(record["row_count"]),
                artifact_id=str(record["artifact_id"]),
                cutoff=_timestamp(record["cutoff"]),
                sampling_policy=str(record["sampling_policy"]),
                capped=bool(record["capped"]),
                cap_size=record.get("cap_size"),
                full_source=bool(record["full_source"]),
                data_quality_status=str(record["data_quality_status"]),
                gap_quarantine_state=str(record["gap_quarantine_state"]),
                sequence_availability=_object(
                    record["sequence_availability"], "sequence availability"
                ),
                microstructure_availability=_object(
                    record["microstructure_availability"], "microstructure availability"
                ),
                target_availability=_object(record["target_availability"], "target availability"),
                source_independent_utc_days=record.get("source_independent_utc_days"),
                source_independent_events=record.get("source_independent_events"),
                source_assets=_strings(record.get("source_assets", []), "source assets"),
                coverage_days=_strings(record.get("coverage_days", []), "coverage days"),
                holdout_accessed=bool(record.get("holdout_accessed", False)),
            )
        )
    frozen = item.get("frozen_datasets", [])
    if not isinstance(frozen, list):
        raise ValueError("frozen_datasets must be a list")
    return CanonicalEvidenceSnapshot(
        schema_version=str(item["schema_version"]),
        snapshot_id=str(item["snapshot_id"]),
        experiment_id=str(item["experiment_id"]),
        experiment_cutoff=_timestamp(item["experiment_cutoff"]),
        records=tuple(records),
        frozen_datasets=tuple(_object(entry, "frozen dataset") for entry in frozen),
        inconsistency_states=_strings(item.get("inconsistency_states", []), "inconsistency states"),
        generated_at=_timestamp(item["generated_at"]),
    )


def write_research_input_snapshot(path: Path, run_input: ResearchRunInput) -> None:
    """Persist a typed, payload-free input envelope for a reproducible research invocation."""
    holdout = run_input.research_universe.frozen_holdout
    universe = run_input.research_universe.to_public_dict() | {
        "frozen_holdout_metadata": {
            "dataset_id": holdout.dataset_id,
            "status": holdout.status,
            "excluded_event_ids": list(holdout.excluded_event_ids),
            "excluded_time_ranges": [
                [start.isoformat(), end.isoformat()] for start, end in holdout.excluded_time_ranges
            ],
            "validation_days": list(holdout.validation_days),
        }
    }
    payload = {
        "schema_version": RESEARCH_INPUT_SNAPSHOT_SCHEMA_VERSION,
        "model_family": run_input.model_family,
        "research_universe": universe,
        "canonical_evidence": run_input.canonical_evidence.to_dict(),
    }
    _atomic_json(path, {"payload": payload, "checksum": _hash(payload)})


def load_research_input_snapshot(path: Path) -> ResearchRunInput:
    """Load only typed RDA/canonical evidence inputs; Dataset paths are not a supported seam."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ResearchRunnerError("research input snapshot is not valid JSON") from error
    envelope = _object(payload, "research input envelope")
    item = _object(envelope.get("payload"), "research input")
    if not isinstance(envelope.get("checksum"), str) or envelope["checksum"] != _hash(item):
        raise ResearchRunnerError("research input snapshot checksum does not match")
    if item.get("schema_version") != RESEARCH_INPUT_SNAPSHOT_SCHEMA_VERSION:
        raise ResearchRunnerError("research input snapshot schema is incompatible")
    if "dataset" in item or "dataset_path" in item:
        raise ExperimentConflictError("Dataset inputs are forbidden; use ResearchUniverseSnapshot")
    evidence = _evidence_from_json(item["canonical_evidence"])
    _reject_frozen_payloads(evidence.frozen_datasets)
    rebuilt = build_canonical_evidence_snapshot(
        experiment_id=evidence.experiment_id,
        experiment_cutoff=evidence.experiment_cutoff,
        records=evidence.records,
        frozen_datasets=evidence.frozen_datasets,
    )
    if rebuilt.snapshot_id != evidence.snapshot_id:
        raise ResearchRunnerError("canonical evidence snapshot identity does not match content")
    return ResearchRunInput(
        _universe_from_json(item["research_universe"]), evidence, str(item["model_family"])
    )


def _reject_frozen_payloads(entries: Sequence[Mapping[str, object]]) -> None:
    forbidden = {"payload", "dataset_path", "feature_store_path", "sqlite_path"}
    for entry in entries:
        if any(str(key).casefold() in forbidden for key in entry):
            raise ExperimentConflictError(
                "canonical evidence frozen dataset contains forbidden payload data"
            )


@dataclass(frozen=True, slots=True)
class ResearchRunConfig:
    """Bounded resource policy; the first implementation is intentionally single-process."""

    max_work_units: int = 100
    checkpoint_interval_units: int = 10
    max_output_bytes: int = 64 * 1024 * 1024
    max_wall_seconds: float = 60.0
    worker_count: int = 1
    max_memory_bytes: int = 2 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_work_units <= 0 or self.checkpoint_interval_units <= 0:
            raise ValueError("work and checkpoint limits must be positive")
        if self.max_output_bytes <= 0 or self.max_wall_seconds <= 0 or self.max_memory_bytes <= 0:
            raise ValueError("resource limits must be positive")
        if self.worker_count != 1:
            raise ValueError("MVN-003 is intentionally single-process")

    @property
    def config_hash(self) -> str:
        return _hash(asdict(self))


@dataclass(frozen=True, slots=True)
class ResearchRunInput:
    """The sole data seam accepted by the runner; Dataset paths are deliberately absent."""

    research_universe: ResearchUniverseSnapshot
    canonical_evidence: CanonicalEvidenceSnapshot
    model_family: str
    holdout_accessed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.research_universe, ResearchUniverseSnapshot):
            raise TypeError("research_universe must be a ResearchUniverseSnapshot")
        if not isinstance(self.canonical_evidence, CanonicalEvidenceSnapshot):
            raise TypeError("canonical_evidence must be a CanonicalEvidenceSnapshot")
        if self.holdout_accessed or self.research_universe.holdout_accessed:
            raise ExperimentConflictError("holdout access is forbidden")
        result = training_preflight(self.canonical_evidence, model_family=self.model_family)
        if result.status is PreflightStatus.BLOCKED:
            raise ExperimentConflictError(
                f"training preflight blocked: {', '.join(result.reasons)}"
            )

    @property
    def identity(self) -> dict[str, str]:
        return {
            "research_universe_id": self.research_universe.universe_id,
            "research_universe_hash": self.research_universe.content_hash,
            "canonical_evidence_id": self.canonical_evidence.snapshot_id,
            "model_family": self.model_family,
        }


class ResearchWorkAdapter(Protocol):
    """Adapter seam for factor/model/walk-forward work; no storage or runtime authority leaks in."""

    @property
    def specification_hash(self) -> str: ...

    def work_units(self, run_input: ResearchRunInput) -> Sequence[str]: ...

    def execute(self, unit: str, *, seed: int, rng: random.Random) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class HashWorkAdapter:
    """Deterministic, side-effect-free adapter used only for infrastructure smoke coverage."""

    count: int

    @property
    def specification_hash(self) -> str:
        return _hash({"adapter": "hash-work-v1", "count": self.count})

    def work_units(self, run_input: ResearchRunInput) -> Sequence[str]:
        del run_input
        if self.count <= 0:
            raise ValueError("HashWorkAdapter count must be positive")
        return tuple(f"unit-{index:03d}" for index in range(self.count))

    def execute(self, unit: str, *, seed: int, rng: random.Random) -> Mapping[str, object]:
        return {"unit": unit, "value": _hash({"unit": unit, "seed": seed, "nonce": rng.random()})}


@dataclass(frozen=True, slots=True)
class ExperimentPaths:
    output_root: Path
    experiment_dir: Path
    manifest_path: Path
    checkpoint_path: Path
    lock_path: Path
    artifact_path: Path
    metrics_dir: Path
    logs_dir: Path


@dataclass(frozen=True, slots=True)
class ResearchRunResult:
    status: str
    experiment_dir: Path
    completed_units: tuple[str, ...]
    result_digest: str | None
    checkpoint_sequence: int


class _ExperimentLease:
    def __init__(self, path: Path, identity: Mapping[str, str]) -> None:
        self.path = path
        self.identity = dict(identity)
        self.token = uuid.uuid4().hex

    @staticmethod
    def _is_live(pid: int, expected_process_identity: str | None = None) -> bool:
        if pid <= 0:
            return False
        if _IS_WINDOWS:
            observed = _windows_process_identity(pid)
            if observed is None:
                return False
            if expected_process_identity and observed != expected_process_identity:
                return False
            return True
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "identity": self.identity,
            "token": self.token,
            "process_identity": _windows_process_identity(os.getpid()) if _IS_WINDOWS else None,
            "created_monotonic": time.monotonic(),
        }
        try:
            with self.path.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            return
        except FileExistsError:
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
                pid = int(existing.get("pid", 0))
                stale_token = existing.get("token")
                process_identity = existing.get("process_identity")
            except (OSError, ValueError, json.JSONDecodeError):
                pid = 0
                stale_token = None
                process_identity = None
            if self._is_live(pid, process_identity if isinstance(process_identity, str) else None):
                raise ExperimentLockError("experiment has an active local writer") from None
            try:
                current = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = {}
            if current.get("token") != stale_token:
                self.acquire()
                return
            self.path.unlink(missing_ok=True)
            self.acquire()

    def release(self) -> None:
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if current.get("token") == self.token:
            self.path.unlink(missing_ok=True)


class ResearchRunner:
    """Deep module hiding output isolation, leases, checkpoints, resume, and bounded execution."""

    def __init__(
        self,
        *,
        output_root: Path,
        protected_roots: Iterable[Path],
        code_identity: str,
        config: ResearchRunConfig,
    ) -> None:
        self.output_root = output_root.resolve()
        self.protected_roots = tuple(root.resolve() for root in protected_roots)
        self.code_identity = code_identity
        self.config = config
        if not code_identity:
            raise ValueError("code_identity is required")
        if any(self._is_within(self.output_root, protected) for protected in self.protected_roots):
            raise ResearchRunnerPathError(
                "research output root is inside a protected production path"
            )

    @staticmethod
    def _is_within(candidate: Path, root: Path) -> bool:
        try:
            candidate.relative_to(root)
        except ValueError:
            return False
        return True

    def paths_for(self, experiment_id: str) -> ExperimentPaths:
        if not experiment_id or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in experiment_id
        ):
            raise ValueError(
                "experiment_id must contain only letters, digits, hyphen, or underscore"
            )
        experiment = self.output_root / "experiments" / experiment_id
        return ExperimentPaths(
            self.output_root,
            experiment,
            experiment / "manifest.json",
            experiment / "checkpoints" / "current.json",
            experiment / ".writer.lock",
            experiment / "artifacts" / "result.json",
            experiment / "metrics",
            experiment / "logs",
        )

    def _identity(
        self,
        experiment_id: str,
        run_input: ResearchRunInput,
        adapter: ResearchWorkAdapter,
        seed: int,
    ) -> dict[str, str]:
        identity = {
            "experiment_id": experiment_id,
            **run_input.identity,
            "code_identity": self.code_identity,
            "config_hash": self.config.config_hash,
            "specification_hash": adapter.specification_hash,
            "seed": str(seed),
        }
        return {**identity, "experiment_hash": _hash(identity)}

    @staticmethod
    def _read_checkpoint(path: Path) -> dict[str, object]:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            payload = envelope["payload"]
            checksum = envelope["checksum"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise CheckpointCorruptError("checkpoint is malformed") from error
        if (
            not isinstance(payload, dict)
            or not isinstance(checksum, str)
            or _hash(payload) != checksum
        ):
            raise CheckpointCorruptError("checkpoint checksum does not match")
        if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise ResumeMismatchError("checkpoint schema is incompatible")
        return payload

    @staticmethod
    def _write_checkpoint(path: Path, payload: Mapping[str, object]) -> None:
        envelope = {"payload": dict(payload), "checksum": _hash(payload)}
        _atomic_json(path, envelope)

    def _validate_existing_manifest(
        self, paths: ExperimentPaths, identity: Mapping[str, str]
    ) -> None:
        if not paths.manifest_path.exists():
            return
        try:
            existing = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
            previous = existing["identity"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ExperimentConflictError("existing experiment manifest is malformed") from error
        if previous != dict(identity):
            raise ExperimentConflictError(
                "experiment ID is already bound to conflicting immutable inputs"
            )

    @staticmethod
    def _directory_size(path: Path) -> int:
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())

    def run(
        self,
        experiment_id: str,
        run_input: ResearchRunInput,
        adapter: ResearchWorkAdapter,
        *,
        seed: int,
        resume: bool = False,
    ) -> ResearchRunResult:
        paths = self.paths_for(experiment_id)
        identity = self._identity(experiment_id, run_input, adapter, seed)
        lease = _ExperimentLease(paths.lock_path, identity)
        lease.acquire()
        started_tracing = not tracemalloc.is_tracing()
        if started_tracing:
            tracemalloc.start()
        try:
            if not resume:
                self._validate_existing_manifest(paths, identity)
                if paths.artifact_path.exists():
                    raise ExperimentConflictError(
                        "experiment is already complete; use a new experiment ID"
                    )
            units = tuple(adapter.work_units(run_input))
            if len(set(units)) != len(units):
                raise ExperimentConflictError("adapter returned duplicate work unit identities")
            completed: list[str] = []
            results: list[Mapping[str, object]] = []
            sequence = 0
            rng = random.Random(seed)
            if resume:
                if not paths.checkpoint_path.exists():
                    raise ResumeMismatchError("resume requested but no checkpoint exists")
                checkpoint = self._read_checkpoint(paths.checkpoint_path)
                if checkpoint.get("identity") != identity:
                    mismatches = {
                        key
                        for key, value in identity.items()
                        if checkpoint.get("identity", {}).get(key) != value
                    }
                    if "canonical_evidence_id" in mismatches:
                        raise ResumeMismatchError(
                            "canonical evidence identity does not match checkpoint"
                        )
                    if "config_hash" in mismatches:
                        raise ResumeMismatchError("configuration does not match checkpoint")
                    raise ResumeMismatchError("checkpoint immutable identity does not match")
                completed = list(checkpoint.get("completed_units", ()))
                results = list(checkpoint.get("results", ()))
                if len(completed) != len(set(completed)) or completed != list(
                    units[: len(completed)]
                ):
                    raise CheckpointCorruptError(
                        "checkpoint completed work is not a valid ordered prefix"
                    )
                if len(results) != len(completed) or not all(
                    isinstance(result, Mapping) for result in results
                ):
                    raise CheckpointCorruptError("checkpoint results do not match completed work")
                sequence = int(checkpoint.get("sequence", 0))
                try:
                    rng.setstate(_decode_random_state(checkpoint["rng_state"]))
                except (KeyError, TypeError, ValueError) as error:
                    raise CheckpointCorruptError("checkpoint random state is invalid") from error
                self._validate_existing_manifest(paths, identity)
            elif paths.checkpoint_path.exists():
                raise ExperimentConflictError(
                    "checkpoint exists; resume is required for this experiment"
                )
            for directory in (
                paths.experiment_dir,
                paths.checkpoint_path.parent,
                paths.artifact_path.parent,
                paths.metrics_dir,
                paths.logs_dir,
            ):
                directory.mkdir(parents=True, exist_ok=True)
            _atomic_json(
                paths.manifest_path,
                {
                    "schema_version": MANIFEST_SCHEMA_VERSION,
                    "identity": identity,
                    "input": run_input.identity,
                    "holdout_accessed": False,
                    "resource_config": asdict(self.config),
                },
            )
            started = time.monotonic()
            remaining_budget = self.config.max_work_units
            for unit in units[len(completed) :]:
                if (
                    remaining_budget <= 0
                    or time.monotonic() - started >= self.config.max_wall_seconds
                ):
                    break
                results.append(dict(adapter.execute(unit, seed=seed, rng=rng)))
                completed.append(unit)
                if _current_python_memory_bytes() > self.config.max_memory_bytes:
                    raise ResearchRunnerError(
                        "research Python heap exceeds configured memory budget"
                    )
                remaining_budget -= 1
                if len(completed) % self.config.checkpoint_interval_units == 0:
                    sequence += 1
                    self._write_checkpoint(
                        paths.checkpoint_path,
                        {
                            "schema_version": CHECKPOINT_SCHEMA_VERSION,
                            "sequence": sequence,
                            "stage": "RUNNING",
                            "identity": identity,
                            "input": run_input.identity,
                            "resource_config": asdict(self.config),
                            "completed_units": completed,
                            "results": results,
                            "rng_state": rng.getstate(),
                        },
                    )
                if self._directory_size(paths.experiment_dir) > self.config.max_output_bytes:
                    raise ResearchRunnerError("research output exceeds configured byte budget")
            sequence += 1
            completed_all = len(completed) == len(units)
            checkpoint_payload = {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "sequence": sequence,
                "stage": "COMPLETE" if completed_all else "PAUSED",
                "identity": identity,
                "input": run_input.identity,
                "resource_config": asdict(self.config),
                "completed_units": completed,
                "results": results,
                "rng_state": rng.getstate(),
            }
            self._write_checkpoint(paths.checkpoint_path, checkpoint_payload)
            digest = _hash(results) if completed_all else None
            if completed_all:
                _atomic_json(
                    paths.artifact_path,
                    {"identity": identity, "completed_units": completed, "result_digest": digest},
                )
            return ResearchRunResult(
                "COMPLETE" if completed_all else "PAUSED",
                paths.experiment_dir,
                tuple(completed),
                digest,
                sequence,
            )
        finally:
            lease.release()
            if started_tracing:
                tracemalloc.stop()


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "RESEARCH_INPUT_SNAPSHOT_SCHEMA_VERSION",
    "CheckpointCorruptError",
    "ExperimentConflictError",
    "ExperimentLockError",
    "HashWorkAdapter",
    "ResearchRunConfig",
    "ResearchRunInput",
    "ResearchRunResult",
    "ResearchRunner",
    "ResearchRunnerError",
    "ResearchRunnerPathError",
    "ResearchWorkAdapter",
    "ResumeMismatchError",
    "load_research_input_snapshot",
    "write_research_input_snapshot",
]
