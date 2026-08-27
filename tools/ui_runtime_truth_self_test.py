"""Read-only Control Center UI/runtime truth self-test.

The probe detects and diagnoses link failures; it never starts/stops services and never
performs writes to the Recorder or database. Its JSON artifact is intentionally minimal and
contains no response bodies or credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REQUIRED_ENDPOINTS = (
    "/",
    "/api/health",
    "/api/system",
    "/api/markets",
    "/api/account?profile=production_primary",
    "/api/archive",
    "/api/storage",
)
FORBIDDEN_MARKERS = ("mock", "placeholder", "sample", "fake")


def _get_json(url: str, timeout: float) -> tuple[int, Any | None, str | None]:
    request = urllib.request.Request(url, headers={"Accept": "application/json,text/html"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            raw = response.read(256 * 1024)
        if url.endswith("/"):
            return status, None, None
        try:
            return status, json.loads(raw), None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return status, None, "invalid_json"
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        return 0, None, type(error).__name__


def _contains_forbidden(value: Any) -> bool:
    if isinstance(value, str):
        lowered = value.lower()
        return any(marker in lowered for marker in FORBIDDEN_MARKERS)
    if isinstance(value, dict):
        return any(_contains_forbidden(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_forbidden(item) for item in value)
    return False


def run_checks(root: Path, base_url: str, timeout: float = 3.0) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    payloads: dict[str, Any] = {}
    for path in REQUIRED_ENDPOINTS:
        url = f"{base_url.rstrip('/')}{path}"
        status, payload, error = _get_json(url, timeout)
        ok = status == 200 and (path == "/" or error is None)
        checks.append(
            {
                "name": f"endpoint:{path}",
                "status": "PASS" if ok else "FAIL",
                "http": status,
                "error": error,
            }
        )
        if payload is not None:
            payloads[path] = payload

    health = payloads.get("/api/health")
    system = payloads.get("/api/system")
    if isinstance(health, dict):
        required = (
            "status",
            "recorder_state",
            "heartbeat_status",
            "observed_at",
            "fatal_task",
            "fatal_error_type",
        )
        missing = [key for key in required if key not in health]
        checks.append(
            {
                "name": "health_schema",
                "status": "PASS" if not missing else "FAIL",
                "missing": missing,
            }
        )
        checks.append(
            {
                "name": "fatal_fields",
                "status": "PASS"
                if health.get("fatal_task") is None and health.get("fatal_error_type") is None
                else "WARN",
            }
        )
        checks.append(
            {
                "name": "heartbeat",
                "status": "PASS" if health.get("heartbeat_status") == "available" else "WARN",
            }
        )
    if isinstance(system, dict):
        expected = {"service": "LIVE15 Control Center", "bind_host": "127.0.0.1"}
        mismatches = {
            key: system.get(key) for key, value in expected.items() if system.get(key) != value
        }
        checks.append(
            {
                "name": "system_identity",
                "status": "PASS" if not mismatches else "FAIL",
                "mismatches": mismatches,
            }
        )

    account = payloads.get("/api/account?profile=production_primary")
    if isinstance(account, dict) and account.get("status") == "UNAVAILABLE":
        checks.append(
            {"name": "account_read", "status": "WARN", "reason": "account_endpoint_unavailable"}
        )
    elif account is not None:
        checks.append({"name": "account_read", "status": "PASS"})

    for path, payload in payloads.items():
        if _contains_forbidden(payload):
            checks.append(
                {
                    "name": f"payload_markers:{path}",
                    "status": "FAIL",
                    "reason": "forbidden_placeholder_marker",
                }
            )

    launcher = root / "scripts" / "start_control_center.cmd"
    python = root / ".venv" / "Scripts" / "python.exe"
    checks.append(
        {
            "name": "canonical_launcher",
            "status": "PASS" if launcher.is_file() else "FAIL",
            "path": str(launcher),
        }
    )
    checks.append(
        {
            "name": "canonical_python",
            "status": "PASS" if python.is_file() else "WARN",
            "path": str(python),
        }
    )

    statuses = {item["status"] for item in checks}
    overall = "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS"
    return {
        "schema_version": "1.0",
        "observed_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "overall": overall,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only LIVE15 Control Center truth probe")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("runtime/ui_truth_health.json"))
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args(argv)
    report = run_checks(args.root.resolve(), args.base_url, max(0.1, args.timeout))
    output = args.output if args.output.is_absolute() else args.root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"overall": report["overall"], "output": str(output)}, sort_keys=True))
    return 1 if report["overall"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
