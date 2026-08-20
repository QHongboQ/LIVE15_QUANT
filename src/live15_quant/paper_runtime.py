"""Long-running, local-only paper runtime driven by public Kalshi quotes."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime

from live15_quant.config import Settings
from live15_quant.execution import ContractOutcome
from live15_quant.paper import DeterministicDummyStrategy
from live15_quant.paper_execution import KalshiPaperExecutionProvider
from live15_quant.paper_storage import PaperStore
from live15_quant.providers.kalshi import KalshiOfficialQuoteProvider
from live15_quant.providers.robinhood_15min import Robinhood15MinuteProvider
from live15_quant.risk import HardRiskLimits, ImmutableHardRiskLayer

logger = logging.getLogger(__name__)


class PaperRuntime:
    """Poll metadata and official quotes, then execute deterministic paper decisions."""

    def __init__(self, settings: Settings, store: PaperStore) -> None:
        self._settings = settings
        self._store = store
        self._discovery = Robinhood15MinuteProvider(settings)
        self._quotes = KalshiOfficialQuoteProvider(settings)
        self._strategy = DeterministicDummyStrategy()
        self._execution = KalshiPaperExecutionProvider(
            store=store,
            account_id=settings.paper_account_id,
            starting_cash=settings.paper_starting_cash,
            risk=ImmutableHardRiskLayer(
                HardRiskLimits(
                    settings.paper_max_order_notional,
                    settings.paper_max_event_exposure,
                    settings.paper_max_daily_loss,
                    settings.paper_max_total_exposure,
                    settings.paper_max_consecutive_losses,
                )
            ),
            kill_switch=settings.paper_kill_switch,
        )
        self._last_signal: dict[str, float] = {}
        self._known_ends: dict[str, datetime] = {}

    @property
    def execution(self) -> KalshiPaperExecutionProvider:
        return self._execution

    def run(
        self,
        *,
        stop_event: threading.Event | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        stop = stop_event or threading.Event()
        started = time.monotonic()
        last_discovery = float("-inf")
        contracts = ()
        logger.info(
            "Paper runtime started",
            extra={"event": "paper_runtime_started", "mode": "paper"},
        )
        try:
            while not stop.is_set():
                if duration_seconds is not None and time.monotonic() - started >= duration_seconds:
                    break
                now = datetime.now(UTC)
                pending_changed = False
                for event_id, end in tuple(self._known_ends.items()):
                    if now >= end:
                        has_position = any(
                            self._execution.portfolio.position(event_id, outcome) is not None
                            for outcome in ContractOutcome
                        )
                        self._execution.expire_event(event_id)
                        self._strategy.forget(event_id)
                        self._last_signal.pop(event_id, None)
                        if has_position:
                            pending_changed = True
                            logger.info(
                                "Position pending official settlement truth",
                                extra={
                                    "event": "paper_settlement_pending",
                                    "event_id": event_id,
                                },
                            )
                        self._known_ends.pop(event_id)
                if pending_changed:
                    self._store.append_portfolio(self._execution.portfolio.state({}, now))
                monotonic_now = time.monotonic()
                if monotonic_now - last_discovery >= self._settings.robinhood_poll_interval_seconds:
                    try:
                        contracts = self._discovery.discover()
                    except Exception:
                        last_discovery = monotonic_now
                        logger.exception(
                            "Paper event discovery failed",
                            extra={"event": "paper_discovery_failure"},
                        )
                    else:
                        last_discovery = monotonic_now
                        for contract in contracts:
                            self._known_ends[contract.event_id] = contract.end_time
                try:
                    quotes = self._quotes.quotes(contracts)
                except Exception:
                    logger.exception(
                        "Paper quote cycle failed",
                        extra={"event": "paper_source_failure"},
                    )
                    stop.wait(self._settings.official_quote_poll_interval_seconds)
                    continue
                for quote in quotes:
                    self._execution.update_quote(quote)
                    last = self._last_signal.get(quote.robinhood_event_id, float("-inf"))
                    if time.monotonic() - last < self._settings.paper_signal_interval_seconds:
                        continue
                    attempts = self._store.decision_count(quote.robinhood_event_id)
                    filled_orders = self._store.filled_order_count(quote.robinhood_event_id)
                    has_hold = self._store.has_hold(quote.robinhood_event_id)
                    if filled_orders >= 4:
                        continue
                    step = (
                        0
                        if filled_orders == 0
                        else 1
                        if filled_orders == 1 and not has_hold
                        else filled_orders + 1
                    )
                    self._strategy.set_step(quote.robinhood_event_id, step)
                    decision = self._strategy.decide(quote, now)
                    decision = replace(
                        decision,
                        decision_id=f"{decision.decision_id}-attempt-{attempts}",
                    )
                    result = self._execution.execute(
                        decision,
                        quote,
                    )
                    self._last_signal[quote.robinhood_event_id] = time.monotonic()
                    logger.info(
                        "Paper decision processed",
                        extra={
                            "event": "paper_execution",
                            "asset": quote.asset,
                            "event_id": quote.robinhood_event_id,
                            "decision": decision.decision,
                            "state": result.state,
                            "reason": result.reason,
                            "filled_quantity": result.filled_quantity,
                            "average_fill_price": result.average_fill_price,
                            "fees": result.fees,
                        },
                    )
                stop.wait(self._settings.official_quote_poll_interval_seconds)
        finally:
            self._discovery.close()
            self._quotes.close()
            logger.info(
                "Paper runtime stopped safely",
                extra={"event": "paper_runtime_stopped", "mode": "paper"},
            )
