"""Bounded, read-only projections for the local Control Center."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from live15_quant.dataset import DATASET_VERSION
from live15_quant.feature_registry import FEATURE_SCHEMA_VERSION
from live15_quant.features import (
    COINBASE_PRODUCT_BY_ASSET,
    FeatureEngine,
    FeatureInputs,
    SamplingPolicy,
)
from live15_quant.market_sessions import market_data_state, market_session
from live15_quant.models import Asset, UnderlyingProvider
from live15_quant.providers.pyth import PYTH_FEEDS
from live15_quant.storage import RecorderStorageError, RecorderStore


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("dashboard timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _decimal(value: object) -> str | None:
    return None if value is None else str(value)


def _json(value: object, default: object) -> object:
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class DashboardReadStore:
    """Open SQLite in mode=ro and issue only indexed, bounded queries."""

    def __init__(
        self,
        raw_path: Path,
        feature_path: Path,
        *,
        current_trainable_path: Path | None = None,
        coinbase_stale_seconds: float = 30.0,
        pyth_stale_seconds: float = 15.0,
        secondary_stale_seconds: float = 10.0,
    ) -> None:
        self.raw_path = raw_path
        self.feature_path = feature_path
        self.current_trainable_path = current_trainable_path
        self.coinbase_stale_seconds = coinbase_stale_seconds
        self.pyth_stale_seconds = pyth_stale_seconds
        self.secondary_stale_seconds = secondary_stale_seconds

    @staticmethod
    def _open(path: Path) -> sqlite3.Connection | None:
        try:
            exists = path.is_file()
        except OSError:
            return None
        if not exists:
            return None
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=2
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=2000")
            return connection
        except (OSError, sqlite3.Error):
            if connection is not None:
                connection.close()
            return None

    def asset(
        self,
        asset: Asset,
        now: datetime,
        current_ticker: str | None = None,
        *,
        allow_ws: bool = True,
    ) -> dict[str, Any]:
        """Return the explicit detail projection, including bounded feature computation."""

        connection = self._open(self.raw_path)
        if connection is None:
            return self._missing_asset(asset, "raw_store_unavailable")
        try:
            quote_columns = self._quote_columns(connection, include_detail=True)
            return self._asset(
                connection,
                asset,
                now,
                current_ticker,
                include_features=True,
                include_detail=True,
                allow_ws=allow_ws,
                quote_columns=quote_columns,
            )
        finally:
            connection.close()

    def realtime_asset(
        self,
        asset: Asset,
        now: datetime,
        current_ticker: str | None = None,
        *,
        allow_ws: bool = True,
    ) -> dict[str, Any]:
        """Return detail data without re-running feature research on every stream update."""

        connection = self._open(self.raw_path)
        if connection is None:
            return self._missing_asset(asset, "raw_store_unavailable")
        try:
            quote_columns = self._quote_columns(connection, include_detail=True)
            return self._asset(
                connection,
                asset,
                now,
                current_ticker,
                include_features=False,
                include_detail=True,
                allow_ws=allow_ws,
                quote_columns=quote_columns,
            )
        finally:
            connection.close()

    def realtime_cursor(self) -> tuple[object, ...]:
        """Return cheap indexed cursors for all facts consumed by terminal subscriptions."""

        connection = self._open(self.raw_path)
        if connection is None:
            return ("unavailable",)
        try:
            cursors: list[object] = []
            for table in (
                "kalshi_ws_orderbook_events",
                "coinbase_ticks",
                "underlying_observations",
                "secondary_underlying_observations",
                "kalshi_market_lifecycle",
            ):
                if not self._table_exists(connection, table):
                    cursors.append(None)
                    continue
                row = connection.execute(
                    f"SELECT id FROM {table} ORDER BY id DESC LIMIT 1"
                ).fetchone()
                cursors.append(None if row is None else int(row["id"]))
            return tuple(cursors)
        finally:
            connection.close()

    def realtime_asset_cursor(self, asset: Asset, ticker: str | None) -> tuple[object, ...]:
        """Return only cursors capable of changing one market-detail projection."""

        connection = self._open(self.raw_path)
        if connection is None:
            return ("unavailable",)
        try:
            book = (
                connection.execute(
                    "SELECT sequence FROM kalshi_ws_current_books WHERE ticker=?",
                    (ticker,),
                ).fetchone()
                if ticker and self._table_exists(connection, "kalshi_ws_current_books")
                else None
            )
            product = COINBASE_PRODUCT_BY_ASSET.get(asset)
            if product is not None:
                underlying = connection.execute(
                    """SELECT id FROM coinbase_ticks WHERE product=?
                    ORDER BY received_timestamp DESC, id DESC LIMIT 1""",
                    (product,),
                ).fetchone()
            else:
                underlying = connection.execute(
                    """SELECT id FROM underlying_observations
                    WHERE asset=? AND provider=? ORDER BY id DESC LIMIT 1""",
                    (asset.value, UnderlyingProvider.PYTH_HERMES.value),
                ).fetchone()
            secondary = connection.execute(
                """SELECT id FROM secondary_underlying_observations
                WHERE asset=? ORDER BY id DESC LIMIT 1""",
                (asset.value,),
            ).fetchone()
            lifecycle = connection.execute(
                """SELECT id FROM kalshi_market_lifecycle
                WHERE asset=? ORDER BY id DESC LIMIT 1""",
                (asset.value,),
            ).fetchone()
            return (
                None if book is None else int(book["sequence"]),
                None if underlying is None else int(underlying["id"]),
                None if secondary is None else int(secondary["id"]),
                None if lifecycle is None else int(lifecycle["id"]),
            )
        finally:
            connection.close()

    def summaries(
        self,
        now: datetime,
        current_markets: dict[str, str | None],
        synchronized_markets: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return lightweight market cards using one read-only connection per request.

        This intentionally excludes FeatureEngine work: it is a list projection, not a
        decision/readiness projection.  Detail requests continue to own that bounded work.
        """

        connection = self._open(self.raw_path)
        if connection is None:
            return [self._missing_asset(asset, "raw_store_unavailable") for asset in Asset]
        try:
            quote_columns = self._quote_columns(connection, include_detail=False)
            return [
                self._asset(
                    connection,
                    asset,
                    now,
                    current_markets.get(asset.value),
                    include_features=False,
                    include_detail=False,
                    allow_ws=(
                        synchronized_markets is None
                        or synchronized_markets.get(asset.value) == current_markets.get(asset.value)
                    ),
                    quote_columns=quote_columns,
                )
                for asset in Asset
            ]
        finally:
            connection.close()

    def _asset(
        self,
        connection: sqlite3.Connection,
        asset: Asset,
        now: datetime,
        current_ticker: str | None,
        *,
        include_features: bool,
        include_detail: bool,
        allow_ws: bool,
        quote_columns: str,
    ) -> dict[str, Any]:
        try:
            market = self._market_row(connection, asset, now, current_ticker)
            if market is None:
                return self._missing_asset(asset, "market_missing")
            ticker = str(market["ticker"])
            rest_quote = connection.execute(
                f"""SELECT {quote_columns} FROM kalshi_prediction_quotes WHERE ticker=?
                ORDER BY received_timestamp DESC, id DESC LIMIT 1""",
                (ticker,),
            ).fetchone()
            ws_book = self._current_ws_book(connection, ticker) if allow_ws else None
            quote = ws_book if ws_book is not None else rest_quote
            product = COINBASE_PRODUCT_BY_ASSET.get(asset)
            tick = None
            if product is not None:
                tick = connection.execute(
                    """SELECT * FROM coinbase_ticks WHERE product=?
                    ORDER BY received_timestamp DESC, id DESC LIMIT 1""",
                    (product,),
                ).fetchone()
            elif self._table_exists(connection, "underlying_observations"):
                tick = connection.execute(
                    """SELECT * FROM underlying_observations
                    WHERE asset=? AND provider=?
                    ORDER BY received_timestamp DESC,id DESC LIMIT 1""",
                    (asset.value, UnderlyingProvider.PYTH_HERMES.value),
                ).fetchone()
            secondary_provider = {
                Asset.BNB: UnderlyingProvider.BINANCE_SPOT,
                Asset.HYPE: UnderlyingProvider.HYPERLIQUID_PERP,
            }.get(asset)
            secondary = None
            if (
                include_detail
                and secondary_provider is not None
                and self._table_exists(connection, "secondary_underlying_observations")
            ):
                secondary = connection.execute(
                    """SELECT * FROM secondary_underlying_observations
                    WHERE asset=? AND provider=?
                    ORDER BY received_timestamp DESC,id DESC LIMIT 1""",
                    (asset.value, secondary_provider.value),
                ).fetchone()
            end = datetime.fromisoformat(str(market["window_end"])).astimezone(UTC)
            quote_received = (
                datetime.fromisoformat(
                    str(
                        ws_book["socket_received_timestamp"]
                        if ws_book is not None
                        else quote["received_timestamp"]
                    )
                ).astimezone(UTC)
                if quote is not None
                else None
            )
            tick_received = (
                datetime.fromisoformat(str(tick["received_timestamp"])).astimezone(UTC)
                if tick is not None
                else None
            )
            quote_age = (
                None if quote_received is None else max(0, (now - quote_received).total_seconds())
            )
            tick_age = (
                None if tick_received is None else max(0, (now - tick_received).total_seconds())
            )
            secondary_received = (
                datetime.fromisoformat(str(secondary["received_timestamp"])).astimezone(UTC)
                if secondary is not None
                else None
            )
            secondary_age = (
                None
                if secondary_received is None
                else max(0, (now - secondary_received).total_seconds())
            )
            ws_yes = _json(ws_book["yes_bids"], []) if ws_book is not None else []
            ws_no = _json(ws_book["no_bids"], []) if ws_book is not None else []
            ws_yes_bid = str(ws_yes[0][0]) if ws_yes else None
            ws_no_bid = str(ws_no[0][0]) if ws_no else None
            yes_bid = (
                ws_yes_bid
                if ws_book is not None
                else _decimal(quote["yes_bid"])
                if quote is not None
                else None
            )
            yes_ask = (
                str(Decimal(1) - Decimal(ws_no_bid))
                if ws_book is not None and ws_no_bid is not None
                else _decimal(quote["yes_ask"])
                if quote is not None
                else None
            )
            no_bid = (
                ws_no_bid
                if ws_book is not None
                else _decimal(quote["no_bid"])
                if quote is not None
                else None
            )
            no_ask = (
                str(Decimal(1) - Decimal(ws_yes_bid))
                if ws_book is not None and ws_yes_bid is not None
                else self._rest_no_ask(rest_quote)
                if rest_quote is not None
                else None
            )
            spread = None
            if yes_bid is not None and yes_ask is not None:
                spread = str(Decimal(yes_ask) - Decimal(yes_bid))
            underlying_status = self._age_status(
                tick_age,
                self.coinbase_stale_seconds if product is not None else self.pyth_stale_seconds,
            )
            if product is None and market_session(asset) is not None:
                session_status = market_data_state(
                    asset,
                    checked_at=now,
                    latest_received=tick_received,
                    max_age=timedelta(seconds=self.pyth_stale_seconds),
                )
                underlying_status = session_status.value
                if (
                    session_status.value == "healthy"
                    and tick is not None
                    and str(tick["freshness"]) == "stale"
                ):
                    underlying_status = "stale"
            elif product is None and tick is not None:
                recorded_freshness = str(tick["freshness"])
                if recorded_freshness == "stale":
                    underlying_status = "stale"
                elif recorded_freshness != "fresh":
                    underlying_status = "unavailable"
            secondary_status = self._age_status(secondary_age, self.secondary_stale_seconds)
            secondary_clock_skew = bool(
                secondary is not None and Decimal(str(secondary["source_receive_latency_ms"])) < 0
            )
            if secondary is not None and str(secondary["freshness"]) == "stale":
                secondary_status = "stale"
            primary_price = _decimal(tick["price"]) if tick is not None else None
            secondary_price = _decimal(secondary["price"]) if secondary is not None else None
            price_diff = (
                str(Decimal(secondary_price) - Decimal(primary_price))
                if secondary_price is not None and primary_price is not None
                else None
            )
            return {
                "asset": asset.value,
                "ticker": ticker,
                "series": str(market["series"]),
                "target": str(market["target"]),
                "window_start": str(market["window_start"]),
                "window_end": str(market["window_end"]),
                "seconds_remaining": max(0, (end - now).total_seconds()),
                "lifecycle": str(market["lifecycle"]),
                "official_status": str(market["official_status"]),
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "no_bid": no_bid,
                "no_ask": no_ask,
                "last_trade": (
                    _decimal(rest_quote["last_trade"]) if rest_quote is not None else None
                ),
                "spread": spread,
                "quote_age_seconds": quote_age,
                "quote_status": self._age_status(quote_age, 15),
                "quote_source": (
                    "kalshi_ws_synchronized"
                    if ws_book is not None
                    else "kalshi_rest_recovery"
                    if rest_quote is not None
                    else "unavailable"
                ),
                "book_verification_state": (
                    "synchronized" if ws_book is not None else "rest_fallback"
                ),
                "book_sequence": int(ws_book["sequence"]) if ws_book is not None else None,
                "book_connection_id": (
                    str(ws_book["connection_id"]) if ws_book is not None else None
                ),
                "quote_source_timestamp": (
                    _decimal(quote["source_timestamp"]) if quote is not None else None
                ),
                "quote_received_timestamp": (
                    str(
                        ws_book["socket_received_timestamp"]
                        if ws_book is not None
                        else quote["received_timestamp"]
                    )
                    if quote is not None
                    else None
                ),
                "socket_event_age_seconds": quote_age if ws_book is not None else None,
                # Event delivery age is not evidence that a displayed market value changed.
                # The detail history contract derives last-change time from the immutable facts.
                "last_market_change_age_seconds": None,
                "source_transport_latency_ms": (
                    str(
                        max(
                            0,
                            (
                                datetime.fromisoformat(str(ws_book["persisted_timestamp"]))
                                - datetime.fromisoformat(str(ws_book["socket_received_timestamp"]))
                            ).total_seconds()
                            * 1000,
                        )
                    )
                    if ws_book is not None
                    else None
                ),
                "projection_available_timestamp": (
                    str(ws_book["persisted_timestamp"]) if ws_book is not None else None
                ),
                "orderbook_status": (
                    "synchronized"
                    if ws_book is not None
                    else "rest_fallback"
                    if rest_quote is not None
                    else "missing"
                ),
                "yes_bid_depth": (
                    ws_yes
                    if ws_book is not None and include_detail
                    else _json(quote["yes_bid_depth"], [])
                    if quote is not None and include_detail
                    else []
                ),
                "no_bid_depth": (
                    ws_no
                    if ws_book is not None and include_detail
                    else _json(quote["no_bid_depth"], [])
                    if quote is not None and include_detail
                    else []
                ),
                "underlying_provider": (
                    UnderlyingProvider.COINBASE.value
                    if product is not None
                    else UnderlyingProvider.PYTH_HERMES.value
                ),
                "underlying_product": product or PYTH_FEEDS[asset][0],
                "underlying_price": primary_price,
                "underlying_age_seconds": tick_age,
                "underlying_status": underlying_status,
                "underlying_received_timestamp": (
                    str(tick["received_timestamp"]) if tick is not None else None
                ),
                "underlying_persisted_timestamp": (
                    str(tick["persisted_timestamp"])
                    if tick is not None and "persisted_timestamp" in tick.keys()
                    else None
                ),
                "primary_provider": (
                    UnderlyingProvider.COINBASE.value
                    if product is not None
                    else UnderlyingProvider.PYTH_HERMES.value
                ),
                "primary_age_seconds": tick_age,
                "secondary_provider": (
                    secondary_provider.value if secondary_provider is not None else None
                ),
                "secondary_instrument": (
                    str(secondary["instrument"]) if secondary is not None else None
                ),
                "secondary_price": secondary_price,
                "secondary_bid": _decimal(secondary["bid"]) if secondary is not None else None,
                "secondary_ask": _decimal(secondary["ask"]) if secondary is not None else None,
                "secondary_price_semantics": (
                    str(secondary["price_semantics"]) if secondary is not None else None
                ),
                "secondary_age_seconds": secondary_age,
                "secondary_status": secondary_status,
                "secondary_clock_skew": secondary_clock_skew,
                "secondary_source_timestamp": (
                    str(secondary["source_timestamp"]) if secondary is not None else None
                ),
                "secondary_received_timestamp": (
                    str(secondary["received_timestamp"]) if secondary is not None else None
                ),
                "secondary_persisted_timestamp": (
                    str(secondary["persisted_timestamp"])
                    if secondary is not None and secondary["persisted_timestamp"] is not None
                    else None
                ),
                "secondary_source_receive_latency_ms": (
                    _decimal(secondary["source_receive_latency_ms"])
                    if secondary is not None
                    else None
                ),
                "secondary_receive_persist_latency_ms": (
                    _decimal(secondary["receive_persist_latency_ms"])
                    if secondary is not None
                    else None
                ),
                "primary_secondary_price_diff": price_diff,
                "primary_secondary_age_diff": (
                    secondary_age - tick_age
                    if secondary_age is not None and tick_age is not None
                    else None
                ),
                "settlement_followup": "pending" if now >= end else "not_due",
                "features": self._features(connection, market, now) if include_features else {},
                "availability": "available",
            }
        except (sqlite3.Error, ValueError, RecorderStorageError):
            return self._missing_asset(asset, "store_error")

    @staticmethod
    def _quote_columns(connection: sqlite3.Connection, *, include_detail: bool) -> str:
        if include_detail:
            return "*"
        fields = [
            "yes_bid",
            "yes_ask",
            "no_bid",
            "last_trade",
            "source_timestamp",
            "received_timestamp",
        ]
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info('kalshi_prediction_quotes')")
        }
        if "no_ask" in columns:
            fields.insert(3, "no_ask")
        return ",".join(fields)

    @staticmethod
    def _rest_no_ask(quote: sqlite3.Row) -> str | None:
        if "no_ask" in quote.keys():
            return _decimal(quote["no_ask"])
        yes_bid = _decimal(quote["yes_bid"])
        return None if yes_bid is None else str(Decimal(1) - Decimal(yes_bid))

    @staticmethod
    def _current_ws_book(connection: sqlite3.Connection, ticker: str) -> sqlite3.Row | None:
        if not DashboardReadStore._table_exists(connection, "kalshi_ws_current_books"):
            return None
        current_session = connection.execute(
            """SELECT connection_id,subscription_id FROM kalshi_ws_current_books
            ORDER BY persisted_timestamp DESC LIMIT 1"""
        ).fetchone()
        if current_session is None:
            return None
        return connection.execute(
            """SELECT * FROM kalshi_ws_current_books
            WHERE ticker=? AND connection_id=? AND subscription_id=? LIMIT 1""",
            (ticker, current_session["connection_id"], current_session["subscription_id"]),
        ).fetchone()

    @staticmethod
    def _market_row(
        connection: sqlite3.Connection,
        asset: Asset,
        now: datetime,
        current_ticker: str | None,
    ) -> sqlite3.Row | None:
        if current_ticker:
            return connection.execute(
                """SELECT * FROM kalshi_market_lifecycle WHERE ticker=? AND asset=?
                ORDER BY fetched_timestamp DESC, id DESC LIMIT 1""",
                (current_ticker, asset.value),
            ).fetchone()
        return connection.execute(
            """SELECT * FROM kalshi_market_lifecycle
            WHERE asset=? AND window_start<=? AND window_end>?
            AND lifecycle IN ('open','paused','upcoming')
            ORDER BY fetched_timestamp DESC, id DESC LIMIT 1""",
            (asset.value, _timestamp(now), _timestamp(now)),
        ).fetchone()

    @staticmethod
    def _age_status(age: float | None, stale_after: float) -> str:
        if age is None:
            return "missing"
        return "stale" if age > stale_after else "healthy"

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def _missing_asset(asset: Asset, reason: str) -> dict[str, Any]:
        return {
            "asset": asset.value,
            "ticker": None,
            "lifecycle": "unavailable",
            "availability": reason,
            "quote_status": "missing",
            "quote_source": "unavailable",
            "book_verification_state": "unavailable",
            "orderbook_status": "missing",
            "underlying_provider": (
                UnderlyingProvider.COINBASE.value
                if asset in COINBASE_PRODUCT_BY_ASSET
                else UnderlyingProvider.PYTH_HERMES.value
            ),
            "underlying_status": "missing",
            "primary_provider": (
                UnderlyingProvider.COINBASE.value
                if asset in COINBASE_PRODUCT_BY_ASSET
                else UnderlyingProvider.PYTH_HERMES.value
            ),
            "secondary_provider": (
                UnderlyingProvider.BINANCE_SPOT.value
                if asset is Asset.BNB
                else UnderlyingProvider.HYPERLIQUID_PERP.value
                if asset is Asset.HYPE
                else None
            ),
            "secondary_status": "missing",
            "secondary_clock_skew": False,
            "settlement_followup": "unavailable",
            "features": {},
        }

    @staticmethod
    def _features(
        connection: sqlite3.Connection, market_row: sqlite3.Row, now: datetime
    ) -> dict[str, dict[str, str | None]]:
        try:
            decision = min(
                now,
                datetime.fromisoformat(str(market_row["window_end"])).astimezone(UTC)
                - timedelta(microseconds=1),
            )
            market = RecorderStore._kalshi_feature_market_record(market_row)
            quote_rows = connection.execute(
                """SELECT * FROM kalshi_prediction_quotes
                WHERE ticker=? AND received_timestamp<=? AND received_timestamp>=?
                ORDER BY received_timestamp ASC, id ASC LIMIT 1000""",
                (market.ticker, _timestamp(decision), _timestamp(decision - timedelta(minutes=6))),
            )
            product = COINBASE_PRODUCT_BY_ASSET.get(market.asset)
            tick_rows: tuple[sqlite3.Row, ...] = ()
            underlying_rows: tuple[sqlite3.Row, ...] = ()
            if product is not None:
                tick_rows = tuple(
                    connection.execute(
                        """SELECT * FROM coinbase_ticks
                        WHERE product=? AND received_timestamp<=? AND received_timestamp>=?
                        ORDER BY received_timestamp ASC, id ASC LIMIT 5000""",
                        (
                            product,
                            _timestamp(decision),
                            _timestamp(decision - timedelta(minutes=6)),
                        ),
                    )
                )
            elif DashboardReadStore._table_exists(connection, "underlying_observations"):
                underlying_rows = tuple(
                    connection.execute(
                        """SELECT * FROM underlying_observations
                        WHERE asset=? AND provider=? AND received_timestamp<=?
                          AND received_timestamp>=?
                        ORDER BY received_timestamp ASC,id ASC LIMIT 5000""",
                        (
                            market.asset.value,
                            UnderlyingProvider.PYTH_HERMES.value,
                            _timestamp(decision),
                            _timestamp(decision - timedelta(minutes=6)),
                        ),
                    )
                )
            inputs = FeatureInputs(
                market=market,
                quotes=tuple(RecorderStore._kalshi_native_quote_record(row) for row in quote_rows),
                ticks=tuple(RecorderStore._tick_record(row) for row in tick_rows),
                decision_timestamp=decision,
                underlying=tuple(RecorderStore._underlying_record(row) for row in underlying_rows),
            )
            policy = SamplingPolicy((timedelta(seconds=60),))
            vector = FeatureEngine(policy).compute(inputs)
            wanted = {
                "return_60s",
                "return_300s",
                "realized_volatility_60s",
                "realized_volatility_300s",
                "signed_distance_to_target",
                "normalized_distance_to_target",
                "top_depth_imbalance",
                "yes_cumulative_depth",
                "no_cumulative_depth",
            }
            return {
                item.name: {
                    "value": None if item.value is None else str(item.value),
                    "status": (
                        "available" if item.missing_reason is None else item.missing_reason.value
                    ),
                }
                for item in vector.observations
                if item.name in wanted
            }
        except (sqlite3.Error, ValueError, RecorderStorageError):
            return {}

    def previous_events(self, asset: Asset, limit: int = 8) -> list[dict[str, Any]]:
        connection = self._open(self.raw_path)
        if connection is None:
            return []
        try:
            rows = connection.execute(
                """SELECT ticker,target,result,settlement_timestamp,window_end
                FROM kalshi_settlements WHERE asset=?
                ORDER BY settlement_timestamp DESC,id DESC LIMIT ?""",
                (asset.value, min(max(limit, 1), 25)),
            )
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []
        finally:
            connection.close()

    def market_history(
        self, asset: Asset, now: datetime, current_ticker: str | None
    ) -> dict[str, Any]:
        """Return one current-contract, bounded history from existing Recorder truth."""

        connection = self._open(self.raw_path)
        if connection is None:
            raise RecorderStorageError("raw store is unavailable")
        try:
            market = self._market_row(connection, asset, now, current_ticker)
            if market is None:
                raise RecorderStorageError("current market is unavailable")
            ticker = str(market["ticker"])
            start = datetime.fromisoformat(str(market["window_start"])).astimezone(UTC)
            end = datetime.fromisoformat(str(market["window_end"])).astimezone(UTC)
            effective_end = min(now, end)
            underlying, source = self._underlying_history(connection, asset, start, effective_end)
            probability, complete, probability_last_change_at = self._probability_history(
                connection, ticker, start, effective_end
            )
            notes: list[str] = []
            if not complete:
                notes.append("probability history exceeded the bounded 100000-event replay")
            if not probability:
                notes.append("no synchronized WebSocket baseline exists in this contract window")
            return {
                "asset": asset.value,
                "ticker": ticker,
                "window_start": start,
                "window_end": end,
                "generated_at": now,
                "underlying_source": source,
                "underlying": underlying,
                "probability": probability,
                "underlying_last_actual_change_at": self._underlying_last_change_at(
                    connection, asset, start, effective_end
                ),
                "probability_last_actual_change_at": probability_last_change_at,
                "probability_complete": complete,
                "notes": notes,
            }
        finally:
            connection.close()

    @staticmethod
    def _underlying_history(
        connection: sqlite3.Connection,
        asset: Asset,
        start: datetime,
        end: datetime,
    ) -> tuple[list[dict[str, str]], str]:
        product = COINBASE_PRODUCT_BY_ASSET.get(asset)
        if product is not None:
            table = "coinbase_ticks"
            predicate = "product=?"
            parameters: tuple[object, ...] = (product, _timestamp(start), _timestamp(end))
            source = UnderlyingProvider.COINBASE.value
        else:
            table = "underlying_observations"
            predicate = "asset=? AND provider=?"
            parameters = (
                asset.value,
                UnderlyingProvider.PYTH_HERMES.value,
                _timestamp(start),
                _timestamp(end),
            )
            source = UnderlyingProvider.PYTH_HERMES.value
        rows = connection.execute(
            f"""WITH bucketed AS (
                SELECT CAST(strftime('%s', received_timestamp) AS INTEGER) AS bucket,
                    price, received_timestamp, id,
                    min(CAST(price AS REAL)) OVER (
                        PARTITION BY CAST(strftime('%s', received_timestamp) AS INTEGER)
                    ) AS minimum_price,
                    max(CAST(price AS REAL)) OVER (
                        PARTITION BY CAST(strftime('%s', received_timestamp) AS INTEGER)
                    ) AS maximum_price,
                    row_number() OVER (
                        PARTITION BY CAST(strftime('%s', received_timestamp) AS INTEGER)
                        ORDER BY received_timestamp DESC, id DESC
                    ) AS newest
                FROM {table} WHERE {predicate}
                  AND received_timestamp>=? AND received_timestamp<=?
            )
            SELECT bucket, price AS close_price, minimum_price, maximum_price
            FROM bucketed WHERE newest=1 ORDER BY bucket ASC LIMIT 1800""",
            parameters,
        )
        return (
            [
                {
                    "observed_at": datetime.fromtimestamp(int(row["bucket"]), UTC).isoformat(),
                    "source": source,
                    "close_price": str(row["close_price"]),
                    "minimum_price": str(row["minimum_price"]),
                    "maximum_price": str(row["maximum_price"]),
                }
                for row in rows
                if row["bucket"] is not None
            ],
            source,
        )

    @staticmethod
    def _underlying_last_change_at(
        connection: sqlite3.Connection,
        asset: Asset,
        start: datetime,
        end: datetime,
    ) -> str | None:
        """Find the final observed value transition without inventing a bucket average."""

        product = COINBASE_PRODUCT_BY_ASSET.get(asset)
        if product is not None:
            table = "coinbase_ticks"
            predicate = "product=?"
            predicate_parameters: tuple[object, ...] = (product,)
        else:
            table = "underlying_observations"
            predicate = "asset=? AND provider=?"
            predicate_parameters = (
                asset.value,
                UnderlyingProvider.PYTH_HERMES.value,
            )
        row = connection.execute(
            f"""WITH preceding AS (
                SELECT received_timestamp, price, id FROM {table}
                WHERE {predicate} AND received_timestamp<?
                ORDER BY received_timestamp DESC, id DESC LIMIT 1
            ), windowed AS (
                SELECT received_timestamp, price, id FROM {table}
                WHERE {predicate} AND received_timestamp>=? AND received_timestamp<=?
            ), observations AS (
                SELECT * FROM preceding UNION ALL SELECT * FROM windowed
            ), changes AS (
                SELECT received_timestamp, id, price,
                    lag(price) OVER (ORDER BY received_timestamp ASC, id ASC) AS prior_price
                FROM observations
            )
            SELECT received_timestamp FROM changes
            WHERE received_timestamp>=? AND prior_price IS NOT NULL AND price != prior_price
            ORDER BY received_timestamp DESC, id DESC LIMIT 1""",
            (
                *predicate_parameters,
                _timestamp(start),
                *predicate_parameters,
                _timestamp(start),
                _timestamp(end),
                _timestamp(start),
            ),
        ).fetchone()
        return None if row is None else str(row["received_timestamp"])

    @staticmethod
    def _probability_history(
        connection: sqlite3.Connection,
        ticker: str,
        start: datetime,
        end: datetime,
    ) -> tuple[list[dict[str, object]], bool, str | None]:
        rows = tuple(
            connection.execute(
                """SELECT sequence,event_kind,side,price,quantity_delta,yes_bids,no_bids,
                    socket_received_timestamp,sync_status_after
                FROM kalshi_ws_orderbook_events
                WHERE ticker=? AND socket_received_timestamp>=? AND socket_received_timestamp<=?
                ORDER BY socket_received_timestamp ASC,id ASC LIMIT 100001""",
                (ticker, _timestamp(start), _timestamp(end)),
            )
        )
        complete = len(rows) <= 100000
        yes: dict[Decimal, Decimal] = {}
        no: dict[Decimal, Decimal] = {}
        has_baseline = False
        points: list[dict[str, object]] = []
        previous: tuple[str | None, ...] | None = None
        last_actual_change_at: str | None = None
        for row in rows[:100000]:
            if str(row["sync_status_after"]) != "synchronized":
                yes.clear()
                no.clear()
                has_baseline = False
                previous = None
                continue
            kind = str(row["event_kind"])
            if kind == "orderbook_snapshot":
                yes = {
                    Decimal(str(item[0])): Decimal(str(item[1]))
                    for item in _json(row["yes_bids"], [])
                }
                no = {
                    Decimal(str(item[0])): Decimal(str(item[1]))
                    for item in _json(row["no_bids"], [])
                }
                has_baseline = True
            elif kind == "orderbook_delta" and row["side"] in {"yes", "no"}:
                if not has_baseline:
                    continue
                levels = yes if row["side"] == "yes" else no
                price = Decimal(str(row["price"]))
                quantity = levels.get(price, Decimal(0)) + Decimal(str(row["quantity_delta"]))
                if quantity <= 0:
                    levels.pop(price, None)
                else:
                    levels[price] = quantity
            else:
                continue
            yes_bid = str(max(yes)) if yes else None
            no_bid = str(max(no)) if no else None
            state = (
                yes_bid,
                str(Decimal(1) - Decimal(no_bid)) if no_bid is not None else None,
                no_bid,
                str(Decimal(1) - Decimal(yes_bid)) if yes_bid is not None else None,
            )
            if state == previous:
                continue
            if previous is not None:
                last_actual_change_at = str(row["socket_received_timestamp"])
            previous = state
            points.append(
                {
                    "observed_at": str(row["socket_received_timestamp"]),
                    "sequence": int(row["sequence"]),
                    "yes_bid": state[0],
                    "yes_ask": state[1],
                    "no_bid": state[2],
                    "no_ask": state[3],
                }
            )
        points = DashboardReadStore._downsample_probability(points)
        return points, complete, last_actual_change_at

    @staticmethod
    def _downsample_probability(
        points: list[dict[str, object]], maximum: int = 2000
    ) -> list[dict[str, object]]:
        """Bound output while retaining every bucket's extrema for all four quote series."""

        if len(points) <= maximum:
            return points
        fields = ("yes_bid", "yes_ask", "no_bid", "no_ask")
        interior = points[1:-1]
        # Each bucket can contribute at most min+max for each of four fields.
        bucket_count = max(1, (maximum - 2) // (len(fields) * 2))
        width = max(1, math.ceil(len(interior) / bucket_count))
        selected: list[dict[str, object]] = [points[0]]
        for offset in range(0, len(interior), width):
            bucket = interior[offset : offset + width]
            indices: set[int] = set()
            for field in fields:
                available = [
                    (index, Decimal(str(point[field])))
                    for index, point in enumerate(bucket)
                    if point.get(field) is not None
                ]
                if available:
                    indices.add(min(available, key=lambda item: item[1])[0])
                    indices.add(max(available, key=lambda item: item[1])[0])
            selected.extend(bucket[index] for index in sorted(indices))
        selected.append(points[-1])
        return selected[:maximum]

    def coverage(self) -> dict[str, Any]:
        finalized = self._finalized_counts()
        connection = self._open(self.feature_path)
        if connection is None:
            return self._empty_coverage("not_enough_training_data", finalized)
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                """SELECT * FROM dataset_builds WHERE status='complete'
                ORDER BY completed_timestamp DESC,created_timestamp DESC LIMIT 1"""
            ).fetchone()
            if row is None:
                return self._empty_coverage("not_enough_training_data", finalized)
            diagnostics = _json(row["diagnostics_json"], {})
            diagnostic_map = diagnostics if isinstance(diagnostics, dict) else {}
            source_snapshot = _json(row["source_snapshot_json"], {})
            snapshot_settlements = (
                source_snapshot.get("kalshi_settlements")
                if isinstance(source_snapshot, dict)
                else None
            )
            raw_snapshot_counts = (
                snapshot_settlements.get("counts_by_asset")
                if isinstance(snapshot_settlements, dict)
                else None
            )
            parsed_snapshot_counts = _int_dict(raw_snapshot_counts)
            snapshot_counts = (
                {asset.value: parsed_snapshot_counts.get(asset.value, 0) for asset in Asset}
                if parsed_snapshot_counts is not None
                else None
            )
            counts = {
                asset.value: {
                    "finalized_events": finalized[asset.value],
                    "evaluated_finalized_events": (
                        snapshot_counts[asset.value] if snapshot_counts is not None else 0
                    ),
                    "unevaluated_finalized_events": (
                        finalized[asset.value] - snapshot_counts[asset.value]
                        if snapshot_counts is not None
                        else finalized[asset.value]
                    ),
                    "trainable_events": 0,
                    "training_rows": 0,
                }
                for asset in Asset
            }
            for item in connection.execute(
                """SELECT asset,COUNT(DISTINCT ticker) events,COUNT(*) rows
                FROM training_examples WHERE build_id=? GROUP BY asset""",
                (row["build_id"],),
            ):
                counts[str(item["asset"])] = {
                    "finalized_events": finalized[str(item["asset"])],
                    "evaluated_finalized_events": (
                        snapshot_counts[str(item["asset"])] if snapshot_counts is not None else 0
                    ),
                    "unevaluated_finalized_events": (
                        finalized[str(item["asset"])] - snapshot_counts[str(item["asset"])]
                        if snapshot_counts is not None
                        else finalized[str(item["asset"])]
                    ),
                    "trainable_events": int(item["events"]),
                    "training_rows": int(item["rows"]),
                }
            trainable_events = sum(item["trainable_events"] for item in counts.values())
            training_rows = sum(item["training_rows"] for item in counts.values())
            snapshot_finalized = (
                sum(snapshot_counts.values()) if snapshot_counts is not None else None
            )
            unevaluated = (
                sum(finalized.values()) - snapshot_finalized
                if snapshot_finalized is not None
                else None
            )
            return {
                "status": "available",
                "finalized_events": sum(finalized.values()),
                "trainable_events": trainable_events,
                "training_rows": training_rows,
                "build_id": str(row["build_id"]),
                "dataset_version": str(row["dataset_version"]),
                "feature_schema_version": str(row["feature_schema_version"]),
                "completed_timestamp": row["completed_timestamp"],
                "snapshot_status": (
                    "unknown"
                    if snapshot_finalized is None
                    else "current"
                    if unevaluated == 0
                    else "outdated"
                ),
                "snapshot_finalized_events": snapshot_finalized,
                "unevaluated_finalized_events": unevaluated,
                "skipped_decisions": _optional_nonnegative_int(
                    diagnostic_map.get("skipped_decisions")
                ),
                "events_without_training_rows": _optional_nonnegative_int(
                    diagnostic_map.get("events_without_training_rows")
                ),
                "trainability_rejections": _int_dict(diagnostic_map.get("trainability_rejections")),
                "per_asset": counts,
                "label_balance": diagnostic_map.get("label_balance"),
                "decision_time_bucket_coverage": diagnostic_map.get(
                    "rows_per_decision_bucket_seconds"
                ),
                "missing_feature_rates": diagnostic_map.get("missing_feature_rates"),
                "stale_feature_rates": diagnostic_map.get("stale_feature_rates"),
            }
        except sqlite3.Error:
            return self._empty_coverage("feature_store_error", finalized)
        finally:
            connection.close()

    def training(self) -> dict[str, Any]:
        """Return separate, read-only projections for each training truth layer.

        A completed dataset build is immutable evidence; it is intentionally not used as
        the current trainable pool.  Missing projections remain ``None`` and carry an
        explicit reason instead of being represented as zero.
        """

        return {
            "raw_finalized_pool": self._raw_finalized_pool(),
            "current_trainable": self._current_trainable_projection(),
            "latest_completed_dataset": self._completed_dataset_projection(),
            "frozen_experiment_facts": [],
        }

    def _raw_finalized_pool(self) -> dict[str, Any]:
        counts = {asset.value: 0 for asset in Asset}
        connection = self._open(self.raw_path)
        base = {
            "state": "unknown",
            "status": "unknown",
            "reason_code": "RAW_FINALIZED_POOL_UNAVAILABLE",
            "events": None,
            "eligible_events": None,
            "ineligible_events": None,
            "rows": None,
            "assets": None,
            "observed_at": None,
            "source_path": str(self.raw_path),
            "per_asset": {},
        }
        if connection is None:
            return base
        try:
            if not self._table_exists(connection, "kalshi_settlements"):
                return base
            rows = connection.execute(
                "SELECT asset,COUNT(*) AS events,MAX(settlement_timestamp) AS observed_at "
                "FROM kalshi_settlements GROUP BY asset"
            ).fetchall()
            latest: str | None = None
            for row in rows:
                asset = str(row["asset"])
                if asset in counts:
                    counts[asset] = int(row["events"])
                candidate = row["observed_at"]
                if isinstance(candidate, str) and (latest is None or candidate > latest):
                    latest = candidate
            total = sum(counts.values())
            return {
                "state": "available",
                "status": "available",
                "reason_code": "RAW_FINALIZED_POOL_READ_ONLY",
                "events": total,
                "eligible_events": total,
                "ineligible_events": 0,
                "rows": total,
                "assets": sum(value > 0 for value in counts.values()),
                "observed_at": latest,
                "source_path": str(self.raw_path),
                "per_asset": {
                    asset: {"events": value, "rows": value, "eligible_events": value}
                    for asset, value in counts.items()
                },
            }
        except sqlite3.Error:
            return base
        finally:
            connection.close()

    def _current_trainable_projection(self) -> dict[str, Any]:
        path = self.current_trainable_path
        base = {
            "state": "unknown",
            "status": "unknown",
            "reason_code": "CURRENT_TRAINABLE_UNAVAILABLE",
            "events": None,
            "eligible_events": None,
            "ineligible_events": None,
            "rows": None,
            "assets": None,
            "observed_at": None,
            "source_path": str(path) if path is not None else None,
            "per_asset": {},
        }
        if path is None:
            return base
        connection = self._open(path)
        if connection is None:
            return base
        try:
            required = {"current_trainable_events", "current_trainable_rows"}
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not required.issubset(tables):
                return {
                    **base,
                    "reason_code": "CURRENT_TRAINABLE_SCHEMA_UNAVAILABLE",
                    "status": "schema_unavailable",
                }
            event_rows = connection.execute(
                "SELECT asset,COUNT(*) AS events,SUM(CASE WHEN eligibility_status='eligible' "
                "THEN 1 ELSE 0 END) AS eligible_events,"
                "SUM(CASE WHEN eligibility_status!='eligible' THEN 1 ELSE 0 END) "
                "AS ineligible_events FROM current_trainable_events GROUP BY asset"
            ).fetchall()
            row_counts = connection.execute(
                "SELECT asset,COUNT(*) AS rows FROM current_trainable_rows GROUP BY asset"
            ).fetchall()
            per_asset: dict[str, dict[str, int]] = {}
            for row in event_rows:
                asset = str(row["asset"])
                per_asset[asset] = {
                    "events": int(row["events"]),
                    "eligible_events": int(row["eligible_events"] or 0),
                    "ineligible_events": int(row["ineligible_events"] or 0),
                }
            for row in row_counts:
                per_asset.setdefault(str(row["asset"]), {})["rows"] = int(row["rows"])
            checkpoint = None
            if "current_trainable_checkpoint" in tables:
                checkpoint = connection.execute(
                    "SELECT last_evaluated_timestamp FROM current_trainable_checkpoint "
                    "WHERE singleton=1"
                ).fetchone()
            return {
                "state": "available",
                "status": "available",
                "reason_code": "CURRENT_TRAINABLE_MATERIALIZED",
                "events": sum(int(row["events"]) for row in event_rows),
                "eligible_events": sum(int(row["eligible_events"] or 0) for row in event_rows),
                "ineligible_events": sum(int(row["ineligible_events"] or 0) for row in event_rows),
                "rows": sum(int(row["rows"]) for row in row_counts),
                "assets": len({str(row["asset"]) for row in event_rows}),
                "observed_at": checkpoint["last_evaluated_timestamp"] if checkpoint else None,
                "source_path": str(path),
                "per_asset": per_asset,
            }
        except sqlite3.Error:
            return base
        finally:
            connection.close()

    def _completed_dataset_projection(self) -> dict[str, Any]:
        base = {
            "state": "unknown",
            "status": "source_unavailable",
            "reason_code": "FEATURE_STORE_UNAVAILABLE",
            "build_id": None,
            "dataset_version": None,
            "feature_schema_version": None,
            "completed_timestamp": None,
            "events": None,
            "rows": None,
            "snapshot_status": "unknown",
            "diagnostics": None,
            "per_asset": {},
        }
        connection = self._open(self.feature_path)
        if connection is None:
            return base
        try:
            row = connection.execute(
                "SELECT * FROM dataset_builds WHERE status='complete' "
                "ORDER BY completed_timestamp DESC,created_timestamp DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return {
                    **base,
                    "state": "not_materialized",
                    "status": "not_materialized",
                    "reason_code": "DATASET_SNAPSHOT_NOT_BUILT",
                    "snapshot_status": "not_built",
                }
            diagnostics = _json(row["diagnostics_json"], {})
            diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
            per_asset: dict[str, dict[str, int]] = {}
            for item in connection.execute(
                "SELECT asset,COUNT(DISTINCT ticker) AS events,COUNT(*) AS rows "
                "FROM training_examples WHERE build_id=? GROUP BY asset",
                (row["build_id"],),
            ):
                per_asset[str(item["asset"])] = {
                    "events": int(item["events"]),
                    "rows": int(item["rows"]),
                }
            rows = sum(item["rows"] for item in per_asset.values())
            events = sum(item["events"] for item in per_asset.values())
            snapshot_status = "unknown"
            source_snapshot = _json(row["source_snapshot_json"], {})
            if isinstance(source_snapshot, dict):
                settlement = source_snapshot.get("kalshi_settlements")
                if isinstance(settlement, dict):
                    source_count = settlement.get("count")
                    if isinstance(source_count, int):
                        raw = self._raw_finalized_pool()
                        raw_events = raw.get("events")
                        snapshot_status = "current" if raw_events == source_count else "outdated"
            state = "stale" if snapshot_status == "outdated" else "available"
            return {
                "state": state,
                "status": "available" if state == "available" else "stale",
                "reason_code": "COMPLETED_DATASET_SNAPSHOT_STALE"
                if state == "stale"
                else "COMPLETED_DATASET_SNAPSHOT",
                "build_id": str(row["build_id"]),
                "dataset_version": str(row["dataset_version"]),
                "feature_schema_version": str(row["feature_schema_version"]),
                "completed_timestamp": row["completed_timestamp"],
                "events": events,
                "rows": rows,
                "snapshot_status": snapshot_status,
                "diagnostics": diagnostics,
                "per_asset": per_asset,
            }
        except sqlite3.Error:
            return base
        finally:
            connection.close()

    @staticmethod
    def _empty_coverage(reason: str, finalized: dict[str, int]) -> dict[str, Any]:
        return {
            "status": reason,
            "finalized_events": sum(finalized.values()),
            "trainable_events": 0,
            "training_rows": 0,
            "dataset_version": DATASET_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "build_id": None,
            "completed_timestamp": None,
            "snapshot_status": "not_built",
            "snapshot_finalized_events": None,
            "unevaluated_finalized_events": sum(finalized.values()),
            "skipped_decisions": None,
            "events_without_training_rows": None,
            "trainability_rejections": None,
            "label_balance": None,
            "decision_time_bucket_coverage": None,
            "missing_feature_rates": None,
            "stale_feature_rates": None,
            "per_asset": {
                asset.value: {
                    "finalized_events": finalized[asset.value],
                    "evaluated_finalized_events": 0,
                    "unevaluated_finalized_events": finalized[asset.value],
                    "trainable_events": 0,
                    "training_rows": 0,
                }
                for asset in Asset
            },
        }

    def _finalized_counts(self) -> dict[str, int]:
        counts = {asset.value: 0 for asset in Asset}
        raw = self._open(self.raw_path)
        if raw is None:
            return counts
        try:
            raw.execute("BEGIN")
            for item in raw.execute("SELECT asset,count FROM kalshi_settlement_counts"):
                asset = str(item["asset"])
                if asset in counts:
                    counts[asset] = int(item["count"])
        except sqlite3.Error:
            return {asset.value: 0 for asset in Asset}
        finally:
            raw.close()
        return counts

    def recorder_events(
        self,
        *,
        limit: int,
        severity: str | None,
        asset: Asset | None,
        source: str | None,
        since: datetime | None,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("event limit must be in 1..200")
        if source is not None and (not source or len(source) > 160):
            raise ValueError("invalid event source filter")
        connection = self._open(self.raw_path)
        if connection is None:
            return []
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recorder_events'"
            ).fetchone()
            if exists is None:
                return []
            clauses: list[str] = []
            parameters: list[object] = []
            if severity is not None:
                clauses.append("severity=?")
                parameters.append(severity)
            if asset is not None:
                clauses.append("asset=?")
                parameters.append(asset.value)
            if source is not None:
                clauses.append("source=?")
                parameters.append(source)
            if since is not None:
                clauses.append("observed_timestamp>=?")
                parameters.append(_timestamp(since))
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            parameters.append(limit)
            rows = connection.execute(
                f"""SELECT observed_timestamp,severity,event_type,asset,source,error_type,message
                FROM recorder_events {where}
                ORDER BY observed_timestamp DESC,id DESC LIMIT ?""",
                parameters,
            )
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []
        finally:
            connection.close()

    def event_summary(
        self,
        *,
        asset: Asset | None,
        source: str | None,
        since: datetime,
        until: datetime,
    ) -> dict[str, int] | None:
        """Return exact severity totals from the indexed, explicitly bounded event window."""

        if source is not None and (not source or len(source) > 160):
            raise ValueError("invalid event source filter")
        connection = self._open(self.raw_path)
        if connection is None:
            return None
        try:
            if not self._table_exists(connection, "recorder_events"):
                return None
            clauses = [
                "observed_timestamp>=?",
                "observed_timestamp<=?",
                "severity IN ('warning','error','fatal')",
            ]
            parameters: list[object] = [_timestamp(since), _timestamp(until)]
            if asset is not None:
                clauses.append("asset=?")
                parameters.append(asset.value)
            if source is not None:
                clauses.append("source=?")
                parameters.append(source)
            rows = connection.execute(
                "SELECT severity,COUNT(*) AS count FROM recorder_events "
                f"WHERE {' AND '.join(clauses)} GROUP BY severity",
                parameters,
            )
            return {str(row["severity"]): int(row["count"]) for row in rows}
        except sqlite3.Error:
            return None
        finally:
            connection.close()

    def raw_finalized_pool(self) -> dict[str, Any]:
        """Expose only the raw-finalized read projection needed by ``/api/data``."""

        return self._raw_finalized_pool()


def _optional_nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _int_dict(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    result = {
        key: item
        for key, item in value.items()
        if isinstance(key, str)
        and isinstance(item, int)
        and not isinstance(item, bool)
        and item >= 0
    }
    return result if len(result) == len(value) else None
