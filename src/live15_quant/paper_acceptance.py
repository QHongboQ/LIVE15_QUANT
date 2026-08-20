"""Explicit long-running acceptance harness for local-only paper execution."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from live15_quant.config import Settings, load_settings
from live15_quant.execution import ContractOutcome
from live15_quant.logging_config import configure_logging
from live15_quant.models import FreshnessState
from live15_quant.paper import (
    KalshiOrderBookFillSimulator,
    PaperDecisionType,
    StrategyDecision,
    TimeInForce,
)
from live15_quant.paper_execution import KalshiPaperExecutionProvider, PaperReplayReader
from live15_quant.paper_runtime import PaperRuntime
from live15_quant.paper_storage import PaperStore
from live15_quant.providers.kalshi import KalshiOfficialQuoteProvider
from live15_quant.providers.robinhood_15min import Robinhood15MinuteProvider
from live15_quant.risk import HardRiskLimits, ImmutableHardRiskLayer


class AcceptanceUpstreamUnavailable(RuntimeError):
    """The public discovery/mapping path had no executable current instrument."""


def _limits() -> ImmutableHardRiskLayer:
    return ImmutableHardRiskLayer(
        HardRiskLimits(
            Decimal("1000000"),
            Decimal("1000000"),
            Decimal("1000000"),
            Decimal("1000000"),
            100,
        )
    )


def _decision(
    quote,
    decision_id: str,
    kind: PaperDecisionType,
    outcome: ContractOutcome,
    quantity: Decimal,
    price: Decimal,
    *,
    tif: TimeInForce = TimeInForce.IMMEDIATE_OR_CANCEL,
) -> StrategyDecision:
    return StrategyDecision(
        decision_id=decision_id,
        signal_timestamp=datetime.now(UTC),
        asset=quote.asset,
        event_id=quote.robinhood_event_id,
        contract_id=quote.robinhood_contract_id,
        decision=kind,
        outcome=outcome,
        quantity=quantity,
        limit_price=price,
        time_in_force=tif,
    )


def _executable_quote(settings: Settings, timeout_seconds: float = 120.0):
    deadline = time.monotonic() + timeout_seconds
    provider = KalshiOfficialQuoteProvider(settings)
    discovery = Robinhood15MinuteProvider(settings)
    try:
        while time.monotonic() < deadline:
            contracts = discovery.discover()
            for quote in provider.quotes(contracts):
                if quote.freshness is not FreshnessState.FRESH:
                    continue
                sides = (
                    (
                        ContractOutcome.YES,
                        quote.yes_ask,
                        quote.yes_bid,
                        quote.no_bid_depth,
                        quote.yes_bid_depth,
                    ),
                    (
                        ContractOutcome.NO,
                        quote.no_ask,
                        quote.no_bid,
                        quote.yes_bid_depth,
                        quote.no_bid_depth,
                    ),
                )
                for outcome, ask, bid, opposing_depth, bid_depth in sides:
                    if ask is None or bid is None or not bid_depth or bid_depth[0].price != bid:
                        continue
                    ask_quantity = (
                        opposing_depth[0].quantity
                        if opposing_depth and Decimal(1) - opposing_depth[0].price == ask
                        else None
                    )
                    if ask_quantity is not None and ask_quantity > Decimal("0.02"):
                        return quote, outcome, ask, bid, ask_quantity
            time.sleep(2)
    finally:
        discovery.close()
        provider.close()
    raise AcceptanceUpstreamUnavailable(
        "no mapped Kalshi instrument currently has consistent two-sided depth"
    )


def _scenario_acceptance(settings: Settings, path: Path) -> dict[str, object]:
    quote, outcome, ask, bid, top_quantity = _executable_quote(settings)
    buy = PaperDecisionType.BUY_YES if outcome is ContractOutcome.YES else PaperDecisionType.BUY_NO
    simulator = KalshiOrderBookFillSimulator()
    partial = simulator.simulate(
        order_id="accept-partial",
        decision=_decision(
            quote,
            "accept-partial",
            buy,
            outcome,
            top_quantity + Decimal("0.01"),
            ask,
        ),
        quote=quote,
        submit_timestamp=datetime.now(UTC),
    )
    no_fill_price = max(ask - Decimal("0.001"), Decimal(0))
    no_fill = simulator.simulate(
        order_id="accept-no-fill",
        decision=_decision(quote, "accept-no-fill", buy, outcome, Decimal("0.01"), no_fill_price),
        quote=quote,
        submit_timestamp=datetime.now(UTC),
    )
    fok = simulator.simulate(
        order_id="accept-fok",
        decision=_decision(
            quote,
            "accept-fok",
            buy,
            outcome,
            top_quantity + Decimal("0.01"),
            ask,
            tif=TimeInForce.FILL_OR_KILL,
        ),
        quote=quote,
        submit_timestamp=datetime.now(UTC),
    )
    moved_quote = (
        replace(quote, no_bid_depth=quote.no_bid_depth[1:])
        if outcome is ContractOutcome.YES
        else replace(quote, yes_bid_depth=quote.yes_bid_depth[1:])
    )
    moved = simulator.simulate(
        order_id="accept-moved",
        decision=_decision(quote, "accept-moved", buy, outcome, Decimal("0.01"), ask),
        quote=moved_quote,
        submit_timestamp=datetime.now(UTC),
    )
    with PaperStore(path, account_id="acceptance-paper", starting_cash=Decimal("1000000")) as store:
        adapter = KalshiPaperExecutionProvider(
            store=store,
            account_id="acceptance-paper",
            starting_cash=Decimal("1000000"),
            risk=_limits(),
        )
        open_result = adapter.execute(
            _decision(quote, "accept-open", buy, outcome, Decimal("0.01"), ask), quote
        )
        if open_result.filled_quantity != Decimal("0.01"):
            raise RuntimeError(
                f"acceptance open did not fill: {open_result.reason.value} {open_result.risk}"
            )
        add_result = adapter.execute(
            _decision(quote, "accept-add", PaperDecisionType.ADD, outcome, Decimal("0.01"), ask),
            quote,
        )
        reduce_result = adapter.execute(
            _decision(
                quote,
                "accept-reduce",
                PaperDecisionType.REDUCE,
                outcome,
                Decimal("0.005"),
                bid,
            ),
            quote,
        )
        close_result = adapter.execute(
            _decision(
                quote,
                "accept-close",
                PaperDecisionType.CLOSE,
                outcome,
                Decimal("0.01"),
                bid,
            ),
            quote,
        )
        stale_result = adapter.execute(
            _decision(quote, "accept-stale", buy, outcome, Decimal("0.01"), ask),
            replace(quote, freshness=FreshnessState.STALE),
        )
        uncertain_result = adapter.execute(
            _decision(quote, "accept-uncertain", buy, outcome, Decimal("0.01"), ask),
            quote,
            fill_state_certain=False,
        )
        counts = store.counts()
        integrity = store.integrity_check()
    with PaperStore(
        path, account_id="acceptance-paper", starting_cash=Decimal("1000000")
    ) as restarted:
        recovered = KalshiPaperExecutionProvider(
            store=restarted,
            account_id="acceptance-paper",
            starting_cash=Decimal("1000000"),
            risk=_limits(),
        )
        replay = PaperReplayReader(restarted)
        restart_ok = (
            restarted.integrity_check() == "ok"
            and [item.row_id for item in replay.fills()]
            == sorted(item.row_id for item in replay.fills())
            and recovered.portfolio.position(quote.robinhood_event_id, outcome) is None
        )
    return {
        "asset": quote.asset.value,
        "venue_ticker": quote.venue_ticker,
        "open": open_result.reason.value,
        "add": add_result.reason.value,
        "reduce": reduce_result.reason.value,
        "close": close_result.reason.value,
        "partial": partial.reason.value,
        "partial_filled": str(partial.filled_quantity),
        "no_fill": no_fill.reason.value,
        "fok": fok.reason.value,
        "price_moved": moved.reason.value,
        "stale_blocked": not stale_result.risk.allowed,
        "fill_uncertainty_blocked": not uncertain_result.risk.allowed,
        "counts": counts,
        "integrity": integrity,
        "restart_replay_ok": restart_ok,
    }


def run(duration_seconds: float, runtime_path: Path, scenario_path: Path) -> dict[str, object]:
    settings = replace(
        load_settings(),
        paper_data_path=runtime_path,
        paper_signal_interval_seconds=60.0,
    )
    with PaperStore(
        runtime_path,
        account_id=settings.paper_account_id,
        starting_cash=settings.paper_starting_cash,
    ) as store:
        PaperRuntime(settings, store).run(duration_seconds=duration_seconds)
        runtime_counts = store.counts()
        runtime_integrity = store.integrity_check()
    with PaperStore(
        runtime_path,
        account_id=settings.paper_account_id,
        starting_cash=settings.paper_starting_cash,
    ) as restarted:
        before = restarted.counts()
        recovered = KalshiPaperExecutionProvider(
            store=restarted,
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
        )
        replay = PaperReplayReader(restarted)
        restart = {
            "counts_preserved": before == runtime_counts,
            "replay_orders": len(replay.orders()),
            "replay_fills": len(replay.fills()),
            "cash": str(recovered.portfolio.cash),
            "integrity": restarted.integrity_check(),
        }
    try:
        scenarios = _scenario_acceptance(settings, scenario_path)
    except AcceptanceUpstreamUnavailable as error:
        scenarios = {
            "status": "expected_upstream_unavailable",
            "reason": str(error),
        }
    return {
        "duration_seconds": duration_seconds,
        "runtime_counts": runtime_counts,
        "runtime_integrity": runtime_integrity,
        "restart": restart,
        "scenarios": scenarios,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-seconds", type=float, default=1800.0)
    parser.add_argument("--runtime-path", type=Path)
    parser.add_argument("--scenario-path", type=Path)
    args = parser.parse_args()
    temporary = Path(tempfile.mkdtemp(prefix="live15-paper-acceptance-"))
    runtime_path = args.runtime_path or temporary / "runtime.sqlite3"
    scenario_path = args.scenario_path or temporary / "scenarios.sqlite3"
    settings = load_settings()
    configure_logging(settings.log_level)
    result = run(args.duration_seconds, runtime_path, scenario_path)
    result["temporary_directory"] = str(temporary)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
