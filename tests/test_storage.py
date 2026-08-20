from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from live15_quant.models import (
    Asset,
    ContractQuote,
    ExecutabilityClassification,
    FifteenMinuteContract,
    FreshnessState,
    LifecycleState,
    MappingConfidence,
    MarketTick,
    OrderBookLevel,
    PredictionMarketQuote,
    RecorderDiagnosticKind,
    RecorderEventSeverity,
    RecorderEventType,
    SourceTimestampKind,
    SupportLevel,
    Venue,
)
from live15_quant.records import SCHEMA_VERSION
from live15_quant.replay import ReplayReader
from live15_quant.settlement import SETTLEMENT_SPECS
from live15_quant.storage import RecorderStorageError, RecorderStore

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


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


def prediction_quote(
    received_at: datetime = datetime(2026, 8, 20, 12, 1, tzinfo=UTC),
    *,
    yes_bid: Decimal = Decimal("0.5100"),
) -> PredictionMarketQuote:
    return PredictionMarketQuote(
        asset=Asset.BTC,
        robinhood_event_id="event-1",
        robinhood_contract_id="contract-1",
        venue=Venue.KALSHI,
        venue_series="KXBTC15M",
        venue_ticker="KXBTC15M-26AUG201215-00",
        mapping_confidence=MappingConfidence.VERIFIED,
        source_timestamp=received_at - timedelta(milliseconds=25),
        source_timestamp_kind=SourceTimestampKind.HTTP_RESPONSE_DATE,
        received_timestamp=received_at,
        yes_bid=yes_bid,
        yes_ask=Decimal("0.5200"),
        no_bid=Decimal("0.4800"),
        no_ask=Decimal("0.4900"),
        last_trade=Decimal("0.5150"),
        volume=Decimal("1234.567890123456789"),
        yes_bid_depth=(OrderBookLevel(Decimal("0.5100"), Decimal("12.340000")),),
        no_bid_depth=(OrderBookLevel(Decimal("0.4800"), Decimal("8.900000")),),
        source="https://external-api.kalshi.com/trade-api/v2/markets/example",
        freshness=FreshnessState.FRESH,
        executability=ExecutabilityClassification.OFFICIAL_VENUE_ORDER_BOOK,
        evidence_urls=("https://docs.kalshi.com/getting_started/quick_start_market_data",),
    )


def test_first_create_append_and_full_decimal_precision(tmp_path) -> None:
    path = tmp_path / "history.sqlite3"

    with RecorderStore(path) as store:
        assert store.append_robinhood(contract()) is True
        assert store.append_coinbase(tick()) is True
        assert store.append_prediction_quote(prediction_quote()) is True
        assert store.count("robinhood_snapshots") == 1
        assert store.count("coinbase_ticks") == 1
        assert store.count("prediction_market_quotes") == 1
        assert store._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store._connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1000
        assert store._connection.execute("PRAGMA journal_size_limit").fetchone()[0] == 67108864
        indexes = {
            row[0]
            for row in store._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert {
            "idx_kalshi_market_followup",
            "idx_kalshi_settlement_asset_cursor",
            "idx_kalshi_native_quote_asset_cursor",
        } <= indexes
        snapshot = next(store.replay_robinhood("event-1"))
        saved_tick = next(store.replay_coinbase("BTC-USD"))
        saved_quote = next(store.replay_prediction_quotes("event-1"))

    assert path.exists()
    assert snapshot.target_price == Decimal("68159.82000001")
    assert saved_tick.price == Decimal("68159.1234567890123456789")
    assert saved_tick.last_size == Decimal("0.000000010000")
    assert saved_tick.bid_size == Decimal("1.000000000001")
    assert saved_quote.volume == Decimal("1234.567890123456789")
    assert saved_quote.yes_bid_depth[0].quantity == Decimal("12.340000")


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

    assert version == str(SCHEMA_VERSION)
    assert snapshot.schema_version == SCHEMA_VERSION
    assert saved_tick.schema_version == SCHEMA_VERSION
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


def test_v2_database_is_atomically_migrated_with_quote_stream(tmp_path) -> None:
    path = tmp_path / "history.sqlite3"
    with RecorderStore(path) as store:
        store.append_robinhood(contract())
        store.append_coinbase(tick())

    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE prediction_market_quotes")
    connection.execute("UPDATE recorder_metadata SET value = '2'")
    connection.execute("UPDATE robinhood_snapshots SET schema_version = 2")
    connection.execute("UPDATE coinbase_ticks SET schema_version = 2")
    connection.execute("UPDATE robinhood_diagnostics SET schema_version = 2")
    connection.commit()
    connection.close()

    with RecorderStore(path) as migrated:
        assert migrated.count("robinhood_snapshots") == 1
        assert migrated.count("coinbase_ticks") == 1
        assert migrated.count("prediction_market_quotes") == 0
        version = migrated._connection.execute(
            "SELECT value FROM recorder_metadata WHERE key = 'schema_version'"
        ).fetchone()[0]

    assert version == str(SCHEMA_VERSION)


def test_failed_v2_migration_rolls_back_version_and_quote_table(tmp_path) -> None:
    path = tmp_path / "history.sqlite3"
    with RecorderStore(path) as store:
        store.append_robinhood(contract())
        store.append_coinbase(tick())

    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE prediction_market_quotes")
    connection.execute("UPDATE recorder_metadata SET value = '2'")
    connection.execute("UPDATE robinhood_snapshots SET schema_version = 2")
    connection.execute("UPDATE coinbase_ticks SET schema_version = 1")
    connection.execute("UPDATE robinhood_diagnostics SET schema_version = 2")
    connection.commit()
    connection.close()

    with pytest.raises(RecorderStorageError, match="mixed schema"):
        RecorderStore(path)

    connection = sqlite3.connect(path)
    version = connection.execute(
        "SELECT value FROM recorder_metadata WHERE key = 'schema_version'"
    ).fetchone()[0]
    snapshot_version = connection.execute(
        "SELECT schema_version FROM robinhood_snapshots"
    ).fetchone()[0]
    quote_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'prediction_market_quotes'"
    ).fetchone()
    connection.close()

    assert version == "2"
    assert snapshot_version == 2
    assert quote_table is None


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


def test_prediction_quote_suppresses_only_consecutive_duplicate_state(tmp_path) -> None:
    with RecorderStore(tmp_path / "history.sqlite3") as store:
        first = prediction_quote()
        repeated = prediction_quote(datetime(2026, 8, 20, 12, 1, 1, tzinfo=UTC))
        changed = prediction_quote(
            datetime(2026, 8, 20, 12, 1, 2, tzinfo=UTC), yes_bid=Decimal("0.5000")
        )
        returned = prediction_quote(datetime(2026, 8, 20, 12, 1, 3, tzinfo=UTC))

        assert store.append_prediction_quote(first) is True
        assert store.append_prediction_quote(repeated) is False
        assert store.append_prediction_quote(changed) is True
        assert store.append_prediction_quote(returned) is True
        records = list(store.replay_prediction_quotes("event-1"))

    assert [item.yes_bid for item in records] == [
        Decimal("0.5100"),
        Decimal("0.5000"),
        Decimal("0.5100"),
    ]


def test_replay_reader_orders_official_quotes_by_receive_timestamp(tmp_path) -> None:
    path = tmp_path / "history.sqlite3"
    later = prediction_quote(datetime(2026, 8, 20, 12, 3, tzinfo=UTC))
    earlier = prediction_quote(datetime(2026, 8, 20, 12, 2, tzinfo=UTC), yes_bid=Decimal("0.5000"))
    with RecorderStore(path) as store:
        store.append_prediction_quote(later)
        store.append_prediction_quote(earlier)

    with ReplayReader(path) as reader:
        records = list(reader.quotes("event-1"))

    assert [item.received_timestamp for item in records] == [
        earlier.received_timestamp,
        later.received_timestamp,
    ]


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


def test_operational_events_are_bounded_filterable_and_deduplicated(tmp_path) -> None:
    with RecorderStore(tmp_path / "events.sqlite3") as store:
        for index in range(105):
            store.append_recorder_event(
                observed_timestamp=NOW + timedelta(seconds=index),
                severity=(
                    RecorderEventSeverity.ERROR if index % 2 else RecorderEventSeverity.WARNING
                ),
                event_type=RecorderEventType.SOURCE_UNAVAILABLE,
                asset=Asset.BTC if index % 3 else Asset.ETH,
                source="kalshi_quote:BTC",
                error_type="TimeoutError",
                message="Source temporarily unavailable; bounded retry scheduled",
                dedup_key=f"event-{index}",
                retain=100,
            )
        assert not store.append_recorder_event(
            observed_timestamp=NOW + timedelta(seconds=104),
            severity=RecorderEventSeverity.WARNING,
            event_type=RecorderEventType.SOURCE_UNAVAILABLE,
            message="Duplicate is ignored",
            dedup_key="event-104",
            retain=100,
        )
        assert store.count("recorder_events") == 100
        filtered = store.replay_recorder_events(
            limit=25,
            severity=RecorderEventSeverity.ERROR,
            asset=Asset.BTC,
            since=NOW + timedelta(seconds=50),
        )

    assert 0 < len(filtered) <= 25
    assert all(event.severity is RecorderEventSeverity.ERROR for event in filtered)
    assert all(event.asset is Asset.BTC for event in filtered)
    assert list(filtered) == sorted(
        filtered, key=lambda event: (event.observed_timestamp, event.row_id), reverse=True
    )


def test_v4_database_migrates_operational_events_atomically(tmp_path) -> None:
    path = tmp_path / "v4.sqlite3"
    with RecorderStore(path) as store:
        store.append_coinbase(tick())

    versioned_tables = (
        "robinhood_snapshots",
        "coinbase_ticks",
        "robinhood_diagnostics",
        "prediction_market_quotes",
        "kalshi_market_lifecycle",
        "kalshi_settlements",
        "kalshi_prediction_quotes",
        "kalshi_settlement_conflicts",
    )
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE recorder_events")
    connection.execute("UPDATE recorder_metadata SET value='4'")
    for table in versioned_tables:
        connection.execute(f"UPDATE {table} SET schema_version=4")
    connection.commit()
    connection.close()

    with RecorderStore(path) as migrated:
        assert migrated.count("coinbase_ticks") == 1
        assert migrated.count("recorder_events") == 0
        assert migrated.integrity_check() == "ok"
        assert migrated._connection.execute(
            "SELECT value FROM recorder_metadata WHERE key='schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)


def test_failed_v4_migration_rolls_back_version_and_event_table(tmp_path) -> None:
    path = tmp_path / "bad-v4.sqlite3"
    with RecorderStore(path) as store:
        store.append_coinbase(tick())
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE recorder_events")
    connection.execute("UPDATE recorder_metadata SET value='4'")
    for table in (
        "robinhood_snapshots",
        "coinbase_ticks",
        "robinhood_diagnostics",
        "prediction_market_quotes",
        "kalshi_market_lifecycle",
        "kalshi_settlements",
        "kalshi_prediction_quotes",
        "kalshi_settlement_conflicts",
    ):
        connection.execute(f"UPDATE {table} SET schema_version=4")
    connection.execute("UPDATE coinbase_ticks SET schema_version=3")
    connection.commit()
    connection.close()

    with pytest.raises(RecorderStorageError, match="mixed schema"):
        RecorderStore(path)
    connection = sqlite3.connect(path)
    assert (
        connection.execute(
            "SELECT value FROM recorder_metadata WHERE key='schema_version'"
        ).fetchone()[0]
        == "4"
    )
    assert (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recorder_events'"
        ).fetchone()
        is None
    )
    connection.close()


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


def test_malformed_prediction_quote_fails_explicitly_during_replay(tmp_path) -> None:
    path = tmp_path / "history.sqlite3"
    with RecorderStore(path) as store:
        store.append_prediction_quote(prediction_quote())

    connection = sqlite3.connect(path)
    connection.execute("UPDATE prediction_market_quotes SET mapping_confidence = 'partial'")
    connection.commit()
    connection.close()

    with RecorderStore(path) as store, pytest.raises(RecorderStorageError, match="malformed"):
        next(store.replay_prediction_quotes("event-1"))


def test_unknown_count_table_is_rejected(tmp_path) -> None:
    with RecorderStore(tmp_path / "history.sqlite3") as store:
        with pytest.raises(ValueError, match="unknown"):
            store.count("sqlite_master")
