"""Command-line entry points for public market-data collectors."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import requests

from live15_quant.adaptive_retention import (
    AdaptiveRetentionController,
    AdaptiveRetentionObservation,
    AdaptiveRetentionPolicy,
    write_adaptive_retention_status,
)
from live15_quant.certified_dataset import CertifiedDatasetV1Builder, DatasetV1Config
from live15_quant.config import Settings, load_settings
from live15_quant.dataset import (
    DatasetBuildConfig,
    DatasetBuilder,
    DatasetBuildSummary,
    FeatureStore,
)
from live15_quant.demo_execution import (
    DemoExecutionCoordinator,
    DemoExecutionStore,
    DemoIntent,
    DemoIntentPurpose,
    DemoReconciliationResult,
    DemoRiskContext,
    SqliteKalshiWsQuoteSource,
)
from live15_quant.demo_first_fill import main as demo_first_fill_runtime_main
from live15_quant.features import SamplingPolicy
from live15_quant.forward_shadow import ForwardShadowRuntime
from live15_quant.kalshi_lifecycle import KalshiNativeMarketProvider
from live15_quant.latency_benchmark import LowLatencyBenchmarkRunner
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
    KalshiDemoReadOnlyClient,
    resolve_kalshi_demo_credentials,
)
from live15_quant.providers.kalshi_demo_execution import DemoBookSide, KalshiDemoExecutionClient
from live15_quant.providers.low_latency import (
    BenchmarkSource,
    BinanceBnbBenchmarkSource,
    HyperliquidHypeBenchmarkSource,
    PythCoreBenchmarkSource,
    PythProBenchmarkSource,
)
from live15_quant.providers.pyth import PythHermesClient
from live15_quant.readiness import build_readiness_report, snapshot_database, write_report_atomic
from live15_quant.recorder_control import (
    ManagedRecorderState,
    RecorderPidLease,
    RecorderProcessController,
)
from live15_quant.secondary_diagnostics import build_secondary_diagnostics
from live15_quant.sqlite_attribution import attribute_sqlite_snapshot
from live15_quant.storage import RecorderStore
from live15_quant.storage_scaling import benchmark_snapshot
from live15_quant.ws_retention import (
    ArchiveState,
    CompactionBenefitGate,
    DiskQuota,
    WsArchiveService,
    WsPurgeService,
    WsRetentionManifest,
    checkpoint_stopped_database,
    compact_database_offline,
    evaluate_database_compaction,
)

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


async def _watch_recorder_control(
    recorder: KalshiNativeRecorder, controller: RecorderProcessController
) -> None:
    while True:
        if controller.desired_state() == "paused":
            recorder.request_stop()
            return
        await asyncio.sleep(0.25)


async def _run_recorder(
    settings: Settings, controller: RecorderProcessController | None = None
) -> None:
    with RecorderStore(settings.recorder_data_path) as store:
        recorder = KalshiNativeRecorder(
            settings,
            store,
            controlled_pause=(
                None
                if controller is None
                else lambda reason: controller.write_child_state(
                    "paused",
                    ManagedRecorderState.STOPPING,
                    f"controlled storage pause: {reason}",
                )
            ),
        )
        control_task = (
            asyncio.create_task(_watch_recorder_control(recorder, controller))
            if controller is not None
            else None
        )
        if controller is not None:
            controller.write_child_state(
                "running", ManagedRecorderState.RUNNING, "recorder is running"
            )
        if settings.dataset_build_interval_seconds is None:
            try:
                await recorder.run()
            finally:
                if control_task is not None:
                    control_task.cancel()
                    await asyncio.gather(control_task, return_exceptions=True)
            return
        recorder_task = asyncio.create_task(recorder.run())
        snapshot_task = asyncio.create_task(_periodic_dataset_build(settings))
        try:
            await recorder_task
        finally:
            if control_task is not None:
                control_task.cancel()
            snapshot_task.cancel()
            await asyncio.gather(
                snapshot_task,
                *(item for item in (control_task,) if item is not None),
                return_exceptions=True,
            )


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
        controller = RecorderProcessController(settings)
    except ValueError:
        controller = None
    try:
        with RecorderPidLease(settings.recorder_pid_path):
            asyncio.run(_run_recorder(settings, controller))
    except KeyboardInterrupt:
        logger.info("Recorder interrupted safely", extra={"event": "recorder_interrupted"})
    finally:
        if controller is not None:
            desired = controller.desired_state()
            state = (
                ManagedRecorderState.PAUSED if desired == "paused" else ManagedRecorderState.STOPPED
            )
            controller.write_child_state(
                desired,
                state,
                "collection is paused" if desired == "paused" else "recorder stopped",
            )


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


def paper_shadow_main(argv: Sequence[str] | None = None) -> None:
    """Run bounded, local-only frozen-candidate forward paper/shadow validation."""

    parser = argparse.ArgumentParser(
        prog="live15-paper-shadow", description=paper_shadow_main.__doc__
    )
    parser.add_argument("--once", action="store_true", help="process only currently due decisions")
    parser.add_argument("--duration-seconds", type=float, default=None)
    parser.add_argument(
        "--materialize-frozen-models",
        action="store_true",
        help="one-time Train-only immutable materialization of approved v2 candidates",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="read current features without creating a forward decision",
    )
    arguments = parser.parse_args(argv)
    if arguments.duration_seconds is not None and arguments.duration_seconds <= 0:
        parser.error("--duration-seconds must be positive")
    settings = load_settings()
    configure_logging(settings.log_level)
    with ForwardShadowRuntime(
        settings, allow_model_materialization=arguments.materialize_frozen_models
    ) as runtime:
        if arguments.probe:
            logger.info(
                "Forward shadow probe", extra={"event": "forward_shadow_probe", **runtime.probe()}
            )
            return
        started = time.monotonic()
        while True:
            summary = runtime.run_once()
            logger.info("Forward shadow cycle", extra={"event": "forward_shadow_cycle", **summary})
            if arguments.once or (
                arguments.duration_seconds is not None
                and time.monotonic() - started >= arguments.duration_seconds
            ):
                return
            time.sleep(settings.forward_shadow_poll_interval_seconds)


def kalshi_demo_audit_main() -> None:
    """Run the credentialed, GET-only Kalshi Demo connectivity audit."""

    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        credentials = resolve_kalshi_demo_credentials(settings)
    except Exception as error:
        raise SystemExit("DEMO_CREDENTIAL_UNAVAILABLE") from error
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


def _recent_forward_demo_intents(settings: Settings, *, now: datetime) -> tuple[DemoIntent, ...]:
    """Read a bounded set of fresh, genuine forward-model signals for Demo smoke only."""

    cutoff = now - timedelta(seconds=30)
    connection = sqlite3.connect(
        f"file:{settings.forward_shadow_data_path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=2,
    )
    try:
        rows = connection.execute(
            """SELECT model_id,opportunity_id,decision_timestamp,ticker,prediction,
                      yes_ask,no_ask,yes_edge,no_edge,action,model_artifact_hash
               FROM forward_decisions
               WHERE action IN ('buy_yes','buy_no')
               ORDER BY id DESC LIMIT 64"""
        ).fetchall()
    finally:
        connection.close()
    intents: list[DemoIntent] = []
    for row in rows:
        try:
            decision_timestamp = datetime.fromisoformat(str(row[2]).replace("Z", "+00:00"))
            if decision_timestamp.tzinfo is None or decision_timestamp < cutoff:
                continue
            action = str(row[9])
            side = DemoBookSide.BID if action == "buy_yes" else DemoBookSide.ASK
            price = Decimal(str(row[5] if side is DemoBookSide.BID else row[6]))
            edge = Decimal(str(row[7] if side is DemoBookSide.BID else row[8]))
            probability = Decimal(str(row[4]))
            if price <= 0 or edge <= 0:
                continue
            intents.append(
                DemoIntent(
                    model_id=str(row[0]),
                    model_artifact_hash=str(row[10]),
                    decision_id=f"demo-diagnostic:{row[0]}:{row[1]}",
                    event_id=str(row[3]),
                    opportunity_id=str(row[1]),
                    ticker=str(row[3]),
                    side=side,
                    count=Decimal("1"),
                    price=price,
                    probability=probability,
                    edge=edge,
                    decision_timestamp=decision_timestamp.astimezone(UTC),
                    purpose=DemoIntentPurpose.EXECUTION_SMOKE,
                )
            )
        except (TypeError, ValueError, ArithmeticError):
            continue
    return tuple(intents)


def demo_diagnostic_watch_main(argv: Sequence[str] | None = None) -> None:
    """Bounded, one-POST Demo-only watch using fresh frozen-model signals only."""

    parser = argparse.ArgumentParser(prog="live15-demo-diagnostic-watch")
    parser.add_argument("--duration-seconds", type=int, default=900)
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--approved-diagnostic-post", action="store_true")
    arguments = parser.parse_args(argv)
    if not arguments.approved_diagnostic_post:
        raise SystemExit("EXPLICIT_DIAGNOSTIC_POST_APPROVAL_REQUIRED")
    if not 1 <= arguments.duration_seconds <= 900 or not 1 <= arguments.poll_seconds <= 60:
        raise SystemExit("duration must be 1..900 seconds and poll interval must be 1..60 seconds")
    settings = load_settings()
    try:
        credentials = resolve_kalshi_demo_credentials(settings)
    except Exception as error:
        raise SystemExit("DEMO_CREDENTIAL_UNAVAILABLE") from error
    source = SqliteKalshiWsQuoteSource(settings.recorder_data_path, settings.recorder_health_path)
    deadline = time.monotonic() + arguments.duration_seconds
    with KalshiDemoExecutionClient(settings, credentials, repository_root=Path.cwd()) as client:
        with DemoExecutionStore(Path("data/demo-execution.sqlite3")) as store:
            coordinator = DemoExecutionCoordinator(
                client,
                store,
                quote_source=source,
                writes_enabled=True,
                execution_smoke_approved=True,
            )
            while time.monotonic() < deadline:
                for intent in _recent_forward_demo_intents(settings, now=datetime.now(UTC)):
                    result = coordinator.submit(
                        intent,
                        DemoRiskContext(
                            event_exposure=Decimal(0),
                            total_exposure=Decimal(0),
                            open_positions=0,
                            daily_realized_pnl=Decimal(0),
                            kill_switch=False,
                        ),
                    )
                    if isinstance(result, DemoReconciliationResult):
                        print(
                            json.dumps(
                                {
                                    "status": "DEMO_POST_ATTEMPTED",
                                    "ticker": intent.ticker,
                                    "model_id": intent.model_id,
                                    "state": result.state.value,
                                    "provider_order_id": result.provider_order_id,
                                    "inserted_fills": result.inserted_fills,
                                },
                                sort_keys=True,
                            )
                        )
                        return
                time.sleep(arguments.poll_seconds)
    print(json.dumps({"status": "NO_SAFE_DEMO_DIAGNOSTIC_OPPORTUNITY"}, sort_keys=True))


def demo_first_fill_main(argv: Sequence[str] | None = None) -> None:
    """Run the persistent, Demo-only single-POST first-fill certification worker."""

    demo_first_fill_runtime_main(argv)


def _build_dataset(settings: Settings, *, source_path: Path | None = None) -> DatasetBuildSummary:
    if source_path is None:
        with tempfile.TemporaryDirectory(prefix="live15-dataset-") as directory:
            snapshot = Path(directory) / "raw.sqlite3"
            snapshot_database(settings.recorder_data_path, snapshot)
            return _build_dataset(settings, source_path=snapshot)
    policy = SamplingPolicy(
        tuple(timedelta(seconds=value) for value in settings.dataset_decision_offsets_seconds),
        quote_max_age=timedelta(seconds=settings.dataset_quote_max_age_seconds),
        underlying_max_age=timedelta(seconds=settings.dataset_underlying_max_age_seconds),
    )
    with (
        RecorderStore(source_path) as source,
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


def certified_dataset_v1_main(argv: Sequence[str] | None = None) -> None:
    """Build immutable Dataset v1 from a bounded offline snapshot; never scan the active DB."""

    parser = argparse.ArgumentParser(prog="live15-dataset-v1")
    parser.add_argument("--output-root", type=Path, default=Path("data/datasets"))
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--archive-manifest-snapshot", type=Path)
    arguments = parser.parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)
    policy = SamplingPolicy(
        tuple(timedelta(seconds=value) for value in settings.dataset_decision_offsets_seconds),
        quote_max_age=timedelta(seconds=settings.dataset_quote_max_age_seconds),
        underlying_max_age=timedelta(seconds=settings.dataset_underlying_max_age_seconds),
    )
    config = DatasetV1Config(policy)
    if arguments.snapshot is not None:
        if arguments.snapshot.resolve() == settings.recorder_data_path.resolve():
            raise SystemExit(
                "Dataset v1 refuses the active recorder database; provide an offline snapshot"
            )
        summary = CertifiedDatasetV1Builder(
            arguments.snapshot,
            arguments.output_root,
            archive_manifest_snapshot=arguments.archive_manifest_snapshot,
            snapshot_captured_at=datetime.now(UTC),
        ).build(config)
    else:
        manifest_source = settings.ws_archive_manifest_path or (
            settings.recorder_data_path.parent / "ws_archive_manifest.sqlite3"
        )
        snapshot_parent = arguments.output_root.resolve().parent
        snapshot_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".live15-dataset-v1-snapshot-", dir=snapshot_parent
        ) as directory:
            root = Path(directory)
            raw_snapshot = root / "raw.sqlite3"
            manifest_snapshot = root / "ws-archive-manifest.sqlite3"
            snapshot_database(
                settings.recorder_data_path,
                raw_snapshot,
                max_seconds=settings.readiness_snapshot_max_seconds,
            )
            snapshot_database(manifest_source, manifest_snapshot, max_seconds=60.0)
            summary = CertifiedDatasetV1Builder(
                raw_snapshot,
                arguments.output_root,
                archive_manifest_snapshot=manifest_snapshot,
                snapshot_captured_at=datetime.now(UTC),
            ).build(config)
    print(
        json.dumps(
            {
                "dataset_id": summary.dataset_id,
                "deterministic_build_hash": summary.deterministic_build_hash,
                "events": summary.events,
                "rows": summary.rows,
                "split_events": summary.split_events,
                "split_rows": summary.split_rows,
                "reused_existing_artifact": summary.reused_existing_artifact,
                "diagnostics": summary.diagnostics,
            },
            indent=2,
            sort_keys=True,
        )
    )


def model_zoo_main(argv: Sequence[str] | None = None) -> None:
    """Train the small, immutable offline Model Zoo from an existing Dataset v1 artifact."""

    # Keep optional native ML imports out of recorder and market-data CLI startup paths.
    from live15_quant.model_zoo import ModelZooConfig, ModelZooV1, load_certified_dataset

    parser = argparse.ArgumentParser(prog="live15-model-zoo")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("data/models"))
    parser.add_argument("--seed", type=int, default=20260823)
    parser.add_argument(
        "--reproduction-only",
        action="store_true",
        help="acknowledge this Dataset artifact command is reproduction only",
    )
    arguments = parser.parse_args(argv)
    from live15_quant.research_data_authority import require_reproduction_only

    try:
        require_reproduction_only(
            reproduction_only=arguments.reproduction_only,
            entrypoint="live15-model-zoo",
        )
    except ValueError as error:
        parser.error(str(error))
    dataset = load_certified_dataset(arguments.dataset)
    summary = ModelZooV1(
        dataset,
        arguments.output_root,
        ModelZooConfig(seed=arguments.seed),
    ).build()
    print(
        json.dumps(
            {
                "zoo_id": summary.zoo_id,
                "dataset_id": summary.dataset_id,
                "status": summary.status,
                "champion_model_id": summary.champion_model_id,
                "model_ids": summary.model_ids,
                "reused_existing_artifact": summary.reused_existing_artifact,
            },
            indent=2,
            sort_keys=True,
        )
    )


def model_zoo_v2_main(argv: Sequence[str] | None = None) -> None:
    """Build development-only Model Zoo v2 candidates from Dataset v1 train rows."""

    from live15_quant.model_zoo import load_certified_dataset
    from live15_quant.model_zoo_v2 import ModelZooV2, ModelZooV2Config

    parser = argparse.ArgumentParser(prog="live15-model-zoo-v2")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--v1-model-zoo", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("data/models"))
    parser.add_argument("--seed", type=int, default=20260823)
    parser.add_argument(
        "--reproduction-only",
        action="store_true",
        help="acknowledge this Dataset artifact command is reproduction only",
    )
    arguments = parser.parse_args(argv)
    from live15_quant.research_data_authority import require_reproduction_only

    try:
        require_reproduction_only(
            reproduction_only=arguments.reproduction_only,
            entrypoint="live15-model-zoo-v2",
        )
    except ValueError as error:
        parser.error(str(error))
    summary = ModelZooV2(
        load_certified_dataset(arguments.dataset),
        arguments.output_root,
        arguments.v1_model_zoo,
        ModelZooV2Config(seed=arguments.seed),
    ).build()
    print(
        json.dumps(
            {
                "zoo_id": summary.zoo_id,
                "dataset_id": summary.dataset_id,
                "status": summary.status,
                "forward_candidate_ids": list(summary.forward_candidate_ids),
                "reused_existing_artifact": summary.reused_existing_artifact,
            },
            indent=2,
            sort_keys=True,
        )
    )


def model_v3_structured_main(argv: Sequence[str] | None = None) -> None:
    """Build the immutable train-internal v3 structured development artifact."""

    from live15_quant.model_v3_structured import V3StructuredConfig, V3StructuredDevelopment
    from live15_quant.model_zoo import load_certified_dataset

    parser = argparse.ArgumentParser(prog="live15-model-v3-structured")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("data/models"))
    parser.add_argument("--seed", type=int, default=20260823)
    arguments = parser.parse_args(argv)
    summary = V3StructuredDevelopment(
        load_certified_dataset(arguments.dataset),
        arguments.output_root,
        V3StructuredConfig(seed=arguments.seed),
    ).build()
    print(
        json.dumps(
            {
                "artifact_id": summary.artifact_id,
                "dataset_id": summary.dataset_id,
                "status": summary.status,
                "evidence_status": summary.evidence_status,
                "reused_existing_artifact": summary.reused_existing_artifact,
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
    with tempfile.TemporaryDirectory(prefix="live15-coverage-") as directory:
        snapshot = Path(directory) / "raw.sqlite3"
        snapshot_database(settings.recorder_data_path, snapshot)
        summary = _build_dataset(settings, source_path=snapshot)
        with (
            RecorderStore(snapshot) as source,
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


def readiness_main(argv: Sequence[str] | None = None) -> None:
    """Build a snapshot-consistent, machine-readable data readiness report."""

    _parse_no_args("live15-readiness", readiness_main.__doc__ or "", argv)
    settings = load_settings()
    configure_logging(settings.log_level)
    report = build_readiness_report(settings)
    write_report_atomic(report, settings.readiness_report_path)
    print(json.dumps(report, indent=2, sort_keys=True))


def latency_benchmark_main(argv: Sequence[str] | None = None) -> None:
    """Benchmark official read-only underlying streams without touching the recorder."""

    parser = argparse.ArgumentParser(prog="live15-latency-benchmark", description=__doc__)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument(
        "--venue-only",
        action="store_true",
        help="omit authenticated Pyth Core/Pro comparisons",
    )
    arguments = parser.parse_args(argv)
    settings = load_settings()
    sources: list[BenchmarkSource] = [
        BinanceBnbBenchmarkSource(),
        HyperliquidHypeBenchmarkSource(),
    ]
    if not arguments.venue_only:
        if settings.pyth_api_key_path is None:
            raise SystemExit(
                "Pyth comparison requires LIVE15_PYTH_API_KEY_PATH; use --venue-only otherwise"
            )
        sources.extend(
            (
                PythCoreBenchmarkSource(PythHermesClient(settings)),
                PythProBenchmarkSource(settings.pyth_api_key_path),
            )
        )
    report = asyncio.run(LowLatencyBenchmarkRunner(sources).run(arguments.seconds))
    print(json.dumps(report, indent=2, sort_keys=True))


def secondary_diagnostics_main(argv: Sequence[str] | None = None) -> None:
    """Print bounded primary/secondary divergence diagnostics without writing raw data."""

    parser = argparse.ArgumentParser(prog="live15-secondary-diagnostics")
    parser.add_argument("--minutes", type=float, default=5.0)
    arguments = parser.parse_args(argv)
    if not 0 < arguments.minutes <= 1440:
        raise SystemExit("--minutes must be in (0, 1440]")
    settings = load_settings()
    report = build_secondary_diagnostics(
        settings.recorder_data_path,
        lookback=timedelta(minutes=arguments.minutes),
    )
    print(json.dumps([item.as_dict() for item in report], indent=2, sort_keys=True))


def storage_audit_main(argv: Sequence[str] | None = None) -> None:
    """Attribute and benchmark an explicit fixed snapshot; active raw storage is refused."""

    parser = argparse.ArgumentParser(prog="live15-storage-audit")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--records", type=int, default=100_000)
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.records <= 1_000_000:
        raise SystemExit("--records must be in [1, 1000000]")
    settings = load_settings()
    snapshot = arguments.snapshot.resolve()
    if snapshot == settings.recorder_data_path.resolve():
        raise SystemExit("active recorder database is forbidden; supply a fixed snapshot")
    attribution = attribute_sqlite_snapshot(snapshot)
    benchmark = benchmark_snapshot(snapshot, maximum_records=arguments.records)
    print(
        json.dumps(
            {
                "snapshot": snapshot.name,
                "total_bytes": attribution.total_bytes,
                "unattributed_bytes": attribution.unattributed_pages * attribution.page_size,
                "objects": [
                    {
                        "name": item.name,
                        "type": item.object_type,
                        "table": item.table_name,
                        "entries": item.entries,
                        "bytes": item.allocated_bytes,
                        "average_bytes_per_entry": item.average_bytes_per_entry,
                    }
                    for item in attribution.objects
                ],
                "benchmark": [
                    {
                        "scheme": item.scheme,
                        "records": item.records,
                        "bytes": item.bytes_on_disk,
                        "bytes_per_record": item.bytes_per_record,
                        "write_records_per_second": item.write_records_per_second,
                        "replay_records_per_second": item.replay_records_per_second,
                        "book_hash": item.book_hash,
                    }
                    for item in benchmark
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


def ws_retention_main(argv: Sequence[str] | None = None) -> None:
    """Inspect or advance bounded verified WS retention without arbitrary paths or commands."""

    parser = argparse.ArgumentParser(prog="live15-ws-retention")
    parser.add_argument(
        "action",
        choices=("status", "archive-once", "purge-once", "quarantine-failed", "compact-copy"),
    )
    parser.add_argument("--chunk-id")
    parser.add_argument("--destination", type=Path)
    arguments = parser.parse_args(argv)
    settings = load_settings()
    root = settings.ws_archive_root or (settings.recorder_data_path.parent / "ws_archive")
    manifest_path = settings.ws_archive_manifest_path or (
        settings.recorder_data_path.parent / "ws_archive_manifest.sqlite3"
    )
    manifest = WsRetentionManifest(manifest_path)
    service = WsArchiveService(
        settings.recorder_data_path,
        root,
        manifest,
        hot_retention=timedelta(seconds=settings.ws_archive_hot_retention_seconds),
        chunk_records=settings.ws_archive_chunk_records,
    )
    if arguments.action == "status":
        print(
            json.dumps(
                {**manifest.metrics(), **service.hot_metrics()},
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
        return
    if arguments.action == "archive-once":
        print(json.dumps(asdict(service.run_once()), indent=2, default=str, sort_keys=True))
        return
    if arguments.action == "purge-once":
        result = WsPurgeService(
            settings.recorder_data_path,
            root,
            manifest,
            batch_rows=settings.ws_archive_purge_batch_rows,
        ).run_once()
        print(json.dumps(asdict(result), indent=2, default=str, sort_keys=True))
        return
    if arguments.action == "quarantine-failed":
        if not arguments.chunk_id:
            raise SystemExit("quarantine-failed requires --chunk-id")
        with manifest.maintenance_lease():
            chunk = manifest.quarantine_failed_chunk(
                arguments.chunk_id,
                now=datetime.now(UTC),
            )
        print(json.dumps(asdict(chunk), indent=2, default=str, sort_keys=True))
        return
    if arguments.destination is None:
        raise SystemExit("compact-copy requires --destination")
    benefit = evaluate_database_compaction(
        settings.recorder_data_path,
        CompactionBenefitGate(
            settings.ws_compaction_min_reclaim_bytes,
            settings.ws_compaction_min_reclaim_percent,
        ),
    )
    if not benefit.allowed:
        raise SystemExit(
            "compact-copy refused: reclaimable bytes/percent are below the configured gate"
        )
    controller = RecorderProcessController(settings)
    if controller.status().pid is not None:
        raise SystemExit("compact-copy requires the managed recorder to be paused")
    destination = arguments.destination.resolve()
    if destination.parent != settings.recorder_data_path.resolve().parent:
        raise SystemExit("compact-copy destination must stay beside the recorder database")
    checkpoint_stopped_database(settings.recorder_data_path)
    print(
        json.dumps(
            compact_database_offline(
                settings.recorder_data_path,
                destination,
                minimum_free_bytes=25 * 1024**3,
            ),
            indent=2,
            sort_keys=True,
        )
    )


def archive_maintenance_main(argv: Sequence[str] | None = None) -> None:
    """Run one scheduler-safe archive/verified-purge pass and exit without waiting."""

    parser = argparse.ArgumentParser(prog="live15-archive-maintenance")
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--max-chunks", type=int, default=1, choices=range(0, 4))
    parser.add_argument("--max-purge-batches", type=int, default=8, choices=range(0, 101))
    arguments = parser.parse_args(argv)
    settings = load_settings()
    root = settings.ws_archive_root or (settings.recorder_data_path.parent / "ws_archive")
    manifest_path = settings.ws_archive_manifest_path or (
        settings.recorder_data_path.parent / "ws_archive_manifest.sqlite3"
    )
    manifest = WsRetentionManifest(manifest_path)
    service = WsArchiveService(
        settings.recorder_data_path,
        root,
        manifest,
        hot_retention=timedelta(seconds=settings.ws_archive_hot_retention_seconds),
        chunk_records=settings.ws_archive_chunk_records,
    )
    before = service.eligibility()
    storage_before = manifest.storage_metrics(settings.recorder_data_path)
    results = []
    if before.status == "ELIGIBLE":
        for _ in range(arguments.max_chunks):
            eligibility = service.eligibility()
            if eligibility.status != "ELIGIBLE":
                break
            result = service.run_once()
            if result.chunk is None:
                break
            results.append(asdict(result))
    purge = WsPurgeService(
        settings.recorder_data_path,
        root,
        manifest,
        batch_rows=settings.ws_archive_purge_batch_rows,
    )
    purge_results = []
    for _ in range(arguments.max_purge_batches):
        if not manifest.chunks(ArchiveState.PURGE_ELIGIBLE):
            break
        purge_result = purge.run_once()
        if purge_result.chunk_id is None or purge_result.deleted_events == 0:
            break
        purge_results.append(asdict(purge_result))
    after = service.eligibility()
    storage_after = manifest.storage_metrics(settings.recorder_data_path)
    storage_growth = manifest.record_storage_sample(storage_after)
    purged_chunk_ids = tuple(dict.fromkeys(item["chunk_id"] for item in purge_results))
    if not purged_chunk_ids and arguments.max_purge_batches:
        purged_chunk_ids = tuple(
            chunk.chunk_id
            for chunk in manifest.chunks(ArchiveState.PURGED)[-arguments.max_purge_batches :]
        )
    post_purge_verified = []
    if purged_chunk_ids:
        chunks_by_id = {chunk.chunk_id: chunk for chunk in manifest.chunks()}
        for chunk_id in purged_chunk_ids:
            chunk = chunks_by_id[chunk_id]
            if chunk.state is not ArchiveState.PURGED:
                continue
            purge.verify_preserved_archive(chunk)
            post_purge_verified.append(chunk_id)
    compaction = evaluate_database_compaction(
        settings.recorder_data_path,
        CompactionBenefitGate(
            settings.ws_compaction_min_reclaim_bytes,
            settings.ws_compaction_min_reclaim_percent,
        ),
    )
    print(
        json.dumps(
            {
                "status": (
                    "ARCHIVED_AND_PURGED"
                    if results and purge_results
                    else "ARCHIVED"
                    if results
                    else "PURGED"
                    if purge_results
                    else before.status
                ),
                "before": asdict(before),
                "after": asdict(after),
                "archived_chunks": results,
                "manifest": manifest.metrics(),
                "purge_attempted": bool(arguments.max_purge_batches),
                "purge_batches": purge_results,
                "purged_rows": sum(item["deleted_events"] for item in purge_results),
                "purge_transaction_seconds_max": max(
                    (item["transaction_seconds"] for item in purge_results), default=0.0
                ),
                "purge_reusable_bytes_created": sum(
                    item["reusable_bytes_increase"] for item in purge_results
                ),
                "post_purge_verified_chunks": post_purge_verified,
                "storage_before": asdict(storage_before),
                "storage_after": asdict(storage_after),
                "storage_growth": asdict(storage_growth),
                "compaction_gate": asdict(compaction),
                "compaction_attempted": False,
            },
            indent=2,
            default=str,
            sort_keys=True,
        )
    )


def adaptive_retention_main(argv: Sequence[str] | None = None) -> None:
    """Observe bounded retention metadata once, persist the decision, and exit."""

    parser = argparse.ArgumentParser(prog="live15-adaptive-retention")
    parser.add_argument("--once", action="store_true", required=True)
    arguments = parser.parse_args(argv)
    del arguments
    settings = load_settings()
    manifest_path = settings.ws_archive_manifest_path or (
        settings.recorder_data_path.parent / "ws_archive_manifest.sqlite3"
    )
    manifest = WsRetentionManifest(manifest_path)
    metrics = manifest.metrics()
    storage = manifest.storage_metrics(settings.recorder_data_path)
    disk = shutil.disk_usage(settings.recorder_data_path.parent)
    disk_state = DiskQuota().classify(total_bytes=disk.total, free_bytes=disk.free)
    health: dict[str, object] = {}
    if settings.recorder_health_path.is_file():
        try:
            value = json.loads(settings.recorder_health_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                health = value
        except (OSError, json.JSONDecodeError):
            health = {}
    policy = AdaptiveRetentionPolicy(
        minimum_seconds=settings.adaptive_retention_min_seconds,
        maximum_seconds=settings.adaptive_retention_max_seconds,
        evidence_window=timedelta(seconds=settings.adaptive_retention_evidence_window_seconds),
        minimum_evidence_duration=timedelta(
            seconds=settings.adaptive_retention_min_evidence_seconds
        ),
        minimum_verified_chunks=settings.adaptive_retention_min_verified_chunks,
        minimum_evidence_samples=settings.adaptive_retention_min_evidence_samples,
        minimum_recovery_sessions=settings.adaptive_retention_min_recovery_sessions,
        minimum_simulation_passes=settings.adaptive_retention_simulation_passes,
        safety_margin=timedelta(seconds=settings.adaptive_retention_safety_margin_seconds),
        cooldown=timedelta(seconds=settings.adaptive_retention_cooldown_seconds),
        reevaluation_interval=timedelta(seconds=settings.adaptive_retention_reevaluation_seconds),
        serious_incident_quiet_period=timedelta(
            seconds=settings.adaptive_retention_incident_quiet_seconds
        ),
        minimum_projection_window=timedelta(
            seconds=settings.adaptive_retention_min_projection_window_seconds
        ),
        disk_deescalation_samples=settings.adaptive_retention_disk_deescalation_samples,
        auto_adjust=settings.adaptive_retention_auto_adjust,
    )
    controller = AdaptiveRetentionController(
        settings.adaptive_retention_state_path
        or (settings.recorder_data_path.parent / "adaptive-retention.sqlite3"),
        policy,
        initial_retention_seconds=int(settings.ws_archive_hot_retention_seconds),
    )
    source_failures = health.get("source_failures")
    health_archive = health.get("ws_archive")
    actual_retention = (
        int(health_archive.get("hot_retention_seconds") or 0)
        if isinstance(health_archive, dict)
        else controller.current_retention_seconds()
    )
    if actual_retention == 0:
        actual_retention = controller.current_retention_seconds()
    status = controller.evaluate_once(
        AdaptiveRetentionObservation(
            observed_at=datetime.now(UTC),
            verified_chunks=int(metrics.get("retention_verified") or 0),
            failed_chunks=int(metrics.get("failed") or 0),
            physical_database_bytes=storage.physical_database_bytes,
            hot_used_bytes=storage.hot_sqlite_used_bytes,
            freelist_reusable_bytes=storage.freelist_reusable_bytes,
            cold_archive_bytes=storage.cold_archive_bytes,
            cold_growth_bytes_per_day=storage.cold_archive_growth_bytes_per_day,
            raw_ws_growth_bytes_per_day=storage.raw_ws_growth_bytes_per_day,
            raw_ws_observation_window_seconds=storage.raw_ws_observation_window_seconds,
            disk_free_bytes=disk.free,
            disk_total_bytes=disk.total,
            event_loop_lag_seconds=float(health.get("event_loop_lag_seconds") or 0.0),
            ws_queue_depth=int(health.get("kalshi_ws_queue_depth") or 0),
            ws_queue_capacity=int(health.get("kalshi_ws_queue_capacity") or 0),
            ws_sequence_gaps=int(health.get("kalshi_ws_seq_gaps") or 0),
            ws_resyncs=int(health.get("kalshi_ws_resync_count") or 0),
            ws_reconnects=int(health.get("kalshi_ws_reconnect_count") or 0),
            data_gap_incidents=int(
                (health.get("row_counts") or {}).get("data_gaps", 0)
                if isinstance(health.get("row_counts"), dict)
                else 0
            ),
            unresolved_data_gaps=None,
            archive_or_replay_failure=int(metrics.get("failed") or 0) > 0,
            serious_runtime_incident=bool(
                health.get("fatal_task")
                or health.get("fatal_error_type")
                or (isinstance(source_failures, dict) and "ws_archive" in source_failures)
            ),
            disk_threshold_state=disk_state.value,
            recovery_lookback_seconds=None,
            hot_access_age_seconds=None,
            recovery_session_id=None,
            hot_access_evidence_complete=False,
        ),
        actual_retention_seconds=actual_retention,
        allow_adjustment=False,
        record_evidence=False,
    )
    write_adaptive_retention_status(
        settings.adaptive_retention_status_path
        or (settings.recorder_data_path.parent / "adaptive-retention.json"),
        status,
    )
    print(json.dumps(status.as_dict(), indent=2, sort_keys=True))
