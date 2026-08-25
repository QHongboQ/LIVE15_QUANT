"""Atomic, secret-free runtime component status and singleton leases."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from live15_quant.recorder_control import process_alive


class RuntimeStatusError(RuntimeError):
    """Runtime ownership or status facts are unsafe to use."""


def utc_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise RuntimeStatusError("runtime timestamp must be timezone-aware")
    return current.astimezone(UTC).isoformat(timespec="microseconds")


def atomic_json(
    path: Path,
    payload: dict[str, object],
    *,
    replace: Callable[[Path, Path], None] = os.replace,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        for attempt in range(6):
            try:
                replace(temporary, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                sleep(0.01 * (2**attempt))
    finally:
        temporary.unlink(missing_ok=True)


def read_json(
    path: Path,
    *,
    maximum_bytes: int = 64 * 1024,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object] | None:
    for attempt in range(6):
        try:
            if path.stat().st_size > maximum_bytes:
                raise RuntimeStatusError(f"runtime status exceeds {maximum_bytes} bytes")
            payload = json.loads(path.read_text(encoding="utf-8"))
            break
        except FileNotFoundError:
            return None
        except PermissionError as error:
            # Windows readers can briefly lose the race with an atomic
            # os.replace or a deny-read handle. Mirror atomic_json's bounded
            # backoff so observability cannot tear down healthy market data.
            if attempt == 5:
                raise RuntimeStatusError("runtime status is temporarily unreadable") from error
            sleep(0.01 * (2**attempt))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeStatusError("runtime status is malformed") from error
    else:  # pragma: no cover - loop always returns, breaks, or raises
        raise RuntimeStatusError("runtime status is temporarily unreadable")
    if not isinstance(payload, dict):
        raise RuntimeStatusError("runtime status must be an object")
    return payload


@dataclass(slots=True)
class RuntimePidLease:
    """Exclusive fixed-path PID ownership with stale lease recovery."""

    path: Path
    pid: int = 0
    _held: bool = False

    def __post_init__(self) -> None:
        if self.pid <= 0:
            self.pid = os.getpid()

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as error:
                existing = self._read_owner()
                if existing is not None and process_alive(existing):
                    raise RuntimeStatusError(
                        f"runtime component is already running (pid={existing})"
                    ) from error
                if attempt:
                    raise RuntimeStatusError(
                        "stale runtime lease could not be recovered"
                    ) from error
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(f"{self.pid}\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._held = True
            return
        raise RuntimeStatusError("runtime lease acquisition failed")

    def release(self) -> None:
        if self._held and self._read_owner() == self.pid:
            self.path.unlink(missing_ok=True)
        self._held = False

    def _read_owner(self) -> int | None:
        try:
            if self.path.stat().st_size > 32:
                return None
            value = int(self.path.read_text(encoding="ascii").strip())
            return value if value > 0 else None
        except (FileNotFoundError, OSError, ValueError):
            return None


def component_status(
    *,
    name: str,
    status: str,
    pid: int,
    started_at: str,
    last_heartbeat: str,
    expected_mode: str,
    last_error: str | None = None,
    working_directory: Path | None = None,
    log_path: Path | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "name": name,
        "status": status,
        "pid": pid,
        "started_at": started_at,
        "last_heartbeat": last_heartbeat,
        "last_error": last_error,
        "process_alive": process_alive(pid),
        "expected_mode": expected_mode,
        "working_directory": str(working_directory.resolve()) if working_directory else None,
        "log_path": str(log_path.resolve()) if log_path else None,
    }
    if extra:
        result.update(extra)
    return result
