"""Bounded read-only diagnostics for primary/secondary predictive sources."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from live15_quant.models import Asset, UnderlyingProvider


@dataclass(frozen=True, slots=True)
class SourceSeriesDiagnostics:
    observations: int
    median_cadence_seconds: Decimal | None
    p95_cadence_seconds: Decimal | None
    max_gap_seconds: Decimal | None
    latest_age_seconds: Decimal | None
    stale: bool
    median_source_receive_latency_ms: Decimal | None
    p95_source_receive_latency_ms: Decimal | None
    source_clock_skew_observations: int
    source_clock_skew_detected: bool
    median_receive_persist_latency_ms: Decimal | None


@dataclass(frozen=True, slots=True)
class AssetSourceComparison:
    asset: Asset
    primary_provider: UnderlyingProvider
    secondary_provider: UnderlyingProvider
    primary: SourceSeriesDiagnostics
    secondary: SourceSeriesDiagnostics
    latest_secondary_minus_primary: Decimal | None
    latest_secondary_minus_primary_bps: Decimal | None
    latest_secondary_minus_primary_age_seconds: Decimal | None
    median_secondary_minus_primary_cadence_seconds: Decimal | None
    semantics_note: str

    def as_dict(self) -> dict[str, object]:
        def serialize(value: object) -> object:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, (Asset, UnderlyingProvider)):
                return value.value
            if isinstance(value, dict):
                return {key: serialize(item) for key, item in value.items()}
            return value

        return serialize(asdict(self))  # type: ignore[return-value]


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _seconds(start: datetime, end: datetime) -> Decimal:
    delta = end - start
    micros = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    return Decimal(micros) / Decimal(1_000_000)


def _percentile(values: list[Decimal], percentile: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, (len(ordered) * int(percentile) + 99) // 100 - 1)
    return ordered[min(index, len(ordered) - 1)]


def _series(
    rows: list[sqlite3.Row], now: datetime, stale_seconds: Decimal
) -> SourceSeriesDiagnostics:
    times = [datetime.fromisoformat(str(row["received_timestamp"])).astimezone(UTC) for row in rows]
    gaps = [_seconds(previous, current) for previous, current in pairwise(times)]
    age = _seconds(times[-1], now) if times else None
    source_receive: list[Decimal] = []
    receive_persist: list[Decimal] = []
    for row in rows:
        if "source_receive_latency_ms" in row.keys():
            source_receive.append(Decimal(str(row["source_receive_latency_ms"])))
            if row["receive_persist_latency_ms"] is not None:
                receive_persist.append(Decimal(str(row["receive_persist_latency_ms"])))
        else:
            source = datetime.fromisoformat(str(row["source_timestamp"])).astimezone(UTC)
            received = datetime.fromisoformat(str(row["received_timestamp"])).astimezone(UTC)
            source_receive.append(_seconds(source, received) * Decimal(1000))
    return SourceSeriesDiagnostics(
        observations=len(rows),
        median_cadence_seconds=_percentile(gaps, Decimal(50)),
        p95_cadence_seconds=_percentile(gaps, Decimal(95)),
        max_gap_seconds=max(gaps) if gaps else None,
        latest_age_seconds=age,
        stale=age is None or age > stale_seconds,
        median_source_receive_latency_ms=_percentile(source_receive, Decimal(50)),
        p95_source_receive_latency_ms=_percentile(source_receive, Decimal(95)),
        source_clock_skew_observations=sum(value < 0 for value in source_receive),
        source_clock_skew_detected=any(value < 0 for value in source_receive),
        median_receive_persist_latency_ms=_percentile(receive_persist, Decimal(50)),
    )


def build_secondary_diagnostics(
    path: Path,
    *,
    now: datetime | None = None,
    lookback: timedelta = timedelta(minutes=5),
    limit_per_source: int = 10_000,
    stale_seconds: Decimal = Decimal(10),
) -> tuple[AssetSourceComparison, ...]:
    """Compare independent raw sources without writing or synthesizing a price."""

    observed = (now or datetime.now(UTC)).astimezone(UTC)
    if lookback <= timedelta(0) or not 1 <= limit_per_source <= 100_000:
        raise ValueError("diagnostic lookback and limit must be positive and bounded")
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        results: list[AssetSourceComparison] = []
        for asset, secondary_provider in (
            (Asset.BNB, UnderlyingProvider.BINANCE_SPOT),
            (Asset.HYPE, UnderlyingProvider.HYPERLIQUID_PERP),
        ):
            primary_rows = list(
                connection.execute(
                    """SELECT * FROM (
                        SELECT price,source_timestamp,received_timestamp,id
                        FROM underlying_observations
                        WHERE asset=? AND provider=?
                        AND received_timestamp>=? AND received_timestamp<=?
                        ORDER BY received_timestamp DESC,id DESC LIMIT ?
                    ) ORDER BY received_timestamp ASC,id ASC""",
                    (
                        asset.value,
                        UnderlyingProvider.PYTH_HERMES.value,
                        _timestamp(observed - lookback),
                        _timestamp(observed),
                        limit_per_source,
                    ),
                )
            )
            secondary_rows = list(
                connection.execute(
                    """SELECT * FROM (
                        SELECT price,source_timestamp,received_timestamp,
                        source_receive_latency_ms,receive_persist_latency_ms,id
                        FROM secondary_underlying_observations
                        WHERE asset=? AND provider=?
                        AND received_timestamp>=? AND received_timestamp<=?
                        ORDER BY received_timestamp DESC,id DESC LIMIT ?
                    ) ORDER BY received_timestamp ASC,id ASC""",
                    (
                        asset.value,
                        secondary_provider.value,
                        _timestamp(observed - lookback),
                        _timestamp(observed),
                        limit_per_source,
                    ),
                )
            )
            primary_price = Decimal(primary_rows[-1]["price"]) if primary_rows else None
            secondary_price = Decimal(secondary_rows[-1]["price"]) if secondary_rows else None
            difference = (
                secondary_price - primary_price
                if secondary_price is not None and primary_price is not None
                else None
            )
            age_difference = None
            if primary_rows and secondary_rows:
                primary_received = datetime.fromisoformat(
                    str(primary_rows[-1]["received_timestamp"])
                ).astimezone(UTC)
                secondary_received = datetime.fromisoformat(
                    str(secondary_rows[-1]["received_timestamp"])
                ).astimezone(UTC)
                age_difference = _seconds(secondary_received, primary_received)
            primary_diagnostics = _series(primary_rows, observed, stale_seconds)
            secondary_diagnostics = _series(secondary_rows, observed, stale_seconds)
            primary_cadence = primary_diagnostics.median_cadence_seconds
            secondary_cadence = secondary_diagnostics.median_cadence_seconds
            results.append(
                AssetSourceComparison(
                    asset=asset,
                    primary_provider=UnderlyingProvider.PYTH_HERMES,
                    secondary_provider=secondary_provider,
                    primary=primary_diagnostics,
                    secondary=secondary_diagnostics,
                    latest_secondary_minus_primary=difference,
                    latest_secondary_minus_primary_bps=(
                        difference / primary_price * Decimal(10_000)
                        if difference is not None and primary_price not in {None, Decimal(0)}
                        else None
                    ),
                    latest_secondary_minus_primary_age_seconds=age_difference,
                    median_secondary_minus_primary_cadence_seconds=(
                        secondary_cadence - primary_cadence
                        if secondary_cadence is not None and primary_cadence is not None
                        else None
                    ),
                    semantics_note=(
                        "Binance aggregate trade versus Pyth aggregate BNB/USD"
                        if asset is Asset.BNB
                        else "Hyperliquid perpetual BBO midpoint versus Pyth aggregate HYPE/USD"
                    ),
                )
            )
        return tuple(results)
    finally:
        connection.close()
