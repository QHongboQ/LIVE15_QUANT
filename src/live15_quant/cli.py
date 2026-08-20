"""Command-line entry points for public market-data collectors."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from collections.abc import Sequence
from datetime import timedelta

import requests

from live15_quant.config import Settings, load_settings
from live15_quant.dataset import (
    DatasetBuildConfig,
    DatasetBuilder,
    DatasetBuildSummary,
    FeatureStore,
)
from live15_quant.features import SamplingPolicy
from live15_quant.kalshi_lifecycle import KalshiNativeMarketProvider
from live15_quant.logging_config import configure_logging
from live15_quant.models import Asset, MarketTick
from live15_quant.native_recorder import KalshiNativeRecorder
from live15_quant.paper_runtime import PaperRuntime
from live15_quant.paper_storage import PaperStore
from live15_quant.providers.coinbase import (
    CoinbasePayloadError,
    CoinbaseRestClient,
    CoinbaseWebSocketClient,
)
from live15_quant.providers.kalshi import KALSHI_15MIN_SERIES, KalshiOfficialQuoteProvider
from live15_quant.providers.kalshi_demo import (
    KalshiDemoCredentials,
    KalshiDemoReadOnlyClient,
)
from live15_quant.storage import RecorderStore

logger = logging.getLogger(__name__)


def _tick_fields(tick: MarketTick) -> dict[str, object]:
    return {
        "event": "market_tick",
        "source": "coinbase",
        "symbol": tick.symbol,
        "price": tick.price,
        "bid": tick.bid,
        "ask": tick.ask,
        "spread": tick.spread,
        "bid_size": tick.bid_size,
        "ask_size": tick.ask_size,
        "last_size": tick.last_size,
        "volume_24h": tick.volume_24h,
        "exchange_time": tick.exchange_time,
        "received_at": tick.received_at,
    }


def rest_main() -> None:
    """Poll the public BTC-USD REST ticker until interrupted."""

    settings = load_settings()
    configure_logging(settings.log_level)
    client = CoinbaseRestClient(settings)
    logger.info("REST collector started", extra={"event": "collector_started", "symbol": "BTC-USD"})
    try:
        while True:
            try:
                tick = client.get_ticker("BTC-USD")
                logger.info("Coinbase market tick", extra=_tick_fields(tick))
            except (requests.RequestException, CoinbasePayloadError):
                logger.exception(
                    "Coinbase REST poll failed", extra={"event": "coinbase_rest_error"}
                )
            time.sleep(settings.rest_poll_interval_seconds)
    except KeyboardInterrupt:
        logger.info("REST collector stopped", extra={"event": "collector_stopped"})
    finally:
        client.close()


async def _stream(settings: Settings, products: tuple[str, ...]) -> None:
    client = CoinbaseWebSocketClient(settings, products=products)
    async for tick in client.ticks():
        logger.info("Coinbase market tick", extra=_tick_fields(tick))


def _run_stream(settings: Settings, products: tuple[str, ...]) -> None:
    configure_logging(settings.log_level)
    logger.info(
        "WebSocket collector started",
        extra={"event": "collector_started", "products": products},
    )
    try:
        asyncio.run(_stream(settings, products))
    except KeyboardInterrupt:
        logger.info("WebSocket collector stopped", extra={"event": "collector_stopped"})


def stream_main() -> None:
    """Stream all configured Coinbase products."""

    settings = load_settings()
    _run_stream(settings, settings.products)


def btc_stream_main() -> None:
    """Stream only BTC-USD for backward compatibility."""

    settings = load_settings()
    _run_stream(settings, ("BTC-USD",))


def discover_main() -> None:
    """Discover current official Kalshi 15-minute markets without Robinhood."""

    settings = load_settings()
    configure_logging(settings.log_level)
    client = KalshiOfficialQuoteProvider(settings)
    provider = KalshiNativeMarketProvider(client)
    try:
        discoveries = provider.discover_all()
    finally:
        client.close()
    for discovery in discoveries:
        market = discovery.current
        logger.info(
            "Kalshi-native 15-minute discovery",
            extra={
                "event": "kalshi_native_discovery",
                "asset": discovery.asset,
                "series": KALSHI_15MIN_SERIES[discovery.asset],
                "ticker": market.ticker if market is not None else None,
                "event_ticker": market.event_ticker if market is not None else None,
                "start_time": market.window_start if market is not None else None,
                "end_time": market.window_end if market is not None else None,
                "target": market.target if market is not None else None,
                "lifecycle": market.lifecycle if market is not None else None,
                "next_ticker": discovery.next.ticker if discovery.next is not None else None,
                "rejected_tickers": discovery.rejected_tickers,
            },
        )


async def _run_recorder(settings: Settings) -> None:
    with RecorderStore(settings.recorder_data_path) as store:
        recorder = KalshiNativeRecorder(settings, store)
        if settings.dataset_build_interval_seconds is None:
            await recorder.run()
            return
        recorder_task = asyncio.create_task(recorder.run())
        snapshot_task = asyncio.create_task(_periodic_dataset_build(settings))
        try:
            await recorder_task
        finally:
            snapshot_task.cancel()
            await asyncio.gather(snapshot_task, return_exceptions=True)


async def _periodic_dataset_build(settings: Settings) -> None:
    assert settings.dataset_build_interval_seconds is not None
    while True:
        await asyncio.sleep(settings.dataset_build_interval_seconds)
        try:
            summary = await asyncio.to_thread(_build_dataset, settings)
            logger.info(
                "Periodic dataset snapshot completed",
                extra={
                    "event": "periodic_dataset_complete",
                    "build_id": summary.build_id,
                    "events": summary.events,
                    "rows": summary.rows,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception(
                "Periodic dataset snapshot failed; raw collection continues",
                extra={
                    "event": "periodic_dataset_error",
                    "error_type": type(error).__name__,
                },
            )


def _parse_no_args(
    program: str,
    description: str,
    argv: Sequence[str] | None,
) -> None:
    argparse.ArgumentParser(prog=program, description=description).parse_args(argv)


def recorder_main(argv: Sequence[str] | None = None) -> None:
    """Continuously persist public event snapshots and predictive ticks."""

    _parse_no_args("live15-record", recorder_main.__doc__ or "", argv)
    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        asyncio.run(_run_recorder(settings))
    except KeyboardInterrupt:
        logger.info("Recorder interrupted safely", extra={"event": "recorder_interrupted"})


def paper_main() -> None:
    """Run local-only paper execution; this entry point cannot place real orders."""

    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        with PaperStore(
            settings.paper_data_path,
            account_id=settings.paper_account_id,
            starting_cash=settings.paper_starting_cash,
        ) as store:
            PaperRuntime(settings, store).run()
    except KeyboardInterrupt:
        logger.info("Paper runtime interrupted safely", extra={"event": "paper_interrupted"})


def kalshi_demo_audit_main() -> None:
    """Run the credentialed, GET-only Kalshi Demo connectivity audit."""

    settings = load_settings()
    configure_logging(settings.log_level)
    if settings.kalshi_demo_api_key_id is None or settings.kalshi_demo_private_key_path is None:
        raise SystemExit(
            "Kalshi Demo credentials are not configured. Create a Demo API key, keep its "
            "private key outside the repository, then set LIVE15_KALSHI_DEMO_API_KEY_ID and "
            "LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH."
        )
    credentials = KalshiDemoCredentials(
        api_key_id=settings.kalshi_demo_api_key_id,
        private_key_path=settings.kalshi_demo_private_key_path,
    )
    with KalshiDemoReadOnlyClient(settings, credentials) as client:
        result = client.audit()
    logger.info(
        "Kalshi Demo read-only connectivity audit completed",
        extra={
            "event": "kalshi_demo_audit_complete",
            "environment": result.environment,
            "authenticated": result.authenticated,
            "balance_read": result.balance_dollars is not None,
            "market_count": result.market_count,
            "positions_readable": result.positions_readable,
            "orders_readable": result.orders_readable,
            "fills_readable": result.fills_readable,
            "write_operations_available_in_client": False,
        },
    )


def _build_dataset(settings: Settings) -> DatasetBuildSummary:
    policy = SamplingPolicy(
        tuple(timedelta(seconds=value) for value in settings.dataset_decision_offsets_seconds),
        quote_max_age=timedelta(seconds=settings.dataset_quote_max_age_seconds),
        underlying_max_age=timedelta(seconds=settings.dataset_underlying_max_age_seconds),
    )
    with (
        RecorderStore(settings.recorder_data_path) as source,
        FeatureStore(settings.feature_store_path) as destination,
    ):
        return DatasetBuilder(source, destination).build(DatasetBuildConfig(policy))


def dataset_main() -> None:
    """Build or resume a deterministic training dataset from the raw recorder store."""

    settings = load_settings()
    configure_logging(settings.log_level)
    summary = _build_dataset(settings)
    print(
        json.dumps(
            {
                "build_id": summary.build_id,
                "complete": summary.complete,
                "events": summary.events,
                "rows": summary.rows,
                "rows_written": summary.rows_written,
                "skipped_decisions": summary.skipped_decisions,
                "diagnostics": summary.diagnostics,
            },
            indent=2,
            sort_keys=True,
        )
    )


def coverage_main(argv: Sequence[str] | None = None) -> None:
    """Build a consistent snapshot and print machine-readable training coverage."""

    _parse_no_args("live15-coverage", coverage_main.__doc__ or "", argv)
    settings = load_settings()
    configure_logging(settings.log_level)
    summary = _build_dataset(settings)
    with (
        RecorderStore(settings.recorder_data_path) as source,
        FeatureStore(settings.feature_store_path) as destination,
    ):
        finalized = source.count("kalshi_settlements")
        finalized_by_asset = source.settlement_counts_by_asset()
        trainable_by_asset = destination.coverage_by_asset(summary.build_id)
        integrity = source.integrity_check()
    print(
        json.dumps(
            {
                "finalized_events": finalized,
                "trainable_events": summary.events,
                "training_rows": summary.rows,
                "rows_written": summary.rows_written,
                "skipped_decisions": summary.skipped_decisions,
                "per_asset": {
                    asset.value: {
                        "finalized_events": finalized_by_asset[asset],
                        "trainable_events": trainable_by_asset[asset][0],
                        "training_rows": trainable_by_asset[asset][1],
                    }
                    for asset in Asset
                },
                "label_balance": summary.diagnostics["label_balance"],
                "decision_time_bucket_coverage": summary.diagnostics[
                    "rows_per_decision_bucket_seconds"
                ],
                "missing_feature_rates": summary.diagnostics["missing_feature_rates"],
                "stale_feature_rates": summary.diagnostics["stale_feature_rates"],
                "raw_store_integrity": integrity,
                "build_id": summary.build_id,
            },
            indent=2,
            sort_keys=True,
        )
    )


def status_main(argv: Sequence[str] | None = None) -> None:
    """Print the last atomic recorder heartbeat without opening the writer database."""

    _parse_no_args("live15-status", status_main.__doc__ or "", argv)
    settings = load_settings()
    try:
        payload = json.loads(settings.recorder_health_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SystemExit(
            "Recorder health file does not exist; start live15-record first"
        ) from error
    print(json.dumps(payload, indent=2, sort_keys=True))
