"""Immutable canonical events produced from kalshi-sdk v12 WebSocket models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from live15_quant.models import Asset, OrderBookLevel

SDK_CANONICAL_PROVENANCE = "kalshi_sdk_v12_canonical_ws"


class CanonicalEventType(StrEnum):
    SNAPSHOT = "SNAPSHOT"
    DELTA = "DELTA"
    TICKER = "TICKER"
    LIFECYCLE = "LIFECYCLE"
    UNKNOWN_LIFECYCLE = "UNKNOWN_LIFECYCLE"
    PAYLOAD_INVALID = "PAYLOAD_INVALID"
    RECONNECT = "RECONNECT"


@dataclass(frozen=True, slots=True)
class CanonicalSdkEvent:
    asset: Asset
    ticker: str
    event_type: CanonicalEventType
    sequence: int | None
    subscription_id: int | None
    connection_id: str
    sdk_receive_timestamp: datetime
    exchange_timestamp: datetime | None = None
    market_id: str | None = None
    yes_bids: tuple[OrderBookLevel, ...] = ()
    no_bids: tuple[OrderBookLevel, ...] = ()
    delta_side: str | None = None
    delta_price: Decimal | None = None
    delta_quantity: Decimal | None = None
    lifecycle_type: str | None = None
    lifecycle_result: str | None = None
    event_ticker: str | None = None
    exchange_index: int | None = None
    diagnostic: str | None = None
    provenance: str = SDK_CANONICAL_PROVENANCE

    @property
    def executable_yes_ask(self) -> Decimal | None:
        return None if not self.no_bids else Decimal(1) - self.no_bids[0].price

    @property
    def executable_no_ask(self) -> Decimal | None:
        return None if not self.yes_bids else Decimal(1) - self.yes_bids[0].price

    @property
    def top_depth(self) -> Mapping[str, tuple[OrderBookLevel, ...]]:
        return {"yes": self.yes_bids[:3], "no": self.no_bids[:3]}


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"SDK {field} is malformed")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"SDK {field} is malformed") from None
    if not result.is_finite():
        raise ValueError(f"SDK {field} is malformed")
    return result


def _levels(values: object, field: str) -> tuple[OrderBookLevel, ...]:
    if not isinstance(values, Mapping):
        raise ValueError(f"SDK {field} is malformed")
    levels: dict[Decimal, Decimal] = {}
    for raw_price, raw_quantity in values.items():
        price = _decimal(raw_price, f"{field} price")
        quantity = _decimal(raw_quantity, f"{field} quantity")
        if price < 0 or price > 1 or quantity <= 0 or price in levels:
            raise ValueError(f"SDK {field} is malformed")
        levels[price] = quantity
    return tuple(OrderBookLevel(price, levels[price]) for price in sorted(levels, reverse=True))


def _source_timestamp(payload: object) -> datetime | None:
    raw_ms = getattr(payload, "ts_ms", None)
    if isinstance(raw_ms, int) and not isinstance(raw_ms, bool):
        try:
            return datetime.fromtimestamp(raw_ms / 1000, UTC)
        except (OSError, OverflowError, ValueError):
            raise ValueError("SDK exchange timestamp is malformed") from None
    raw = getattr(payload, "ts", None)
    if isinstance(raw, datetime):
        return _aware(raw, "SDK exchange timestamp")
    for name in ("settled_ts", "determination_ts", "close_ts", "open_ts"):
        lifecycle_seconds = getattr(payload, name, None)
        if isinstance(lifecycle_seconds, int) and not isinstance(lifecycle_seconds, bool):
            try:
                return datetime.fromtimestamp(lifecycle_seconds, UTC)
            except (OSError, OverflowError, ValueError):
                raise ValueError("SDK lifecycle timestamp is malformed") from None
    return None


def _identity(
    message: object,
    asset_by_ticker: Mapping[str, Asset],
) -> tuple[Asset, str, object, int | None, int | None]:
    payload = getattr(message, "msg", None)
    ticker = str(getattr(payload, "market_ticker", ""))
    if not ticker:
        raise ValueError("SDK market ticker is missing")
    try:
        asset = asset_by_ticker[ticker]
    except KeyError:
        raise ValueError("SDK ticker is outside the canonical universe") from None
    sid = getattr(message, "sid", None)
    seq = getattr(message, "seq", None)
    if sid is not None and (not isinstance(sid, int) or isinstance(sid, bool) or sid < 1):
        raise ValueError("SDK subscription identity is malformed")
    if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool) or seq < 1):
        raise ValueError("SDK sequence is malformed")
    return asset, ticker, payload, sid, seq


def canonical_from_sdk(
    message: object,
    *,
    asset_by_ticker: Mapping[str, Asset],
    connection_id: str,
    received_at: datetime,
) -> CanonicalSdkEvent:
    """Convert one SDK typed message without depending on SDK transport internals."""

    if not connection_id:
        raise ValueError("canonical connection identity is required")
    received = _aware(received_at, "SDK receive timestamp")
    asset, ticker, payload, sid, seq = _identity(message, asset_by_ticker)
    kind = str(getattr(message, "type", ""))

    if kind == "orderbook_snapshot":
        market_id = str(getattr(payload, "market_id", ""))
        if not market_id or sid is None or seq is None:
            raise ValueError("SDK snapshot envelope is incomplete")
        return CanonicalSdkEvent(
            asset=asset,
            ticker=ticker,
            event_type=CanonicalEventType.SNAPSHOT,
            sequence=seq,
            subscription_id=sid,
            connection_id=connection_id,
            sdk_receive_timestamp=received,
            exchange_timestamp=_source_timestamp(payload),
            market_id=market_id,
            yes_bids=_levels(getattr(payload, "yes", None), "YES snapshot"),
            no_bids=_levels(getattr(payload, "no", None), "NO snapshot"),
        )

    if kind == "orderbook_delta":
        market_id = str(getattr(payload, "market_id", ""))
        side = str(getattr(payload, "side", "")).lower()
        if not market_id or side not in {"yes", "no"} or sid is None or seq is None:
            raise ValueError("SDK delta envelope is incomplete")
        price = _decimal(getattr(payload, "price", None), "delta price")
        quantity = _decimal(getattr(payload, "delta", None), "delta quantity")
        if price < 0 or price > 1 or quantity == 0:
            raise ValueError("SDK delta values are malformed")
        return CanonicalSdkEvent(
            asset=asset,
            ticker=ticker,
            event_type=CanonicalEventType.DELTA,
            sequence=seq,
            subscription_id=sid,
            connection_id=connection_id,
            sdk_receive_timestamp=received,
            exchange_timestamp=_source_timestamp(payload),
            market_id=market_id,
            delta_side=side,
            delta_price=price,
            delta_quantity=quantity,
        )

    if kind == "ticker":
        return CanonicalSdkEvent(
            asset=asset,
            ticker=ticker,
            event_type=CanonicalEventType.TICKER,
            sequence=seq,
            subscription_id=sid,
            connection_id=connection_id,
            sdk_receive_timestamp=received,
            exchange_timestamp=_source_timestamp(payload),
            market_id=str(getattr(payload, "market_id", "")) or None,
        )

    if kind == "market_lifecycle_v2":
        lifecycle = str(getattr(payload, "event_type", "")).strip().lower()
        if not lifecycle:
            raise ValueError("SDK lifecycle type is missing")
        exchange_index = getattr(payload, "exchange_index", None)
        if exchange_index is not None and (
            not isinstance(exchange_index, int) or isinstance(exchange_index, bool)
        ):
            raise ValueError("SDK lifecycle exchange index is malformed")
        additional = getattr(payload, "additional_metadata", None)
        nested_event_ticker = (
            additional.get("event_ticker") if isinstance(additional, Mapping) else None
        )
        explicit_event_ticker = str(
            getattr(payload, "event_ticker", "") or nested_event_ticker or ""
        )
        raw_result = getattr(payload, "result", None)
        lifecycle_result = str(raw_result).strip().lower() if raw_result not in {None, ""} else None
        # Production may omit ``event_ticker`` from a market-scoped lifecycle
        # frame. The envelope's ``market_ticker`` has already been validated
        # against the active universe, so its final contract suffix gives a
        # deterministic event identity. Global ``event_lifecycle`` frames do
        # not use this compatibility rule and remain diagnostic-only.
        derived_event_ticker, separator, _contract = ticker.rpartition("-")
        return CanonicalSdkEvent(
            asset=asset,
            ticker=ticker,
            event_type=CanonicalEventType.LIFECYCLE,
            sequence=seq,
            subscription_id=sid,
            connection_id=connection_id,
            sdk_receive_timestamp=received,
            exchange_timestamp=_source_timestamp(payload),
            lifecycle_type=lifecycle,
            lifecycle_result=lifecycle_result,
            event_ticker=(explicit_event_ticker or (derived_event_ticker if separator else ticker)),
            exchange_index=exchange_index,
        )
    raise ValueError("unsupported SDK WebSocket typed message")


def reconnect_event(
    *,
    asset: Asset,
    ticker: str,
    connection_id: str,
    observed_at: datetime,
    old_state: str,
    new_state: str,
) -> CanonicalSdkEvent:
    return CanonicalSdkEvent(
        asset=asset,
        ticker=ticker,
        event_type=CanonicalEventType.RECONNECT,
        sequence=None,
        subscription_id=None,
        connection_id=connection_id,
        sdk_receive_timestamp=_aware(observed_at, "reconnect timestamp"),
        diagnostic=f"{old_state.lower()}->{new_state.lower()}",
    )


def unknown_lifecycle_event(
    *,
    asset: Asset,
    ticker: str,
    connection_id: str,
    observed_at: datetime,
    wire_type: str,
) -> CanonicalSdkEvent:
    if not wire_type or "lifecycle" not in wire_type.lower():
        raise ValueError("unknown lifecycle diagnostic is invalid")
    return CanonicalSdkEvent(
        asset=asset,
        ticker=ticker,
        event_type=CanonicalEventType.UNKNOWN_LIFECYCLE,
        sequence=None,
        subscription_id=None,
        connection_id=connection_id,
        sdk_receive_timestamp=_aware(observed_at, "unknown lifecycle timestamp"),
        diagnostic=wire_type[:120],
    )


def invalid_payload_event(
    *,
    asset: Asset,
    ticker: str,
    connection_id: str,
    observed_at: datetime,
    subscription_id: int | None,
    sequence: int | None,
    diagnostic: str,
) -> CanonicalSdkEvent:
    return CanonicalSdkEvent(
        asset=asset,
        ticker=ticker,
        event_type=CanonicalEventType.PAYLOAD_INVALID,
        sequence=sequence,
        subscription_id=subscription_id,
        connection_id=connection_id,
        sdk_receive_timestamp=_aware(observed_at, "invalid payload timestamp"),
        diagnostic=diagnostic[:120],
    )
