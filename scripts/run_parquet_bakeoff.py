"""One fixed-configuration Parquet+ZSTD bakeoff using LIVE15 record mapping."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from run_commercial_archive_benchmark import (
    _decimal_fidelity,
    _replay_hash,
    _timestamp_fidelity,
    load_complete_stream,
)

from live15_quant.archive_arrow import batch_to_records, records_to_batch

SNAPSHOT = Path(r"D:\LIVE15_DEV\storage-bakeoff-001\live15.sqlite3")
OUTPUT = Path(r"D:\LIVE15_DEV\storage-bakeoff-001\parquet-receipt.json")
PATH = Path(r"D:\LIVE15_DEV\storage-bakeoff-001\parquet\events.parquet")


def main() -> None:
    records = load_complete_stream(SNAPSHOT)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    pq.write_table(
        pa.Table.from_batches((records_to_batch(records),)),
        PATH,
        compression="zstd",
        use_dictionary=True,
        row_group_size=100_000,
    )
    encode = time.perf_counter() - started
    started = time.perf_counter()
    decoded = tuple(
        item for batch in pq.read_table(PATH).to_batches() for item in batch_to_records(batch)
    )
    decode = time.perf_counter() - started
    started = time.perf_counter()
    replay_hash = _replay_hash(decoded)
    replay = time.perf_counter() - started
    result = {
        "format": "Parquet+ZSTD",
        "records": len(records),
        "total_bytes": PATH.stat().st_size,
        "bytes_per_event": PATH.stat().st_size / len(records),
        "encode_events_per_second": len(records) / encode,
        "decode_events_per_second": len(records) / decode,
        "replay_events_per_second": len(records) / replay,
        "settings": {"compression": "zstd", "use_dictionary": True, "row_group_size": 100000},
        "verification": {
            "exact_round_trip": decoded == records,
            "decimal_fidelity": _decimal_fidelity(records, decoded),
            "utc_microsecond_timestamp_fidelity": _timestamp_fidelity(records, decoded),
            "ordering": [x.row_id for x in decoded] == [x.row_id for x in records],
            "deterministic_replay_equivalence": replay_hash == _replay_hash(records),
        },
        "replay_hash": replay_hash,
    }
    OUTPUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
