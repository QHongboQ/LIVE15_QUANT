from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from live15_quant.config import Settings
from live15_quant.models import Asset, FreshnessState
from live15_quant.providers.pyth import (
    PYTH_ASSETS,
    PYTH_FEEDS,
    PythCredentialError,
    PythFeedDemultiplexer,
    PythHermesClient,
    PythNetworkError,
    PythPayloadError,
    PythRateLimitError,
    PythUpdateBatch,
    parse_update_payload,
)

RECEIVED = datetime(2026, 8, 21, 0, 0, 1, tzinfo=UTC)


def feed_item(asset: Asset, *, publish_time: int = 1787270400) -> dict[str, object]:
    return {
        "id": PYTH_FEEDS[asset][1],
        "price": {
            "price": "338812345",
            "conf": "1234",
            "expo": -5,
            "publish_time": publish_time,
        },
    }


def payload(assets: tuple[Asset, ...] = PYTH_ASSETS) -> dict[str, object]:
    return {"parsed": [feed_item(asset) for asset in assets]}


class FakeResponse:
    def __init__(
        self,
        body: object,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        lines: list[str] | None = None,
    ) -> None:
        self.body = body
        self.status_code = status_code
        self.headers = headers or {}
        self.lines = lines or []
        self.closed = False

    def json(self) -> object:
        return self.body

    def iter_lines(self, *, decode_unicode: bool):
        assert decode_unicode is True
        yield from self.lines

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


class BlockingResponse(FakeResponse):
    def iter_lines(self, *, decode_unicode: bool):
        assert decode_unicode is True
        while not self.closed:
            time.sleep(0.001)
            yield ":keepalive"


class DisconnectingResponse(FakeResponse):
    def iter_lines(self, *, decode_unicode: bool):
        assert decode_unicode is True
        raise requests.ConnectionError("transport dropped")
        yield  # pragma: no cover


def client(tmp_path, session, **settings):
    key_path = tmp_path / ".secrets" / "pyth.key"
    key_path.parent.mkdir(exist_ok=True)
    key_path.write_text("very-secret-value", encoding="utf-8")
    return PythHermesClient(Settings(pyth_api_key_path=key_path, **settings), session=session)


def test_parse_preserves_decimal_confidence_clocks_and_stale_state() -> None:
    batch = parse_update_payload(
        payload((Asset.GOLD,)),
        received=RECEIVED,
        source="https://official.example/stream",
        max_source_age_seconds=2,
        require_all=False,
    )
    observation = batch.observations[0]
    assert observation.price == Decimal("3388.12345")
    assert observation.confidence == Decimal("0.01234")
    assert observation.source_timestamp == datetime.fromtimestamp(1787270400, UTC)
    assert observation.received_timestamp is RECEIVED
    assert observation.freshness is FreshnessState.FRESH

    stale = parse_update_payload(
        payload((Asset.GOLD,)),
        received=RECEIVED + timedelta(seconds=30),
        source="source",
        max_source_age_seconds=2,
        require_all=False,
    ).observations[0]
    assert stale.freshness is FreshnessState.STALE


def test_project_local_secret_path_is_allowed_without_exposing_value(
    tmp_path: Path, monkeypatch
) -> None:
    secret = tmp_path / ".secrets" / "pyth-api-key.txt"
    secret.parent.mkdir()
    secret.write_bytes(b"opaque")
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    client = PythHermesClient(Settings(pyth_api_key_path=secret), session=FakeSession([]))
    client.close()


def test_one_malformed_feed_isolated_from_other_four() -> None:
    body = payload()
    items = body["parsed"]
    assert isinstance(items, list)
    items[1] = {"id": PYTH_FEEDS[Asset.SILVER][1], "price": {"price": "bad"}}
    batch = parse_update_payload(
        body,
        received=RECEIVED,
        source="source",
        max_source_age_seconds=15,
        require_all=True,
    )
    assert {item.asset for item in batch.observations} == set(PYTH_ASSETS) - {Asset.SILVER}
    assert [(issue.asset, issue.code) for issue in batch.issues] == [
        (Asset.SILVER, "malformed_price")
    ]


def test_windows_invalid_epoch_is_feed_local(monkeypatch) -> None:
    body = payload((Asset.GOLD, Asset.BNB))
    monkeypatch.setattr(
        "live15_quant.providers.pyth.datetime",
        type(
            "FailingDateTime",
            (),
            {"fromtimestamp": staticmethod(lambda *_args: (_ for _ in ()).throw(OSError()))},
        ),
    )
    batch = parse_update_payload(
        body,
        received=RECEIVED,
        source="source",
        max_source_age_seconds=15,
        require_all=False,
    )
    assert not batch.observations
    assert {issue.asset for issue in batch.issues} == {Asset.GOLD, Asset.BNB}


def test_envelope_error_is_explicit_but_missing_feed_is_feed_local() -> None:
    with pytest.raises(PythPayloadError, match="missing parsed"):
        parse_update_payload(
            {}, received=RECEIVED, source="source", max_source_age_seconds=15, require_all=True
        )
    batch = parse_update_payload(
        {"parsed": []},
        received=RECEIVED,
        source="source",
        max_source_age_seconds=15,
        require_all=True,
    )
    assert {issue.asset for issue in batch.issues} == set(PYTH_ASSETS)


def test_demux_suppresses_duplicate_and_detects_out_of_order_per_feed() -> None:
    first = parse_update_payload(
        payload((Asset.GOLD, Asset.BNB)),
        received=RECEIVED,
        source="source",
        max_source_age_seconds=15,
        require_all=False,
    )
    demux = PythFeedDemultiplexer()
    assert len(demux.accept(first).observations) == 2
    duplicate = demux.accept(first)
    assert not duplicate.observations
    assert {issue.code for issue in duplicate.issues} == {"duplicate"}
    gold, bnb = first.observations
    older_gold = replace(gold, source_timestamp=gold.source_timestamp - timedelta(seconds=1))
    newer_bnb = replace(bnb, source_timestamp=bnb.source_timestamp + timedelta(seconds=1))
    result = demux.accept(PythUpdateBatch((older_gold, newer_bnb)))
    assert result.observations == (newer_bnb,)
    assert result.issues[0].code == "out_of_order"
    assert result.issues[0].asset is Asset.GOLD


def test_demux_preserves_changed_state_with_same_second_publish_time() -> None:
    first = parse_update_payload(
        payload((Asset.GOLD,)),
        received=RECEIVED,
        source="source",
        max_source_age_seconds=15,
        require_all=False,
    )
    demux = PythFeedDemultiplexer()
    demux.accept(first)
    changed = replace(first.observations[0], price=Decimal("9999"))
    assert demux.accept(PythUpdateBatch((changed,))).observations == (changed,)
    repeated = demux.accept(PythUpdateBatch((changed,)))
    assert not repeated.observations
    assert repeated.issues[0].code == "duplicate"


def test_batch_rest_uses_one_request_for_all_five_feeds(tmp_path) -> None:
    response = FakeResponse(payload())
    session = FakeSession([response])
    hermes = client(tmp_path, session)
    try:
        batch = hermes.latest_batch()
        assert len(batch.observations) == 5
        assert len(session.calls) == 1
        call = session.calls[0]
        assert call["params"] == [("ids[]", PYTH_FEEDS[asset][1]) for asset in PYTH_ASSETS]
        assert call["headers"]["Authorization"] == "Bearer very-secret-value"  # type: ignore[index]
        assert call["stream"] is False
    finally:
        hermes.close()
    assert response.closed and session.closed


def test_one_sse_connection_demuxes_multiple_events_and_closes(tmp_path) -> None:
    lines = [
        f"data:{json.dumps(payload((Asset.GOLD, Asset.SILVER)))}",
        "",
        f"data:{json.dumps(payload((Asset.WTI_OIL, Asset.HYPE, Asset.BNB)))}",
        "",
    ]
    response = FakeResponse({}, lines=lines)
    session = FakeSession([response])
    hermes = client(tmp_path, session)
    try:
        batches = tuple(hermes.stream_batches())
        assert [len(batch.observations) for batch in batches] == [2, 3]
        for batch in batches:
            assert batch.socket_received_monotonic_ns is not None
            assert batch.parse_completed_monotonic_ns is not None
            assert batch.parse_completed_monotonic_ns >= batch.socket_received_monotonic_ns
        assert len(session.calls) == 1
        assert session.calls[0]["stream"] is True
    finally:
        hermes.close()
    assert response.closed and session.closed


def test_malformed_sse_event_does_not_close_or_pollute_next_event(tmp_path) -> None:
    lines = ["data:not-json", "", f"data:{json.dumps(payload((Asset.BNB,)))}", ""]
    hermes = client(tmp_path, FakeSession([FakeResponse({}, lines=lines)]))
    try:
        batches = tuple(hermes.stream_batches())
        assert batches[0].issues[0].code == "malformed_sse_json"
        assert batches[1].observations[0].asset is Asset.BNB
    finally:
        hermes.close()


def test_stream_transport_drop_is_sanitized_for_bounded_reconnect(tmp_path) -> None:
    hermes = client(tmp_path, FakeSession([DisconnectingResponse({})]))
    try:
        with pytest.raises(PythNetworkError, match="stream disconnected"):
            tuple(hermes.stream_batches())
    finally:
        hermes.close()


def test_close_interrupts_active_sse_connection(tmp_path) -> None:
    response = BlockingResponse({})
    hermes = client(tmp_path, FakeSession([response]))
    iterator = hermes.stream_batches()
    finished = threading.Event()

    def consume() -> None:
        try:
            next(iterator)
        except StopIteration:
            pass
        finally:
            finished.set()

    thread = threading.Thread(target=consume)
    thread.start()
    try:
        for _ in range(100):
            if hermes._active_response is response:
                break
            time.sleep(0.001)
        hermes.close()
        assert finished.wait(1)
    finally:
        hermes.close()
        thread.join(timeout=1)
    assert not thread.is_alive()


def test_429_and_local_request_budget_are_bounded_and_redacted(tmp_path) -> None:
    rate_limited = FakeResponse({}, status_code=429, headers={"Retry-After": "17"})
    hermes = client(tmp_path, FakeSession([rate_limited]))
    try:
        with pytest.raises(PythRateLimitError) as caught:
            hermes.latest_batch()
        assert caught.value.retry_after_seconds == 17
        assert "very-secret-value" not in repr(caught.value)
    finally:
        hermes.close()

    session = FakeSession([FakeResponse(payload())])
    limited = client(tmp_path, session, pyth_request_budget_per_10_seconds=1)
    try:
        limited.latest_batch()
        with pytest.raises(PythRateLimitError) as caught:
            limited.latest_batch()
        assert len(session.calls) == 1
        assert 0 < caught.value.retry_after_seconds <= 10
    finally:
        limited.close()


def test_client_requires_external_key_file_repr_and_errors_redact_secret(tmp_path) -> None:
    with pytest.raises(PythCredentialError, match="not configured"):
        PythHermesClient(Settings())
    hermes = client(tmp_path, FakeSession([]))
    try:
        assert "very-secret-value" not in repr(hermes)
        assert not any(hasattr(hermes, name) for name in ("order", "cancel", "trade", "submit"))
    finally:
        hermes.close()


def test_client_rejects_key_path_inside_repository() -> None:
    with pytest.raises(PythCredentialError, match="outside the repository"):
        PythHermesClient(Settings(pyth_api_key_path=Path.cwd() / "key"))


def test_all_five_feed_ids_are_unique_and_full_length() -> None:
    assert set(PYTH_FEEDS) == {
        Asset.GOLD,
        Asset.SILVER,
        Asset.WTI_OIL,
        Asset.HYPE,
        Asset.BNB,
    }
    ids = [feed_id for _, feed_id in PYTH_FEEDS.values()]
    assert len(set(ids)) == 5
    assert all(len(feed_id) == 64 and int(feed_id, 16) >= 0 for feed_id in ids)
