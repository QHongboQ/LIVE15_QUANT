from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from live15_quant.deployment_restart import (
    FileWinSWLogs,
    RestartDependencies,
    RestartExpectation,
    RestartGateError,
    ServiceSnapshot,
    WinSWLogCursor,
    restart_service_verified,
)


@dataclass
class FakeClock:
    current: datetime = datetime(2026, 8, 29, tzinfo=UTC)
    elapsed: float = 0.0

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds
        self.current += timedelta(seconds=seconds)


class FakeScm:
    def __init__(
        self,
        *,
        before: ServiceSnapshot,
        after_stop: ServiceSnapshot,
        after_start: ServiceSnapshot,
        old_process_alive_after_stop: bool = False,
    ) -> None:
        self.current = before
        self.after_stop = after_stop
        self.after_start = after_start
        self.old_process_alive_after_stop = old_process_alive_after_stop
        self.stop_calls = 0
        self.start_calls = 0

    def inspect(self, service_name: str) -> ServiceSnapshot:
        assert service_name == "LIVE15Recorder"
        return self.current

    def stop(self, service_name: str) -> None:
        assert service_name == "LIVE15Recorder"
        self.stop_calls += 1
        self.current = self.after_stop

    def start(self, service_name: str) -> None:
        assert service_name == "LIVE15Recorder"
        self.start_calls += 1
        self.current = self.after_start

    def is_process_alive(self, pid: int) -> bool:
        return pid == 101 and self.old_process_alive_after_stop


class FakeWinSWLogs:
    def __init__(self, launch: str | None) -> None:
        self.launch = launch

    def cursor(self, path: Path) -> WinSWLogCursor:
        return WinSWLogCursor(
            path=path, byte_offset=7, modified_at=datetime(2026, 8, 29, tzinfo=UTC)
        )

    def service_mode_start_after(
        self, path: Path, cursor: WinSWLogCursor, after: datetime
    ) -> str | None:
        assert path == cursor.path
        return self.launch


def _expectation(tmp_path: Path) -> RestartExpectation:
    winsw = tmp_path / "winsw"
    winsw.mkdir()
    config = winsw / "LIVE15Recorder.xml"
    config.write_text("<service><id>LIVE15Recorder</id></service>", encoding="utf-8")
    (winsw / "LIVE15Recorder.exe").write_bytes(b"winsw")
    wrapper_log = tmp_path / "LIVE15Recorder.wrapper.log"
    wrapper_log.write_text("old log\n", encoding="utf-8")
    return RestartExpectation(
        service_name="LIVE15Recorder",
        component="recorder",
        release_root=tmp_path,
        evidence_directory=tmp_path / "runtime/deployment-evidence/deployment",
        service_config_path=config,
        expected_config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        wrapper_log_path=wrapper_log,
        expected_git_sha="a" * 40,
        timeout_seconds=3.0,
        poll_interval_seconds=1.0,
    )


def _snapshots(tmp_path: Path) -> tuple[ServiceSnapshot, ServiceSnapshot, ServiceSnapshot]:
    image_path = f'"{tmp_path / "winsw/LIVE15Recorder.exe"}"'
    return (
        ServiceSnapshot("RUNNING", 101, image_path),
        ServiceSnapshot("STOPPED", 0, image_path),
        ServiceSnapshot("RUNNING", 202, image_path),
    )


def _write_runner_receipt(
    expectation: RestartExpectation,
    *,
    pid: int = 303,
    parent_pid: int = 202,
    modified_at: datetime | None = None,
) -> Path:
    receipt = expectation.release_root / "runtime/release-runtime-recorder.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"pid": pid, "parent_pid": parent_pid}), encoding="utf-8")
    if modified_at is not None:
        timestamp = modified_at.timestamp()
        os.utime(receipt, (timestamp, timestamp))
    return receipt


def _dependencies(
    scm: FakeScm, logs: FakeWinSWLogs, clock: FakeClock, provenance=None
) -> RestartDependencies:
    return RestartDependencies(
        scm=scm,
        winsw_logs=logs,
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        verify_provenance=provenance or (lambda **_: None),
    )


def test_stop_command_success_but_service_remains_running_fails_closed(tmp_path: Path) -> None:
    expectation = _expectation(tmp_path)
    before, _, after_start = _snapshots(tmp_path)
    clock = FakeClock()
    scm = FakeScm(before=before, after_stop=before, after_start=after_start)

    with pytest.raises(RestartGateError, match="SERVICE_STOP_TIMEOUT"):
        restart_service_verified(
            expectation, dependencies=_dependencies(scm, FakeWinSWLogs("service"), clock)
        )

    assert scm.stop_calls == 1
    assert scm.start_calls == 0
    failed_audit = expectation.evidence_directory / "service-restart-recorder.json"
    assert failed_audit.is_file() and failed_audit.stat().st_size > 0
    assert json.loads(failed_audit.read_text(encoding="utf-8"))["final_status"] == "FAILED"


def test_stopped_service_with_old_process_still_alive_fails_closed(tmp_path: Path) -> None:
    expectation = _expectation(tmp_path)
    before, stopped, after_start = _snapshots(tmp_path)
    scm = FakeScm(
        before=before,
        after_stop=stopped,
        after_start=after_start,
        old_process_alive_after_stop=True,
    )

    with pytest.raises(RestartGateError, match="OLD_PID_STILL_ALIVE"):
        restart_service_verified(
            expectation, dependencies=_dependencies(scm, FakeWinSWLogs("service"), FakeClock())
        )


@pytest.mark.parametrize("after_start", ["same_pid", "zero_pid"])
def test_start_success_without_new_pid_fails_closed(tmp_path: Path, after_start: str) -> None:
    expectation = _expectation(tmp_path)
    before, stopped, started = _snapshots(tmp_path)
    if after_start == "same_pid":
        started = ServiceSnapshot("RUNNING", 101, started.image_path)
    else:
        started = ServiceSnapshot("RUNNING", 0, started.image_path)
    scm = FakeScm(before=before, after_stop=stopped, after_start=started)

    with pytest.raises(RestartGateError, match="SERVICE_START_PID_FAILURE"):
        restart_service_verified(
            expectation, dependencies=_dependencies(scm, FakeWinSWLogs("service"), FakeClock())
        )


def test_new_pid_progresses_but_missing_service_mode_log_fails(tmp_path: Path) -> None:
    expectation = _expectation(tmp_path)
    before, stopped, started = _snapshots(tmp_path)
    scm = FakeScm(before=before, after_stop=stopped, after_start=started)

    with pytest.raises(RestartGateError, match="WINSW_SERVICE_MODE_START_MISSING"):
        restart_service_verified(
            expectation, dependencies=_dependencies(scm, FakeWinSWLogs(None), FakeClock())
        )


def test_console_mode_only_log_is_rejected(tmp_path: Path) -> None:
    expectation = _expectation(tmp_path)
    before, stopped, started = _snapshots(tmp_path)
    scm = FakeScm(before=before, after_stop=stopped, after_start=started)

    with pytest.raises(RestartGateError, match="WINSW_SERVICE_MODE_START_MISSING"):
        restart_service_verified(
            expectation,
            dependencies=_dependencies(
                scm, FakeWinSWLogs("Starting WinSW in console mode"), FakeClock()
            ),
        )


def test_file_winsw_log_reader_requires_a_new_service_mode_line(tmp_path: Path) -> None:
    log_path = tmp_path / "LIVE15Recorder.wrapper.log"
    log_path.write_text("historical line\n", encoding="utf-8")
    logs = FileWinSWLogs()
    cursor = logs.cursor(log_path)
    after = datetime(2026, 8, 29, tzinfo=UTC)
    local_after = after.astimezone()
    before_start = (local_after - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    after_start = (local_after + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    with log_path.open("a", encoding="utf-8") as output:
        output.write(f"{before_start} INFO - Starting WinSW in service mode\n")
        output.write(f"{after_start} DEBUG - Starting WinSW in console mode\n")
    assert logs.service_mode_start_after(log_path, cursor, after) is None
    with log_path.open("a", encoding="utf-8") as output:
        output.write(f"{after_start} DEBUG - Starting WinSW in service mode\n")
    assert "service mode" in str(logs.service_mode_start_after(log_path, cursor, after))


def test_stale_runner_receipt_is_rejected_before_provenance(tmp_path: Path) -> None:
    expectation = _expectation(tmp_path)
    _write_runner_receipt(expectation, modified_at=datetime(2026, 8, 28, tzinfo=UTC))
    before, stopped, started = _snapshots(tmp_path)
    scm = FakeScm(before=before, after_stop=stopped, after_start=started)
    called = False

    def provenance(**_: object) -> None:
        nonlocal called
        called = True

    with pytest.raises(RestartGateError, match="STALE_RELEASE_RUNNER_RECEIPT"):
        restart_service_verified(
            expectation,
            dependencies=_dependencies(
                scm, FakeWinSWLogs("Starting WinSW in service mode"), FakeClock(), provenance
            ),
        )

    assert not called


def test_parent_pid_mismatch_is_rejected_before_provenance(tmp_path: Path) -> None:
    expectation = _expectation(tmp_path)
    _write_runner_receipt(
        expectation, parent_pid=999, modified_at=datetime(2026, 8, 30, tzinfo=UTC)
    )
    before, stopped, started = _snapshots(tmp_path)
    scm = FakeScm(before=before, after_stop=stopped, after_start=started)

    with pytest.raises(RestartGateError, match="RUNNER_PARENT_PID_MISMATCH"):
        restart_service_verified(
            expectation,
            dependencies=_dependencies(
                scm, FakeWinSWLogs("Starting WinSW in service mode"), FakeClock()
            ),
        )


def test_wrong_release_or_hash_from_provenance_fails_closed(tmp_path: Path) -> None:
    expectation = _expectation(tmp_path)
    _write_runner_receipt(expectation, modified_at=datetime(2026, 8, 30, tzinfo=UTC))
    before, stopped, started = _snapshots(tmp_path)
    scm = FakeScm(before=before, after_stop=stopped, after_start=started)

    def provenance(**_: object) -> None:
        raise RuntimeError("wrong release/hash")

    with pytest.raises(RestartGateError, match="PROVENANCE_CONFIRMATION_FAILED"):
        restart_service_verified(
            expectation,
            dependencies=_dependencies(
                scm, FakeWinSWLogs("Starting WinSW in service mode"), FakeClock(), provenance
            ),
        )


def test_valid_new_service_and_fresh_receipt_passes_and_writes_nonempty_audit(
    tmp_path: Path,
) -> None:
    expectation = _expectation(tmp_path)
    _write_runner_receipt(expectation, modified_at=datetime(2026, 8, 30, tzinfo=UTC))
    before, stopped, started = _snapshots(tmp_path)
    scm = FakeScm(before=before, after_stop=stopped, after_start=started)
    calls: list[dict[str, object]] = []

    def provenance(**kwargs: object) -> None:
        calls.append(kwargs)

    result = restart_service_verified(
        expectation,
        dependencies=_dependencies(
            scm, FakeWinSWLogs("Starting WinSW in service mode"), FakeClock(), provenance
        ),
    )

    assert result.status == "PASS"
    assert result.old_pid == 101
    assert result.new_pid == 202
    assert calls and calls[0]["service_pid"] == 202
    audit = expectation.evidence_directory / "service-restart-recorder.json"
    assert audit.is_file() and audit.stat().st_size > 0
    assert json.loads(audit.read_text(encoding="utf-8"))["final_status"] == "PASS"
    assert not list(audit.parent.glob(".service-restart-recorder.json.*.tmp"))


def test_legacy_rollback_uses_the_same_verified_restart_gate(tmp_path: Path) -> None:
    expectation = _expectation(tmp_path)
    expectation = RestartExpectation(**{**expectation.__dict__, "expected_git_sha": "UNPROVEN"})
    _write_runner_receipt(expectation, modified_at=datetime(2026, 8, 30, tzinfo=UTC))
    before, stopped, started = _snapshots(tmp_path)
    scm = FakeScm(before=before, after_stop=stopped, after_start=started)
    seen: list[str] = []

    def provenance(**kwargs: object) -> None:
        seen.append(str(kwargs["expected_git_sha"]))

    result = restart_service_verified(
        expectation,
        dependencies=_dependencies(
            scm, FakeWinSWLogs("Starting WinSW in service mode"), FakeClock(), provenance
        ),
    )

    assert result.status == "PASS"
    assert seen == ["UNPROVEN"]
