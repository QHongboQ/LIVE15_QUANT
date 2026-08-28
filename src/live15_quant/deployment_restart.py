"""Fail-closed, auditable Windows service restart gates for DEP-001.

This module is the *only* deployment authority allowed to stop or start a
LIVE15 WinSW service.  It deliberately treats successful SCM commands as
requests, not evidence: every transition is observed and recorded before the
next stage may proceed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from live15_quant.release_pipeline import ReleaseError, verify_runtime_provenance


class RestartGateError(RuntimeError):
    """Raised when a requested service restart cannot be proven."""


@dataclass(frozen=True)
class ServiceSnapshot:
    """The SCM state required to prove one restart transition."""

    state: str
    pid: int
    image_path: str


@dataclass(frozen=True)
class WinSWLogCursor:
    """A byte boundary captured before the stop request."""

    path: Path
    byte_offset: int
    modified_at: datetime


@dataclass(frozen=True)
class RestartExpectation:
    """Immutable approved inputs for exactly one service restart."""

    service_name: str
    component: str
    release_root: Path
    evidence_directory: Path
    service_config_path: Path
    expected_config_sha256: str
    wrapper_log_path: Path
    expected_git_sha: str
    timeout_seconds: float
    poll_interval_seconds: float


@dataclass(frozen=True)
class RestartResult:
    """A successfully persisted and fully proven restart result."""

    status: str
    old_pid: int
    new_pid: int
    audit_path: Path


class ServiceControl(Protocol):
    def inspect(self, service_name: str) -> ServiceSnapshot: ...

    def stop(self, service_name: str) -> None: ...

    def start(self, service_name: str) -> None: ...

    def is_process_alive(self, pid: int) -> bool: ...


class WinSWLogEvidence(Protocol):
    def cursor(self, path: Path) -> WinSWLogCursor: ...

    def service_mode_start_after(
        self, path: Path, cursor: WinSWLogCursor, after: datetime
    ) -> str | None: ...


ProvenanceVerifier = Callable[..., object]


@dataclass(frozen=True)
class RestartDependencies:
    """Injected host boundaries; tests never need a real Windows service."""

    scm: ServiceControl
    winsw_logs: WinSWLogEvidence
    now: Callable[[], datetime]
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]
    verify_provenance: ProvenanceVerifier


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    """Persist a non-empty audit document or fail the gate.

    ``os.replace`` prevents a completed restart record from ever being observed
    as a zero-byte or partially serialized JSON file.
    """

    serialized = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    if not serialized.strip():  # Defensive: audit records must never be empty.
        raise RestartGateError("AUDIT_RECEIPT_WRITE_FAILURE: empty audit serialization")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(serialized, encoding="utf-8")
        if temporary.stat().st_size == 0:
            raise RestartGateError("AUDIT_RECEIPT_WRITE_FAILURE: temporary audit is empty")
        os.replace(temporary, path)
        if path.stat().st_size == 0:
            raise RestartGateError("AUDIT_RECEIPT_WRITE_FAILURE: committed audit is empty")
    except OSError as error:
        raise RestartGateError(f"AUDIT_RECEIPT_WRITE_FAILURE: {path}: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)


def _image_executable(image_path: str) -> Path:
    value = image_path.strip()
    if value.startswith('"'):
        closing_quote = value.find('"', 1)
        if closing_quote <= 1:
            raise RestartGateError("SERVICE_IMAGE_PATH_INVALID: unterminated executable quote")
        return Path(value[1:closing_quote])
    if not value:
        raise RestartGateError("SERVICE_IMAGE_PATH_INVALID: missing executable")
    return Path(value.split(maxsplit=1)[0])


def _expected_component(service_name: str) -> str:
    components = {
        "LIVE15Recorder": "recorder",
        "LIVE15ControlCenter": "control-center",
        "LIVE15RuntimeSupervisor": "runtime-supervisor",
    }
    try:
        return components[service_name]
    except KeyError as error:
        raise RestartGateError(f"UNSUPPORTED_SERVICE: {service_name}") from error


def _sidecar_config(executable: Path) -> Path:
    return executable.with_suffix(".xml")


def _wait_for(
    *,
    predicate: Callable[[], ServiceSnapshot | None],
    dependencies: RestartDependencies,
    timeout_seconds: float,
    poll_interval_seconds: float,
    error_code: str,
) -> ServiceSnapshot:
    deadline = dependencies.monotonic() + timeout_seconds
    while True:
        observed = predicate()
        if observed is not None:
            return observed
        if dependencies.monotonic() >= deadline:
            raise RestartGateError(error_code)
        dependencies.sleep(min(poll_interval_seconds, timeout_seconds))


class FileWinSWLogs:
    """Read only newly appended WinSW v2 wrapper-log evidence.

    WinSW v2.12 emits ``Starting WinSW in service mode`` when the SCM has
    launched a wrapper process.  The cursor makes a historical matching line
    insufficient, and console-mode text is intentionally never accepted.
    """

    _SERVICE_MODE = "Starting WinSW in service mode"
    _TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s")

    def cursor(self, path: Path) -> WinSWLogCursor:
        try:
            metadata = path.stat()
        except OSError as error:
            raise RestartGateError(f"WINSW_LOG_CURSOR_FAILURE: {path}: {error}") from error
        return WinSWLogCursor(
            path=path,
            byte_offset=metadata.st_size,
            modified_at=datetime.fromtimestamp(metadata.st_mtime, UTC),
        )

    def service_mode_start_after(
        self, path: Path, cursor: WinSWLogCursor, after: datetime
    ) -> str | None:
        try:
            metadata = path.stat()
            if metadata.st_size < cursor.byte_offset:
                raise RestartGateError("WINSW_LOG_CURSOR_INVALIDATED: wrapper log rotated")
            with path.open("rb") as source:
                source.seek(cursor.byte_offset)
                appended = source.read().decode("utf-8", errors="replace")
        except OSError as error:
            raise RestartGateError(f"WINSW_LOG_EVIDENCE_FAILURE: {path}: {error}") from error
        local_after = after.astimezone()
        for line in appended.splitlines():
            timestamp_match = self._TIMESTAMP.match(line)
            if self._SERVICE_MODE not in line or timestamp_match is None:
                continue
            try:
                observed_at = datetime.strptime(timestamp_match.group(1), "%Y-%m-%d %H:%M:%S,%f")
            except ValueError:
                continue
            if observed_at.replace(tzinfo=local_after.tzinfo) > local_after:
                return line
        return None


class WindowsScm:
    """Narrow, shell-free SCM boundary for an authorized future deployment."""

    _STATE = re.compile(r"STATE\s*:\s*(\d+)")
    _PID = re.compile(r"PID\s*:\s*(\d+)")
    _IMAGE = re.compile(r"BINARY_PATH_NAME\s*:\s*(.+)")

    @staticmethod
    def _run(*arguments: str) -> str:
        result = subprocess.run(["sc.exe", *arguments], capture_output=True, text=True, check=False)
        if result.returncode:
            message = (result.stderr or result.stdout).strip()
            raise RestartGateError(f"SCM_COMMAND_FAILURE: {' '.join(arguments)}: {message}")
        return result.stdout

    def inspect(self, service_name: str) -> ServiceSnapshot:
        query = self._run("queryex", service_name)
        config = self._run("qc", service_name)
        state_match = self._STATE.search(query)
        pid_match = self._PID.search(query)
        image_match = self._IMAGE.search(config)
        if state_match is None or pid_match is None or image_match is None:
            raise RestartGateError(f"SCM_OBSERVATION_INCOMPLETE: {service_name}")
        states = {1: "STOPPED", 2: "START_PENDING", 3: "STOP_PENDING", 4: "RUNNING"}
        state = states.get(int(state_match.group(1)), f"UNKNOWN_{state_match.group(1)}")
        return ServiceSnapshot(state, int(pid_match.group(1)), image_match.group(1).strip())

    def stop(self, service_name: str) -> None:
        self._run("stop", service_name)

    def start(self, service_name: str) -> None:
        self._run("start", service_name)

    def is_process_alive(self, pid: int) -> bool:
        result = subprocess.run(
            ["tasklist.exe", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise RestartGateError(f"PROCESS_OBSERVATION_FAILURE: PID {pid}")
        return any(field.strip('"') == str(pid) for field in result.stdout.split(","))


def default_dependencies() -> RestartDependencies:
    return RestartDependencies(
        scm=WindowsScm(),
        winsw_logs=FileWinSWLogs(),
        now=_utc_now,
        monotonic=time.monotonic,
        sleep=time.sleep,
        verify_provenance=verify_runtime_provenance,
    )


def _runner_receipt(path: Path, *, start_requested_at: datetime, new_pid: int) -> tuple[int, str]:
    try:
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RestartGateError(f"RELEASE_RUNNER_RECEIPT_MISSING_OR_INVALID: {path}") from error
    if modified_at <= start_requested_at:
        raise RestartGateError("STALE_RELEASE_RUNNER_RECEIPT")
    if not isinstance(value, dict) or not isinstance(value.get("pid"), int):
        raise RestartGateError("RELEASE_RUNNER_RECEIPT_MISSING_OR_INVALID")
    if value.get("parent_pid") != new_pid:
        raise RestartGateError("RUNNER_PARENT_PID_MISMATCH")
    return value["pid"], _sha256(path)


def _wait_for_runner_receipt(
    *,
    path: Path,
    start_requested_at: datetime,
    new_pid: int,
    dependencies: RestartDependencies,
    timeout_seconds: float,
    poll_interval_seconds: float,
    observe_generation: Callable[[], None],
) -> tuple[int, str]:
    """Observe, but never synthesize, the runner receipt after a new launch."""

    deadline = dependencies.monotonic() + timeout_seconds
    last_error: RestartGateError | None = None
    while True:
        observe_generation()
        try:
            receipt = _runner_receipt(path, start_requested_at=start_requested_at, new_pid=new_pid)
        except RestartGateError as error:
            last_error = error
            if str(error) == "RUNNER_PARENT_PID_MISMATCH":
                raise
        else:
            observe_generation()
            return receipt
        if dependencies.monotonic() >= deadline:
            if last_error is not None:
                raise last_error
            raise RestartGateError("RELEASE_RUNNER_RECEIPT_MISSING_OR_INVALID")
        dependencies.sleep(min(poll_interval_seconds, timeout_seconds))


def _wait_for_winsw_service_mode_start(
    *,
    path: Path,
    cursor: WinSWLogCursor,
    start_requested_at: datetime,
    dependencies: RestartDependencies,
    timeout_seconds: float,
    poll_interval_seconds: float,
    observe_generation: Callable[[], None],
) -> str:
    """Wait only for new WinSW service-mode evidence after the captured cursor."""

    deadline = dependencies.monotonic() + timeout_seconds
    while True:
        observe_generation()
        launch = dependencies.winsw_logs.service_mode_start_after(path, cursor, start_requested_at)
        if launch is not None and "Starting WinSW in service mode" in launch:
            observe_generation()
            return launch
        if dependencies.monotonic() >= deadline:
            raise RestartGateError("WINSW_SERVICE_MODE_START_MISSING")
        dependencies.sleep(min(poll_interval_seconds, timeout_seconds))


def _transition_service_verified(
    expectation: RestartExpectation,
    *,
    allow_stopped_recovery: bool,
    dependencies: RestartDependencies | None = None,
) -> RestartResult:
    """Run the canonical fail-closed restart or stopped-service recovery transition.

    After a new WinSW PID is observed, every later proof step is bound to that
    generation. SCM failure recovery is allowed to run independently, but its
    later generation can never satisfy this transition's evidence gates.
    """

    if expectation.timeout_seconds <= 0 or expectation.poll_interval_seconds <= 0:
        raise RestartGateError("INVALID_RESTART_TIMEOUT")
    if expectation.component != _expected_component(expectation.service_name):
        raise RestartGateError("SERVICE_COMPONENT_MISMATCH")
    dependencies = dependencies or default_dependencies()
    audit_path = expectation.evidence_directory / f"service-restart-{expectation.component}.json"
    stages: list[dict[str, str]] = []
    audit: dict[str, Any] = {
        "schema_version": 1,
        "service_name": expectation.service_name,
        "component": expectation.component,
        "expected_git_sha": expectation.expected_git_sha,
        "service_config_path": str(expectation.service_config_path),
        "wrapper_log_path": str(expectation.wrapper_log_path),
        "stages": stages,
        "observed_generations": [],
        "transition_mode": "RECOVER_STOPPED" if allow_stopped_recovery else "RESTART",
        "final_status": "PRECHECK",
    }

    def record(stage: str, timestamp: datetime) -> None:
        stages.append({"stage": stage, "timestamp_utc": timestamp.isoformat()})
        audit["final_status"] = stage
        _atomic_json(audit_path, audit)

    try:
        precheck_at = dependencies.now()
        before = dependencies.scm.inspect(expectation.service_name)
        executable = _image_executable(before.image_path)
        expected_sidecar = _sidecar_config(executable)
        if expectation.service_config_path.resolve() != expected_sidecar.resolve():
            raise RestartGateError("WINSW_CONFIG_DISCOVERY_MISMATCH")
        if not expectation.service_config_path.is_file():
            raise RestartGateError("WINSW_CONFIG_MISSING")
        if _sha256(expectation.service_config_path) != expectation.expected_config_sha256:
            raise RestartGateError("WINSW_CONFIG_HASH_MISMATCH")
        cursor = dependencies.winsw_logs.cursor(expectation.wrapper_log_path)
        audit.update(
            {
                "precheck": {
                    "service_state": before.state,
                    "old_pid": before.pid,
                    "service_image_path": before.image_path,
                    "winsw_executable_path": str(executable),
                    "adjacent_xml_path": str(expected_sidecar),
                    "xml_sha256": expectation.expected_config_sha256,
                    "wrapper_log_cursor": {
                        "path": str(cursor.path),
                        "byte_offset": cursor.byte_offset,
                        "modified_at": cursor.modified_at.isoformat(),
                    },
                },
                "old_pid": before.pid,
            }
        )
        record("PRECHECK", precheck_at)

        if before.state == "RUNNING" and before.pid > 0:
            old_pid = before.pid
            stop_requested_at = dependencies.now()
            audit["stop_requested_at"] = stop_requested_at.isoformat()
            record("STOP_REQUESTED", stop_requested_at)
            dependencies.scm.stop(expectation.service_name)
            stopped = _wait_for(
                predicate=lambda: (
                    snapshot
                    if (snapshot := dependencies.scm.inspect(expectation.service_name)).state
                    == "STOPPED"
                    else None
                ),
                dependencies=dependencies,
                timeout_seconds=expectation.timeout_seconds,
                poll_interval_seconds=expectation.poll_interval_seconds,
                error_code="SERVICE_STOP_TIMEOUT",
            )
            stopped_at = dependencies.now()
            audit["stopped_confirmed_at"] = stopped_at.isoformat()
            record("STOPPED_CONFIRMED", stopped_at)
            if stopped.pid == old_pid:
                raise RestartGateError("OLD_PID_STILL_BOUND")
            if dependencies.scm.is_process_alive(old_pid):
                raise RestartGateError("OLD_PID_STILL_ALIVE")
            record("OLD_PID_GONE", dependencies.now())
        elif before.state == "STOPPED" and before.pid == 0 and allow_stopped_recovery:
            old_pid = 0
            audit["recovery_entry_state"] = "STOPPED"
            record("STOPPED_PRECHECK", dependencies.now())
        else:
            raise RestartGateError("SERVICE_PRECHECK_NOT_RUNNING")

        start_requested_at = dependencies.now()
        audit["start_requested_at"] = start_requested_at.isoformat()
        record("START_REQUESTED", start_requested_at)
        dependencies.scm.start(expectation.service_name)
        running = _wait_for(
            predicate=lambda: (
                snapshot
                if (snapshot := dependencies.scm.inspect(expectation.service_name)).state
                == "RUNNING"
                else None
            ),
            dependencies=dependencies,
            timeout_seconds=expectation.timeout_seconds,
            poll_interval_seconds=expectation.poll_interval_seconds,
            error_code="SERVICE_START_TIMEOUT",
        )
        running_at = dependencies.now()
        audit["running_confirmed_at"] = running_at.isoformat()
        record("RUNNING_CONFIRMED", running_at)
        if running.pid <= 0 or (old_pid > 0 and running.pid == old_pid):
            raise RestartGateError("SERVICE_START_PID_FAILURE")
        audit["new_pid"] = running.pid
        record("NEW_PID_CONFIRMED", dependencies.now())

        generation = {
            "service_name": expectation.service_name,
            "winsw_pid": running.pid,
            "start_requested_at": start_requested_at.isoformat(),
            "running_observed_at": running_at.isoformat(),
        }
        audit["generation"] = generation

        def observe_generation() -> None:
            observed_at = dependencies.now()
            observed = dependencies.scm.inspect(expectation.service_name)
            audit["observed_generations"].append(
                {
                    "timestamp_utc": observed_at.isoformat(),
                    "state": observed.state,
                    "pid": observed.pid,
                }
            )
            if observed.state == "STOPPED":
                raise RestartGateError("SERVICE_GENERATION_LOST")
            if observed.state != "RUNNING" or observed.pid != running.pid:
                raise RestartGateError("SERVICE_GENERATION_CHANGED")

        winsw_launch = _wait_for_winsw_service_mode_start(
            path=expectation.wrapper_log_path,
            cursor=cursor,
            start_requested_at=start_requested_at,
            dependencies=dependencies,
            timeout_seconds=expectation.timeout_seconds,
            poll_interval_seconds=expectation.poll_interval_seconds,
            observe_generation=observe_generation,
        )
        audit["winsw_service_mode_launch"] = winsw_launch
        generation["winsw_service_mode_launch"] = winsw_launch
        record("WINSW_SERVICE_MODE_START_CONFIRMED", dependencies.now())

        receipt_path = (
            expectation.release_root / "runtime" / f"release-runtime-{expectation.component}.json"
        )
        runner_pid, receipt_hash = _wait_for_runner_receipt(
            path=receipt_path,
            start_requested_at=start_requested_at,
            new_pid=running.pid,
            dependencies=dependencies,
            timeout_seconds=expectation.timeout_seconds,
            poll_interval_seconds=expectation.poll_interval_seconds,
            observe_generation=observe_generation,
        )
        audit["release_runner_receipt"] = {
            "path": str(receipt_path),
            "sha256": receipt_hash,
            "runner_pid": runner_pid,
        }
        record("RELEASE_RUNNER_RECEIPT_CONFIRMED", dependencies.now())

        observe_generation()
        try:
            dependencies.verify_provenance(
                release_root=expectation.release_root,
                service_name=expectation.service_name,
                service_pid=running.pid,
                runner_pid=runner_pid,
                service_config_path=expectation.service_config_path,
                expected_git_sha=expectation.expected_git_sha,
            )
        except (ReleaseError, OSError, ValueError, RuntimeError) as error:
            raise RestartGateError(f"PROVENANCE_CONFIRMATION_FAILED: {error}") from error
        record("PROVENANCE_CONFIRMED", dependencies.now())
        observe_generation()
        audit["final_status"] = "PASS"
        _atomic_json(audit_path, audit)
        return RestartResult("PASS", old_pid, running.pid, audit_path)
    except RestartGateError as error:
        audit["failure"] = str(error)
        audit["final_status"] = "FAILED"
        try:
            _atomic_json(audit_path, audit)
        except RestartGateError as audit_error:
            raise RestartGateError(f"{error}; {audit_error}") from audit_error
        raise
    except Exception as error:
        failure = RestartGateError(f"UNEXPECTED_RESTART_GATE_FAILURE: {error}")
        audit["failure"] = str(failure)
        audit["final_status"] = "FAILED"
        try:
            _atomic_json(audit_path, audit)
        except RestartGateError as audit_error:
            raise RestartGateError(f"{failure}; {audit_error}") from audit_error
        raise failure from error


def restart_service_verified(
    expectation: RestartExpectation, *, dependencies: RestartDependencies | None = None
) -> RestartResult:
    """Restart a currently running service through the canonical transition gate."""

    return _transition_service_verified(
        expectation, allow_stopped_recovery=False, dependencies=dependencies
    )


def recover_service_verified(
    expectation: RestartExpectation, *, dependencies: RestartDependencies | None = None
) -> RestartResult:
    """Restore a stopped service without sending a redundant stop request.

    Rollback uses this sibling entry point when a failed candidate has already
    left the service STOPPED. It shares every generation and provenance gate
    with a normal restart.
    """

    return _transition_service_verified(
        expectation, allow_stopped_recovery=True, dependencies=dependencies
    )
