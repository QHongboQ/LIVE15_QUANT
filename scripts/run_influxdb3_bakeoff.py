"""Isolated InfluxDB 3 Core bakeoff retaining canonical LIVE15 wire payloads."""

from __future__ import annotations

import base64
import json
import subprocess
import time
from pathlib import Path

from run_commercial_archive_benchmark import (
    _decimal_fidelity,
    _replay_hash,
    _timestamp_fidelity,
    load_complete_stream,
)

from live15_quant.ws_archive import event_from_wire, event_to_wire

ROOT = Path(r"D:\LIVE15_DEV\storage-bakeoff-001")
CLI = ROOT / "influxdb3" / "influxdb3.exe"
HOST = "http://127.0.0.1:18181"
DB = "live15_bakeoff"


def esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def main() -> None:
    records = load_complete_stream(ROOT / "live15.sqlite3")
    lp = ROOT / "influx.lp"
    out = ROOT / "influx.jsonl"
    with lp.open("w", encoding="utf-8", newline="\n") as f:
        for r in records:
            wire = base64.b64encode(
                json.dumps(event_to_wire(r), separators=(",", ":")).encode()
            ).decode()
            ts = int(r.socket_received_timestamp.timestamp() * 1_000_000_000)
            fields = (
                f"subscription_id={r.subscription_id}i,sequence={r.sequence}i,"
                f'wire_b64="{wire}"'
            )
            f.write(
                f"live15_ws,connection_id={esc(r.connection_id)},row_id={r.row_id} {fields} {ts}\n"
            )
    start = time.perf_counter()
    subprocess.run([str(CLI), "write", "-H", HOST, "-d", DB, "-f", str(lp), "-q"], check=True)
    ingest = time.perf_counter() - start
    start = time.perf_counter()
    subprocess.run(
        [
            str(CLI),
            "query",
            "-H",
            HOST,
            "-d",
            DB,
            "--format",
            "jsonl",
            "-o",
            str(out),
            "SELECT wire_b64 FROM live15_ws ORDER BY row_id",
        ],
        check=True,
    )
    decoded = tuple(
        event_from_wire(json.loads(base64.b64decode(json.loads(line)["wire_b64"])))
        for line in out.read_text(encoding="utf-8").splitlines()
    )
    export = time.perf_counter() - start
    start = time.perf_counter()
    h = _replay_hash(decoded)
    replay = time.perf_counter() - start
    size = sum(p.stat().st_size for p in (ROOT / "influxdb3-data").rglob("*") if p.is_file())
    result = {
        "format": "InfluxDB 3 Core",
        "records": len(records),
        "total_bytes": size,
        "bytes_per_event": size / len(records),
        "ingest_events_per_second": len(records) / ingest,
        "export_events_per_second": len(records) / export,
        "replay_events_per_second": len(records) / replay,
        "wire_mapping": (
            "canonical event_to_wire JSON encoded as base64 string field; "
            "connection_id and row_id tags prevent timestamp collision"
        ),
        "verification": {
            "exact_round_trip": decoded == records,
            "decimal_fidelity": _decimal_fidelity(records, decoded),
            "utc_microsecond_timestamp_fidelity": _timestamp_fidelity(records, decoded),
            "ordering": [x.row_id for x in decoded] == [x.row_id for x in records],
            "identity_field_preservation": all(
                a.row_id == b.row_id
                and a.connection_id == b.connection_id
                and a.subscription_id == b.subscription_id
                and a.sequence == b.sequence
                for a, b in zip(records, decoded, strict=True)
            ),
            "deterministic_replay_equivalence": h == _replay_hash(records),
        },
        "replay_hash": h,
    }
    (ROOT / "influx-receipt.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
