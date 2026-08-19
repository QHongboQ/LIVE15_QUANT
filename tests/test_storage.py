from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from live15_quant.models import (
    Asset,
    ContractQuote,
    FifteenMinuteContract,
    FreshnessState,
    LifecycleState,
    MarketTick,
    RecorderDiagnosticKind,
    SupportLevel,
)
from live15_quant.replay import ReplayReader
from live15_quant.settlement import SETTLEMENT_SPECS
from live15_quant.storage import RecorderStorageError, RecorderStore


def contract(
    fetched_at: datetime = datetime(2026, 8, 20, 12, 1, tzinfo=UTC),
    *,
    yes: Decimal | None = Decimal("0.5100"),
    lifecycle: LifecycleState = LifecycleState.LIVE,
) -> FifteenMinuteContract:
    return FifteenMinuteContract(
        asset=Asset.BTC,
        event_id="event-1",
        contract_id="contract-1",
        start_time=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        end_time=datetime(2026, 8, 20, 12, 15, tzinfo=UTC),
        target_price=Decimal("68159.82000001"),
        quote=ContractQuote(
            yes_probability=yes,
            no_probability=None,
            availability=SupportLevel.PARTIAL if yes is not None else SupportLevel.UNSUPPORTED,
        ),
        venue=None,
        venue_candidates=(),
        settlement=SETTLEMENT_SPECS[Asset.BTC],
        lifecycle_state=lifecycle,
        source_url="https://robinhood.com/us/en/prediction-markets/15-min/",
        fetched_at=fetched_at,
        freshness_state=FreshnessState.FRESH,
        source_age_seconds=1,
    )


def tick(received_at: datetime = datetime(2026, 8, 20, 12, 1, tzinfo=UTC)) -> MarketTick:
    return MarketTick(
        symbol="BTC-USD",
        price=Decimal("68159.1234567890123456789"),
        bid=Decimal("68159.1234567890123456788"),
        ask=Decimal("68159.1234567890123456790"),
        exchange_time=received_at - timedelta(microseconds=12),
        received_at=received_at,
        bid_size=Decimal("1.000000000001"),
        ask_size=Decimal("2.000000000002"),
        last_size=Decimal("0.000000010000"),
        volume_24h=Decimal("12345.67890123456789"),
    )


def test_first_create_append_and_full_decimal_precision(tmp_path) -> None:
    path = tmp_path / "history.sqlite3"

    with RecorderStore(path) as store:
        assert store.append_robinhood(contract()) is True
        assert store.append_coinbase(tick()) is True
        assert store.count("robinhood_snapshots") == 1
        assert store.count("coinbase_ticks") == 1
        assert store._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store._connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1000
        assert store._connection.execute("PRAGMA journal_size_limit").fetchone()[0] == 67108864
        snapshot = next(store.replay_robinhood("event-1"))
        saved_tick = next(store.replay_coinbase("BTC-USD"))

    assert path.exists()
    assert snapshot.target_price == Decimal("68159.82000001")
    assert saved_tick.price == Decimal("68159.1234567890123456789")
    assert saved_tick.last_size == Decimal("0.000000010000")
    assert saved_tick.bid_size == Decimal("1.000000000001")


def test_restart_continues_without_overwriting_history(tmp_path) -> None:
    path = tmp_path / "history.sqlite3"
    with RecorderStore(path) as store:
        store.append_robinhood(contract())
    with RecorderStore(path) as store:
        store.append_robinhood(contract(datetime(2026, 8, 20, 12, 2, tzinfo=UTC)))
        records = list(store.replay_robinhood("event-1"))

    assert len(records) == 2
    assert [record.row_id for record in records] == [1, 2]


def test_v1_database_is_atomically_migrated_without_losing_history(tmp_path) -> None:
    path = tmp_path / "history.sqlite3"
    with RecorderStore(path) as store:
        store.append_robinhood(contract())
        store.append_coinbase(tick())

    connection = sqlite3.connect(path)
    connection.execute("UPDATE recorder_metadata SET value = '1'")
    connection.execute("UPDATE robinhood_snapshots SET schema_version = 1")
    connection.execute("UPDATE coinbase_ticks SET schema_version = 1")
    connection.execute("DROP TABLE robinhood_diagnostics")
    connection.commit()
    connection.close()

    with RecorderStore(path) as migrated:
        snapshot = next(migrated.replay_robinhood("event-1"))
        saved_tick = next(migrated.replay_coinbase("BTC-USD"))
        version = migrated._connection.execute(
            "SELECT value FROM recorder_metadata WHERE key = 'schema_version'"
        ).fetchone()[0]

    assert version == "2"
    assert snapshot.schema_version == 2
    assert saved_tick.schema_version == 2
    assert snapshot.target_price == Decimal("68159.82000001")
    assert saved_tick.price == Decimal("68159.1234567890123456789")


def test_failed_v1_migration_rolls_back_all_schema_changes(tmp_path) -> None:
    path = tmp_path / "history.sqlite3"
    with RecorderStore(path) as store:
        store.append_robinhood(contract())

    connection = sqlite3.connect(path)
    connection.execute("UPDATE recorder_metadata SET value = '1'")
    connection.execute("DROP TABLE robinhood_diagnostics")
    connection.commit()
    connection.close()

    with pytest.raises(RecorderStorageError, match="mixed schema"):
        RecorderStore(path)

    connection = sqlite3.connect(path)
    version = connection.execute("SELECT value FROM recorder_metadata").fetchone()[0]
    diagnostic_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'robinhood_diagnostics'"
    ).fetchone()
    connection.close()

    assert version == "1"
    assert diagnostic_table is None


def test_future_schema_is_rejected_before_recorder_tables_are_created(tmp_path) -> None:
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE recorder_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
    )
    connection.execute("INSERT INTO recorder_metadata(key, value) VALUES ('schema_version', '99')")
    connection.commit()
    connection.close()

    with pytest.raises(RecorderStorageError, match="incompatible"):
        RecorderStore(path)

    connection = sqlite3.connect(path)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    connection.close()

    assert tables == {"recorder_metadata"}


def test_exact_duplicate_is_ignored_but_price_change_is_retained(tmp_path) -> None:
    with RecorderStore(tmp_path / "history.sqlite3") as store:
        first = contract()
        assert store.append_robinhood(first) is True
        assert store.append_robinhood(first) is False
        changed = replace(
            first,
            quote=ContractQuote(
                yes_probability=Decimal("0.5200"),
                no_probability=None,
                availability=SupportLevel.PARTIAL,
            ),
        )
        assert store.append_robinhood(changed) is True
        records = list(store.replay_robinhood("event-1"))

    assert [record.displayed_yes for record in records] == [Decimal("0.5100"), Decimal("0.5200")]


def test_lifecycle_diagnostics_are_deduplicated_by_logical_event(tmp_path) -> None:
    with RecorderStore(tmp_path / "history.sqlite3") as store:
        first = store.append_robinhood_diagnostic(
            kind=RecorderDiagnosticKind.POST_END_EVENT_RETURNED,
            asset=Asset.BTC,
            event_id="event-1",
            contract_id="contract-1",
            observed_at=datetime(2026, 8, 20, 12, 16, tzinfo=UTC),
            event_end_time=datetime(2026, 8, 20, 12, 15, tzinfo=UTC),
            source_url="https://robinhood.com/us/en/prediction-markets/15-min/",
        )
        repeated = store.append_robinhood_diagnostic(
            kind=RecorderDiagnosticKind.POST_END_EVENT_RETURNED,
            asset=Asset.BTC,
            event_id="event-1",
            contract_id="contract-1",
            observed_at=datetime(2026, 8, 20, 12, 17, tzinfo=UTC),
            event_end_time=datetime(2026, 8, 20, 12, 15, tzinfo=UTC),
            source_url="https://robinhood.com/us/en/prediction-markets/15-min/",
        )

        assert first is True
        assert repeated is False
        assert store.count("robinhood_diagnostics") == 1


def test_storage_rejects_post_end_active_observation(tmp_path) -> None:
    with RecorderStore(tmp_path / "history.sqlite3") as store:
        with pytest.raises(ValueError, match="post-end"):
            store.append_robinhood(contract(datetime(2026, 8, 20, 12, 15, tzinfo=UTC)))


def test_replay_orders_timestamp_then_insertion_id(tmp_path) -> None:
    with RecorderStore(tmp_path / "history.sqlite3") as store:
        later = contract(datetime(2026, 8, 20, 12, 3, tzinfo=UTC))
        earlier = contract(datetime(2026, 8, 20, 12, 2, tzinfo=UTC))
        store.append_robinhood(later)
        store.append_robinhood(earlier)
        records = list(store.replay_robinhood("event-1"))

    assert [record.fetched_timestamp for record in records] == [
        earlier.fetched_at,
        later.fetched_at,
    ]


def test_replay_reader_orders_coinbase_by_local_receive_timestamp(tmp_path) -> None:
    path = tmp_path / "history.sqlite3"
    later = tick(datetime(2026, 8, 20, 12, 3, tzinfo=UTC))
    earlier = tick(datetime(2026, 8, 20, 12, 2, tzinfo=UTC))
    with RecorderStore(path) as store:
        store.append_coinbase(later)
        store.append_coinbase(earlier)

    with ReplayReader(path) as reader:
        records = list(reader.coinbase("BTC-USD"))

    assert [record.received_timestamp for record in records] == [
        earlier.received_at,
        later.received_at,
    ]
    assert records[0].exchange_timestamp == earlier.exchange_time


def test_replay_excludes_legacy_post_end_active_observations(tmp_path) -> None:
    path = tmp_path / "history.sqlite3"
    with RecorderStore(path) as store:
        before_end = contract(datetime(2026, 8, 20, 12, 14, tzinfo=UTC))
        store.append_robinhood(before_end)
        store._connection.execute(
            "UPDATE robinhood_snapshots SET fetched_timestamp = ? WHERE event_id = ?",
            (datetime(2026, 8, 20, 12, 16, tzinfo=UTC).isoformat(), before_end.event_id),
        )
        store._connection.commit()

    with ReplayReader(path) as reader:
        assert list(reader.event(before_end.event_id)) == []


def test_malformed_record_fails_explicitly_during_replay(tmp_path) -> None:
    path = tmp_path / "history.sqlite3"
    with RecorderStore(path) as store:
        store.append_coinbase(tick())

    connection = sqlite3.connect(path)
    connection.execute("UPDATE coinbase_ticks SET price = 'not-a-decimal'")
    connection.commit()
    connection.close()

    with RecorderStore(path) as store, pytest.raises(RecorderStorageError, match="malformed"):
        next(store.replay_coinbase("BTC-USD"))


def test_unknown_count_table_is_rejected(tmp_path) -> None:
    with RecorderStore(tmp_path / "history.sqlite3") as store:
        with pytest.raises(ValueError, match="unknown"):
            store.count("sqlite_master")
