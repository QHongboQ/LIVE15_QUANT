"""Durable, secret-free acceptance facts for bounded SDK-host probes."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path


def write_smoke_result_atomic(
    path: Path,
    payload: Mapping[str, object],
    *,
    replace: Callable[[str | Path, str | Path], None] = os.replace,
    fsync: Callable[[int], None] = os.fsync,
) -> None:
    """Publish one fully-written, fsynced result without exposing a partial JSON file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            fsync(handle.fileno())
        replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
