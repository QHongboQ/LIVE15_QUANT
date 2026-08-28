"""Typed, deterministic detection of observation gaps on immutable snapshots."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from live15_quant.config import Settings
from live15_quant.features import COINBASE_PRODUCT_BY_ASSET
from live15_quant.market_sessions import MarketDataState, open_intervals_for_asset
from live15_quant.models import Asset, UnderlyingProvider
from live15_quant.providers.kalshi import KALSHI_15MIN_SERIES
from live15_quant.providers.pyth import PYTH_FEEDS


class GapSource(StrEnum):
    KALSHI_REST = "kalshi_rest"
    KALSHI_WS = "kalshi_ws"
    COINBASE = "coinbase"
    PYTH = "pyth"
    BINANCE = "binance"
    HYPERLIQUID = "hyperliquid"


class GapReason(StrEnum):
    OBSERVATION_INTERVAL = "observation_interval"
    RESTART = "restart_gap"
    RUNTIME_STALL = "runtime_stall_gap"
    SOURCE_OUTAGE = "source_outage"
    RECONNECT = "reconnect_gap"
    SEQUENCE_GAP = "sequence_gap"
    PAYLOAD_INVALID = "payload_invalid"
    BOOK_INVARIANT = "book_invariant"


class InferenceReadinessStatus(StrEnum):
    PASS = "PASS"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class GapStream:
    source: GapSource
    asset: Asset
    instrument: str
    threshold_seconds: Decimal

    def __post_init__(self) -> None:
        if not self.instrument or self.threshold_seconds <= 0:
            raise ValueError("gap streams require an instrument and positive threshold")


@dataclass(frozen=True, slots=True)
class DataGap:
    source: GapSource
    asset: Asset
    instrument: str
    gap_start: datetime
    gap_end: datetime | None
    detected_at: datetime
    threshold_seconds: Decimal
    reason: GapReason
    error_type: str | None = None
    recovered: bool = True
    recorder_session_id: str | None = None
    incident_id: str | None = None

    def __post_init__(self) -> None:
        for value in (self.gap_start, self.detected_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("gap timestamps must be timezone-aware")
        if self.gap_end is not None and (
            self.gap_end.tzinfo is None or self.gap_end.utcoffset() is None
        ):
            raise ValueError("gap timestamps must be timezone-aware")
        if not self.instrument:
            raise ValueError("a gap requires an instrument")
        if self.recovered and (self.gap_end is None or self.gap_end <= self.gap_start):
            raise ValueError("a recovered gap requires an ordered non-empty interval")
        if not self.recovered and self.gap_end is not None:
            raise ValueError("an active gap cannot have an end timestamp")
        if self.threshold_seconds <= 0:
            raise ValueError("gap threshold must be positive")

    @property
    def duration_seconds(self) -> Decimal | None:
        if self.gap_end is None:
            return None
        delta = self.gap_end - self.gap_start
        microseconds = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
        return Decimal(microseconds) / Decimal(1_000_000)

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.gap_start < end and (self.gap_end is None or self.gap_end > start)


@dataclass(frozen=True, slots=True)
class LiveInferenceReadiness:
    status: InferenceReadinessStatus
    reasons: tuple[str, ...]
    checked_at: datetime


def effective_data_gaps(facts: tuple[DataGap, ...]) -> tuple[DataGap, ...]:
    """Project append-only OPEN/RECOVERED facts into effective intervals."""

    projected: dict[tuple[GapSource, Asset, str, datetime], DataGap] = {}
    for fact in facts:
        key = (fact.source, fact.asset, fact.instrument, fact.gap_start)
        existing = projected.get(key)
        if existing is None or (fact.recovered and not existing.recovered):
            projected[key] = fact
            continue
        if existing == fact or (existing.recovered and not fact.recovered):
            continue
        raise ValueError("conflicting append-only data-gap facts")
    return tuple(
        sorted(
            projected.values(),
            key=lambda gap: (
                gap.gap_start,
                gap.source.value,
                gap.asset.value,
                gap.instrument,
            ),
        )
    )


def configured_streams(settings: Settings) -> tuple[GapStream, ...]:
    """Return the single typed source/asset threshold registry."""

    streams: list[GapStream] = []
    quote_threshold = Decimal(str(settings.official_quote_max_source_age_seconds))
    for asset in Asset:
        streams.append(
            GapStream(GapSource.KALSHI_REST, asset, KALSHI_15MIN_SERIES[asset], quote_threshold)
        )
    ws_threshold = Decimal(str(settings.kalshi_websocket_stale_seconds))
    for asset in Asset:
        streams.append(
            GapStream(
                GapSource.KALSHI_WS,
                asset,
                KALSHI_15MIN_SERIES[asset],
                ws_threshold,
            )
        )
    coinbase_threshold = Decimal(str(settings.recorder_coinbase_stale_seconds))
    for asset, product in COINBASE_PRODUCT_BY_ASSET.items():
        streams.append(GapStream(GapSource.COINBASE, asset, product, coinbase_threshold))
    pyth_threshold = Decimal(str(settings.recorder_pyth_stale_seconds))
    for asset, (symbol, _feed_id) in PYTH_FEEDS.items():
        streams.append(GapStream(GapSource.PYTH, asset, symbol, pyth_threshold))
    secondary_threshold = Decimal(str(settings.recorder_secondary_stale_seconds))
    streams.extend(
        (
            GapStream(GapSource.BINANCE, Asset.BNB, "BNBUSDT", secondary_threshold),
            GapStream(GapSource.HYPERLIQUID, Asset.HYPE, "HYPE", secondary_threshold),
        )
    )
    return tuple(streams)


def detect_gaps(
    connection: sqlite3.Connection,
    streams: tuple[GapStream, ...],
    *,
    start: datetime,
    end: datetime,
    detected_at: datetime,
    immutable_snapshot: bool,
) -> tuple[DataGap, ...]:
    """Detect gaps with bounded indexed queries on an immutable database snapshot."""

    if not immutable_snapshot:
        raise ValueError("historical gap detection requires an immutable database snapshot")
    for value in (start, end, detected_at):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("gap detection timestamps must be timezone-aware")
    if end <= start:
        raise ValueError("gap detection range must be positive")
    events = _operational_events(connection, start=start, end=end)
    existing = _existing_gap_keys(connection, start=start, end=end)
    detected: list[DataGap] = []
    for stream in streams:
        timestamps = _received_timestamps(connection, stream, start=start, end=end)
        for previous, current in pairwise(timestamps):
            candidate_intervals = (
                open_intervals_for_asset(stream.asset, previous, current)
                if stream.source is GapSource.PYTH
                else ((previous, current),)
            )
            for gap_start, gap_end in candidate_intervals:
                duration = timedelta_seconds(gap_end - gap_start)
                if duration <= stream.threshold_seconds:
                    continue
                logical_key = (
                    stream.source.value,
                    stream.asset.value,
                    stream.instrument,
                    gap_start.astimezone(UTC).isoformat(timespec="microseconds"),
                    gap_end.astimezone(UTC).isoformat(timespec="microseconds"),
                )
                if logical_key in existing:
                    continue
                reason, error_type, incident_id = _classify(events, stream, gap_start, gap_end)
                detected.append(
                    DataGap(
                        source=stream.source,
                        asset=stream.asset,
                        instrument=stream.instrument,
                        gap_start=gap_start,
                        gap_end=gap_end,
                        detected_at=detected_at,
                        threshold_seconds=stream.threshold_seconds,
                        reason=reason,
                        error_type=error_type,
                        incident_id=incident_id,
                    )
                )
    return tuple(detected)


def _existing_gap_keys(
    connection: sqlite3.Connection, *, start: datetime, end: datetime
) -> set[tuple[str, str, str, str, str]]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='data_gaps'"
    ).fetchone()
    if exists is None:
        return set()
    return {
        (str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]))
        for row in connection.execute(
            "SELECT source,asset,instrument,gap_start,gap_end FROM data_gaps "
            "WHERE recovered=1 AND gap_end>? AND gap_start<?",
            (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
        )
    }


def inference_readiness(
    *,
    checked_at: datetime,
    required_since: datetime,
    latest_received: datetime | None,
    max_age: timedelta,
    active_gaps: tuple[DataGap, ...] = (),
    source_connected: bool = True,
    synchronized_orderbook: bool = True,
    lookback_complete: bool = True,
    underlying_state: MarketDataState | None = None,
) -> LiveInferenceReadiness:
    """Fail closed without creating strategy or execution behavior."""

    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("inference checked_at must be timezone-aware")
    if required_since.tzinfo is None or required_since.utcoffset() is None:
        raise ValueError("inference required_since must be timezone-aware")
    if required_since >= checked_at or max_age <= timedelta(0):
        raise ValueError("inference lookback and max age must be positive")
    if latest_received is not None and (
        latest_received.tzinfo is None or latest_received.utcoffset() is None
    ):
        raise ValueError("inference receive timestamp must be timezone-aware")
    reasons: list[str] = []
    if underlying_state is MarketDataState.MARKET_CLOSED:
        reasons.append("market_closed")
    elif underlying_state is MarketDataState.SOURCE_UNAVAILABLE:
        reasons.append("source_unavailable")
    elif underlying_state is MarketDataState.STALE:
        reasons.append("stale_source")
    if latest_received is None:
        reasons.append("required_observation_missing")
    elif latest_received > checked_at:
        reasons.append("future_observation")
    elif checked_at - latest_received > max_age:
        reasons.append("stale_source")
    if not lookback_complete:
        reasons.append("insufficient_lookback")
    if any(gap.overlaps(required_since, checked_at) for gap in active_gaps):
        reasons.append("source_gap_overlap")
    if not source_connected:
        reasons.append("source_disconnected")
    if not synchronized_orderbook:
        reasons.append("orderbook_unsynchronized")
    unique = tuple(dict.fromkeys(reasons))
    return LiveInferenceReadiness(
        InferenceReadinessStatus.PASS if not unique else InferenceReadinessStatus.DATA_UNAVAILABLE,
        unique,
        checked_at,
    )


def _received_timestamps(
    connection: sqlite3.Connection,
    stream: GapStream,
    *,
    start: datetime,
    end: datetime,
) -> tuple[datetime, ...]:
    lower = start.astimezone(UTC).isoformat()
    upper = end.astimezone(UTC).isoformat()
    if stream.source is GapSource.KALSHI_REST:
        sql = (
            "SELECT received_timestamp FROM kalshi_prediction_quotes "
            "WHERE asset=? AND received_timestamp>=? AND received_timestamp<=? "
            "ORDER BY received_timestamp,id"
        )
        parameters = (stream.asset.value, lower, upper)
    elif stream.source is GapSource.KALSHI_WS:
        sql = (
            "SELECT socket_received_timestamp FROM kalshi_ws_orderbook_events AS ws "
            "WHERE ws.ticker IS NOT NULL AND socket_received_timestamp>=? "
            "AND socket_received_timestamp<=? AND EXISTS ("
            "SELECT 1 FROM kalshi_market_lifecycle AS market "
            "WHERE market.ticker=ws.ticker AND market.asset=?) "
            "ORDER BY socket_received_timestamp,id"
        )
        parameters = (lower, upper, stream.asset.value)
    elif stream.source is GapSource.COINBASE:
        sql = (
            "SELECT received_timestamp FROM coinbase_ticks "
            "WHERE product=? AND received_timestamp>=? AND received_timestamp<=? "
            "ORDER BY received_timestamp,id"
        )
        parameters = (stream.instrument, lower, upper)
    elif stream.source is GapSource.PYTH:
        sql = (
            "SELECT received_timestamp FROM underlying_observations "
            "WHERE asset=? AND provider=? AND received_timestamp>=? AND received_timestamp<=? "
            "ORDER BY received_timestamp,id"
        )
        parameters = (
            stream.asset.value,
            UnderlyingProvider.PYTH_HERMES.value,
            lower,
            upper,
        )
    else:
        provider = (
            UnderlyingProvider.BINANCE_SPOT
            if stream.source is GapSource.BINANCE
            else UnderlyingProvider.HYPERLIQUID_PERP
        )
        sql = (
            "SELECT received_timestamp FROM secondary_underlying_observations "
            "WHERE asset=? AND provider=? AND received_timestamp>=? AND received_timestamp<=? "
            "ORDER BY received_timestamp,id"
        )
        parameters = (stream.asset.value, provider.value, lower, upper)
    observed = [_parse_timestamp(row[0]) for row in connection.execute(sql, parameters)]
    if stream.source is GapSource.KALSHI_REST:
        prior_sql = (
            "SELECT received_timestamp FROM kalshi_prediction_quotes "
            "WHERE asset=? AND received_timestamp<? "
            "ORDER BY received_timestamp DESC,id DESC LIMIT 1"
        )
        prior_parameters = (stream.asset.value, lower)
    elif stream.source is GapSource.KALSHI_WS:
        prior_sql = (
            "SELECT socket_received_timestamp FROM kalshi_ws_orderbook_events AS ws "
            "WHERE ws.ticker IS NOT NULL AND socket_received_timestamp<? AND EXISTS ("
            "SELECT 1 FROM kalshi_market_lifecycle AS market "
            "WHERE market.ticker=ws.ticker AND market.asset=?) "
            "ORDER BY socket_received_timestamp DESC,id DESC LIMIT 1"
        )
        prior_parameters = (lower, stream.asset.value)
    elif stream.source is GapSource.COINBASE:
        prior_sql = (
            "SELECT received_timestamp FROM coinbase_ticks "
            "WHERE product=? AND received_timestamp<? "
            "ORDER BY received_timestamp DESC,id DESC LIMIT 1"
        )
        prior_parameters = (stream.instrument, lower)
    elif stream.source is GapSource.PYTH:
        prior_sql = (
            "SELECT received_timestamp FROM underlying_observations "
            "WHERE asset=? AND provider=? AND received_timestamp<? "
            "ORDER BY received_timestamp DESC,id DESC LIMIT 1"
        )
        prior_parameters = (stream.asset.value, UnderlyingProvider.PYTH_HERMES.value, lower)
    else:
        provider = (
            UnderlyingProvider.BINANCE_SPOT
            if stream.source is GapSource.BINANCE
            else UnderlyingProvider.HYPERLIQUID_PERP
        )
        prior_sql = (
            "SELECT received_timestamp FROM secondary_underlying_observations "
            "WHERE asset=? AND provider=? AND received_timestamp<? "
            "ORDER BY received_timestamp DESC,id DESC LIMIT 1"
        )
        prior_parameters = (stream.asset.value, provider.value, lower)
    prior = connection.execute(prior_sql, prior_parameters).fetchone()
    if prior is not None:
        observed.insert(0, _parse_timestamp(prior[0]))
    return tuple(observed)


def _operational_events(
    connection: sqlite3.Connection, *, start: datetime, end: datetime
) -> tuple[tuple[datetime, str, str | None, int, str | None, str | None], ...]:
    return tuple(
        (
            _parse_timestamp(row[0]),
            str(row[1]),
            None if row[2] is None else str(row[2]),
            int(row[3]),
            None if row[4] is None else str(row[4]),
            None if row[5] is None else str(row[5]),
        )
        for row in connection.execute(
            "SELECT observed_timestamp,event_type,error_type,id,asset,source FROM recorder_events "
            "WHERE observed_timestamp>=? AND observed_timestamp<=? ORDER BY observed_timestamp,id",
            (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
        )
    )


def _classify(
    events: tuple[tuple[datetime, str, str | None, int, str | None, str | None], ...],
    stream: GapStream,
    start: datetime,
    end: datetime,
) -> tuple[GapReason, str | None, str | None]:
    matching = tuple(event for event in events if start < event[0] <= end)
    for _observed, event_type, error_type, row_id, _asset, _source in matching:
        if event_type in {"fatal_task", "sqlite_integrity_failure"}:
            return GapReason.RUNTIME_STALL, error_type, f"recorder-event:{row_id}"
    for _observed, event_type, error_type, row_id, _asset, _source in matching:
        if event_type in {"recorder_started", "recorder_recovered"}:
            return GapReason.RESTART, error_type, f"recorder-event:{row_id}"
    for _observed, event_type, error_type, row_id, asset, source in matching:
        if event_type in {
            "source_temporarily_unavailable",
            "retry_exhausted",
            "source_stale",
        } and _event_matches_stream(stream, asset, source):
            return GapReason.SOURCE_OUTAGE, error_type, f"recorder-event:{row_id}"
    return GapReason.OBSERVATION_INTERVAL, None, None


def _event_matches_stream(stream: GapStream, asset: str | None, source: str | None) -> bool:
    if asset is not None and asset != stream.asset.value:
        return False
    if source is None:
        return asset == stream.asset.value
    prefixes = {
        GapSource.KALSHI_REST: ("kalshi_quote:", "kalshi_discovery:"),
        GapSource.KALSHI_WS: ("kalshi_ws",),
        GapSource.COINBASE: ("coinbase",),
        GapSource.PYTH: ("pyth",),
        GapSource.BINANCE: ("secondary:BNB",),
        GapSource.HYPERLIQUID: ("secondary:HYPE",),
    }[stream.source]
    return any(source == prefix or source.startswith(prefix) for prefix in prefixes)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("malformed gap source timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("gap source timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def timedelta_seconds(value: timedelta) -> Decimal:
    """Convert a timedelta to exact decimal seconds without float round-trips."""

    return (
        Decimal(value.days * 86400)
        + Decimal(value.seconds)
        + Decimal(value.microseconds) / Decimal(1_000_000)
    )
