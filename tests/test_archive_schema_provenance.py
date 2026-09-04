from live15_quant.archive_arrow import ARROW_WS_EVENT_SCHEMA


def test_archive_semantic_schema_is_format_neutral() -> None:
    metadata = ARROW_WS_EVENT_SCHEMA.metadata or {}

    assert b"live15.archive.format" not in metadata
    assert b"live15.compression" not in metadata
    assert metadata[b"live15.archive.schema_version"] == b"1"
    assert metadata[b"live15.truth"] == b"kalshi-ws-replay"
