"""Persistent, Demo-only certification worker for one guarded first-fill attempt.

The worker does not own model generation, pricing, or risk policy.  It only
observes fresh immutable forward decisions and delegates every execution gate to
the existing Demo coordinator.  A durable pre-POST reservation makes one
attempt the maximum even across a worker crash and restart.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from live15_quant.config import Settings, load_settings
from live15_quant.demo_execution import (
    DemoExecutionCoordinator,
    DemoExecutionStore,
    DemoIntent,
    DemoIntentPurpose,
    DemoPreSubmitGuardResult,
    DemoReconciliationResult,
    DemoRiskContext,
    DemoRiskDecision,
    SqliteKalshiWsQuoteSource,
    stable_client_order_id,
)
from live15_quant.logging_config import configure_logging
from live15_quant.providers.kalshi_demo import resolve_kalshi_demo_credentials
from live15_quant.providers.kalshi_demo_execution import DemoBookSide, KalshiDemoExecutionClient
from live15_quant.recorder_control import process_alive

logger = logging.getLogger(__name__)


class DemoFirstFillError(RuntimeError):
    """A first-fill worker invariant could not be established safely."""


class DemoFirstFillAlreadyRunning(DemoFirstFillError):
    """A live first-fill worker lease already exists."""

    def __init__(self, pid: int) -> None:
        super().__init__(f"Demo first-fill worker is already running (pid={pid})")
        self.pid = pid


class DemoFirstFillStatus(StrEnum):
    STARTING = "STARTING"
    WAITING_SIGNAL = "WAITING_SIGNAL"
    EVALUATING = "EVALUATING"
    GUARD_BLOCKED = "GUARD_BLOCKED"
    POSTING = "POSTING"
    RECONCILING = "RECONCILING"
    FILLED = "FILLED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class DemoFirstFillPaths:
    status_path: Path = Path("runtime/demo_first_fill_status.json")
    lease_path: Path = Path("runtime/demo_first_fill_worker.lock")
    stop_path: Path = Path("runtime/demo_first_fill_stop.json")
    log_path: Path = Path("logs/demo_first_fill_worker.log")
    demo_store_path: Path = Path("data/demo-execution.sqlite3")


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DemoFirstFillError("first-fill timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class DemoFirstFillLease:
    """Exclusive, stale-recoverable local lease for the one-attempt worker."""

    def __init__(self, path: Path, *, pid: int | None = None) -> None:
        self.path = path
        self.pid = os.getpid() if pid is None else pid
        self._held = False

    def acquire(self, started_at: datetime) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"pid": self.pid, "started_at": _timestamp(started_at)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
                existing_pid = int(existing.get("pid", 0))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                raise DemoFirstFillError(
                    "first-fill lease is malformed; refusing concurrent start"
                ) from None
            if process_alive(existing_pid):
                raise DemoFirstFillAlreadyRunning(existing_pid) from None
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return self.acquire(started_at)
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        try:
            existing = json.loads(self.path.read_text(encoding="utf-8"))
            if int(existing.get("pid", 0)) == self.pid:
                self.path.unlink(missing_ok=True)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            # Never remove an unreadable replacement lease owned by an unknown process.
            pass
        self._held = False


class DemoFirstFillStatusStore:
    """Atomic, non-secret status projection with a durable post reservation."""

    _REQUIRED = frozenset(
        {
            "status",
            "pid",
            "started_at",
            "last_heartbeat",
            "last_signal_at",
            "last_candidate",
            "last_skip_reason",
            "post_count",
            "fill_count",
            "last_http_result",
            "last_error",
            "final_state",
        }
    )

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, object] | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise DemoFirstFillError(
                "first-fill status is malformed; refusing a new POST"
            ) from None
        if not isinstance(value, dict) or not self._REQUIRED.issubset(value):
            raise DemoFirstFillError("first-fill status is incomplete; refusing a new POST")
        if not isinstance(value["post_count"], int) or int(value["post_count"]) < 0:
            raise DemoFirstFillError("first-fill post counter is invalid")
        return value

    def write(self, value: dict[str, object]) -> None:
        if not self._REQUIRED.issubset(value):
            raise DemoFirstFillError("first-fill status write is incomplete")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


def read_recent_forward_demo_intents(
    settings: Settings, *, now: datetime
) -> tuple[DemoIntent, ...]:
    """Bounded, strictly fresh actionable v2 facts; never returns historical signals."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("forward intent read time must be timezone-aware")
    cutoff = now.astimezone(UTC) - timedelta(seconds=30)
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
                 AND decision_timestamp>=?
                 AND decision_timestamp<=?
               ORDER BY decision_timestamp,id LIMIT 64""",
            (_timestamp(cutoff), _timestamp(now)),
        ).fetchall()
    finally:
        connection.close()
    intents: list[DemoIntent] = []
    for row in rows:
        try:
            decision_timestamp = datetime.fromisoformat(str(row[2]).replace("Z", "+00:00"))
            if decision_timestamp.tzinfo is None:
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
                    decision_id=f"demo-first-fill:{row[0]}:{row[1]}",
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
        except (ArithmeticError, TypeError, ValueError):
            continue
    return tuple(intents)


class _PostReservationClient:
    """Delegate all reads, reserving the sole durable POST immediately before write."""

    def __init__(self, delegate: KalshiDemoExecutionClient, reserve: Callable[[str], None]) -> None:
        self._delegate = delegate
        self._reserve = reserve

    def create_order(self, request: object):
        client_order_id = getattr(request, "client_order_id", None)
        if not isinstance(client_order_id, str) or not client_order_id:
            raise DemoFirstFillError("Demo create request lacks a safe client order identity")
        self._reserve(client_order_id)
        return self._delegate.create_order(request)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _Coordinator(Protocol):
    def submit(
        self, intent: DemoIntent, context: DemoRiskContext
    ) -> DemoRiskDecision | DemoPreSubmitGuardResult | DemoReconciliationResult: ...

    def reconcile_positions(self) -> int: ...


@dataclass(slots=True)
class DemoFirstFillWorker:
    """Long-running, single-POST first-fill certification state machine."""

    settings: Settings
    coordinator: _Coordinator
    candidate_reader: Callable[[Settings], tuple[DemoIntent, ...]]
    status_store: DemoFirstFillStatusStore
    status: dict[str, object]
    logger: logging.Logger
    utc_now: Callable[[], datetime]
    sleep: Callable[[float], None]
    poll_seconds: float = 5.0
    post_reserved: bool = False
    remote_reconciliation_reader: Callable[[], dict[str, object]] | None = None
    stop_requested: Callable[[], bool] = lambda: False

    @classmethod
    def create(
        cls, settings: Settings, paths: DemoFirstFillPaths, *, writes_enabled: bool
    ) -> tuple[DemoFirstFillWorker, Any]:
        credentials = resolve_kalshi_demo_credentials(settings)
        client = KalshiDemoExecutionClient(settings, credentials, repository_root=Path.cwd())
        store = DemoExecutionStore(paths.demo_store_path)
        status_store = DemoFirstFillStatusStore(paths.status_path)
        existing = status_store.read()
        if existing is not None and int(existing["post_count"]) >= 1:
            store.close()
            client.close()
            raise DemoFirstFillError(
                "first-fill POST was already consumed; refusing another attempt"
            )
        started = datetime.now(UTC)
        status = _initial_status(started)
        worker = cls(
            settings=settings,
            coordinator=DemoExecutionCoordinator(
                _PostReservationClient(client, lambda client_id: worker.reserve_post(client_id)),
                store,
                quote_source=SqliteKalshiWsQuoteSource(
                    settings.recorder_data_path, settings.recorder_health_path
                ),
                writes_enabled=writes_enabled,
                execution_smoke_approved=writes_enabled,
            ),
            candidate_reader=lambda current_settings: read_recent_forward_demo_intents(
                current_settings, now=datetime.now(UTC)
            ),
            status_store=status_store,
            status=status,
            logger=logger,
            utc_now=lambda: datetime.now(UTC),
            sleep=time.sleep,
            stop_requested=lambda: paths.stop_path.exists(),
            remote_reconciliation_reader=lambda: {
                "remote_balance": str(client.balance().balance),
                "remote_portfolio_value": str(client.balance().portfolio_value),
                "remote_position_count": len(client.positions()),
                "remote_open_order_count": len(client.open_orders()),
                "remote_fill_count": len(client.fills()),
            },
        )
        # The closure above resolves after assignment, before any submit can occur.
        return worker, (client, store)

    def reserve_post(self, client_order_id: str) -> None:
        if self.post_reserved or int(self.status["post_count"]) >= 1:
            raise DemoFirstFillError("first-fill POST already reserved; refusing duplicate write")
        self.post_reserved = True
        self.status.update(
            {
                "status": DemoFirstFillStatus.POSTING.value,
                "post_count": 1,
                "last_http_result": None,
                "last_error": None,
                "final_state": None,
                "last_candidate": {
                    **dict(self.status["last_candidate"] or {}),
                    "client_order_id": client_order_id,
                },
            }
        )
        self._write_status()
        self.logger.info(
            "Demo first-fill POST reserved", extra={"event": "demo_first_fill_post_reserved"}
        )

    def run_forever(self) -> str:
        try:
            while True:
                final = self.run_once()
                if final is not None:
                    return final
                self.sleep(self.poll_seconds)
        except KeyboardInterrupt:
            self._stop("USER_STOPPED")
            return "USER_STOPPED"
        except Exception as error:
            self.status.update(
                {
                    "status": DemoFirstFillStatus.ERROR.value,
                    "last_error": type(error).__name__,
                    "final_state": "FATAL_RUNTIME_ERROR",
                }
            )
            self._write_status()
            self.logger.error(
                "Demo first-fill worker stopped with a typed error",
                extra={"event": "demo_first_fill_error", "error_type": type(error).__name__},
            )
            raise

    def run_once(self) -> str | None:
        now = self.utc_now().astimezone(UTC)
        if self.stop_requested():
            self._stop("USER_STOP_REQUESTED")
            return "USER_STOP_REQUESTED"
        self.status.update(
            {
                "status": DemoFirstFillStatus.WAITING_SIGNAL.value,
                "last_heartbeat": _timestamp(now),
                "last_error": None,
            }
        )
        self._write_status()
        intents = self.candidate_reader(self.settings)
        if not intents:
            self.logger.info(
                "Demo first-fill waiting for fresh actionable signal",
                extra={"event": "demo_first_fill_waiting"},
            )
            return None
        for intent in intents:
            self.status.update(
                {
                    "status": DemoFirstFillStatus.EVALUATING.value,
                    "last_signal_at": _timestamp(intent.decision_timestamp),
                    "last_candidate": {
                        "model_id": intent.model_id,
                        "ticker": intent.ticker,
                        "direction": "BUY_YES" if intent.side is DemoBookSide.BID else "BUY_NO",
                        "decision_timestamp": _timestamp(intent.decision_timestamp),
                        "decision_price": str(intent.price),
                        "decision_edge": str(intent.edge),
                    },
                    "last_skip_reason": None,
                }
            )
            self._write_status()
            result = self.coordinator.submit(intent, _empty_risk_context())
            if isinstance(result, DemoReconciliationResult):
                return self._reconcile_after_post(intent, result)
            reason, diagnostics = _skip_detail(result)
            self.status.update(
                {
                    "status": DemoFirstFillStatus.GUARD_BLOCKED.value,
                    "last_skip_reason": reason,
                    "last_candidate": {**dict(self.status["last_candidate"] or {}), **diagnostics},
                }
            )
            self._write_status()
            self.logger.info(
                "Demo first-fill candidate skipped",
                extra={"event": "demo_first_fill_skipped", "reason": reason},
            )
        return None

    def _reconcile_after_post(self, intent: DemoIntent, result: DemoReconciliationResult) -> str:
        self.status["status"] = DemoFirstFillStatus.RECONCILING.value
        self._write_status()
        # These are official Demo GETs only.  The coordinator has already
        # reconciled the order/fills; positions refresh is idempotent.
        reconciled_positions = self.coordinator.reconcile_positions()
        remote_counts = (
            self.remote_reconciliation_reader()
            if self.remote_reconciliation_reader is not None
            else {}
        )
        fill_count = int(result.inserted_fills)
        code = None
        if isinstance(self.coordinator, DemoExecutionCoordinator):
            diagnostic = self.coordinator._store.latest_execution_diagnostic(
                stable_client_order_id(intent)
            )
            if diagnostic is not None:
                code = diagnostic[0].value
        final = "DEMO_ENTRY_EXECUTION_PATH_CERTIFIED" if fill_count > 0 else "DEMO_POST_COMPLETED"
        self.status.update(
            {
                "status": (
                    DemoFirstFillStatus.FILLED.value
                    if fill_count > 0
                    else DemoFirstFillStatus.STOPPED.value
                ),
                "fill_count": fill_count,
                "last_http_result": code,
                "last_error": None,
                "final_state": final,
                "reconciled_positions": reconciled_positions,
                "provider_order_id": result.provider_order_id,
                **remote_counts,
            }
        )
        self._write_status()
        self.logger.info(
            "Demo first-fill worker stopped after its sole POST",
            extra={
                "event": "demo_first_fill_complete",
                "final_state": final,
                "fill_count": fill_count,
            },
        )
        return final

    def _stop(self, reason: str) -> None:
        self.status.update(
            {
                "status": DemoFirstFillStatus.STOPPED.value,
                "last_error": None,
                "final_state": reason,
            }
        )
        self._write_status()
        self.logger.info(
            "Demo first-fill worker stopped", extra={"event": "demo_first_fill_stopped"}
        )

    def _write_status(self) -> None:
        self.status_store.write(self.status)


def _initial_status(started_at: datetime) -> dict[str, object]:
    timestamp = _timestamp(started_at)
    return {
        "status": DemoFirstFillStatus.STARTING.value,
        "pid": os.getpid(),
        "started_at": timestamp,
        "last_heartbeat": timestamp,
        "last_signal_at": None,
        "last_candidate": None,
        "last_skip_reason": None,
        "post_count": 0,
        "fill_count": 0,
        "last_http_result": None,
        "last_error": None,
        "final_state": None,
    }


def _empty_risk_context() -> DemoRiskContext:
    return DemoRiskContext(
        event_exposure=Decimal(0),
        total_exposure=Decimal(0),
        open_positions=0,
        daily_realized_pnl=Decimal(0),
        kill_switch=False,
    )


def _skip_detail(
    result: DemoRiskDecision | DemoPreSubmitGuardResult,
) -> tuple[str, dict[str, object]]:
    if isinstance(result, DemoPreSubmitGuardResult):
        return result.code.value, result.diagnostics()
    return ",".join(reason.value for reason in result.reasons), {}


def _configure_worker_log(path: Path) -> logging.FileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return handler


def main(argv: Iterable[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="live15-demo-first-fill")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--execute-approved",
        action="store_true",
        help="enable the one explicitly approved Demo-only POST; default is dry-run",
    )
    actions.add_argument(
        "--stop",
        action="store_true",
        help="request graceful stop of a running first-fill worker",
    )
    arguments = parser.parse_args(tuple(argv or ()))
    settings = load_settings()
    configure_logging(settings.log_level)
    paths = DemoFirstFillPaths()
    if arguments.stop:
        paths.stop_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = paths.stop_path.with_name(f".{paths.stop_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text('{"requested":true}\n', encoding="utf-8")
            os.replace(temporary, paths.stop_path)
        finally:
            temporary.unlink(missing_ok=True)
        print(json.dumps({"status": "STOP_REQUESTED"}, sort_keys=True))
        return
    log_handler = _configure_worker_log(paths.log_path)
    lease = DemoFirstFillLease(paths.lease_path)
    started = datetime.now(UTC)
    try:
        lease.acquire(started)
    except DemoFirstFillAlreadyRunning as error:
        print(json.dumps({"status": "ALREADY_RUNNING", "pid": error.pid}, sort_keys=True))
        return
    resources: Any = None
    try:
        paths.stop_path.unlink(missing_ok=True)
        worker, resources = DemoFirstFillWorker.create(
            settings, paths, writes_enabled=arguments.execute_approved
        )
        worker.status.update(
            {
                "status": DemoFirstFillStatus.STARTING.value,
                "last_heartbeat": _timestamp(started),
                "execution_mode": "DEMO_WRITE_ENABLED_ONCE"
                if arguments.execute_approved
                else "DRY_RUN_WRITE_DISABLED",
            }
        )
        worker._write_status()
        final = worker.run_forever()
        print(json.dumps({"status": final}, sort_keys=True))
    finally:
        if resources is not None:
            client, store = resources
            store.close()
            client.close()
        lease.release()
        logger.removeHandler(log_handler)
        log_handler.close()


if __name__ == "__main__":
    main(sys.argv[1:])
