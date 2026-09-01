"""Read-only Production Kalshi account projection for the Control Center."""

from __future__ import annotations

import json
import threading
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from live15_quant.control_center_models import (
    AccountEquityHistoryResponse,
    AccountFillResponse,
    AccountLedgerEntryResponse,
    AccountOrderResponse,
    AccountPositionResponse,
    AccountProfileResponse,
    AccountReadResponse,
    AccountSummaryResponse,
)
from live15_quant.kalshi_gateway.client import (
    KalshiEnvironment,
    KalshiGatewayConfig,
    KalshiGatewayError,
    build_sdk_client,
    production_credentials,
)
from live15_quant.kalshi_gateway.portfolio import KalshiPortfolioGateway


class AccountGateway(Protocol):
    def balance(self, *, exchange_index: int | None = None) -> Any: ...
    def positions(
        self, *, ticker: str | None = None, exchange_index: int | None = None
    ) -> tuple[Any, ...]: ...
    def orders(
        self, *, ticker: str | None = None, exchange_index: int | None = None
    ) -> tuple[Any, ...]: ...
    def fills(
        self,
        *,
        ticker: str | None = None,
        order_id: str | None = None,
        exchange_index: int | None = None,
    ) -> tuple[Any, ...]: ...


def _get(item: Any, *names: str) -> Any:
    for name in names:
        if isinstance(item, dict) and name in item:
            return item[name]
        value = getattr(item, name, None)
        if value is not None:
            return value
    return None


def _int(item: Any, *names: str) -> int | None:
    value = _get(item, *names)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dt(item: Any, *names: str) -> datetime | None:
    value = _get(item, *names)
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value) / (1000 if value > 10_000_000_000 else 1)
        return datetime.fromtimestamp(timestamp, UTC)
    return None


class ProductionAccountService:
    """Builds the existing SDK gateway lazily; never serializes credentials."""

    def __init__(self, settings: Any, *, gateway: AccountGateway | None = None, clock=None) -> None:
        self.settings = settings
        self._gateway = gateway
        self._clock = clock or (lambda: datetime.now(UTC))
        recorder_path = getattr(settings, "recorder_data_path", Path("data/live15.sqlite3"))
        self._history_path = Path(recorder_path).parent / "account-equity-history.jsonl"
        self._history_lock = threading.Lock()

    @staticmethod
    def profiles() -> list[AccountProfileResponse]:
        return [
            AccountProfileResponse(
                key="production_primary",
                label="Production · Primary",
                environment="PRODUCTION",
                is_primary=True,
            )
        ]

    def _gateway_or_raise(self) -> AccountGateway:
        if self._gateway is not None:
            return self._gateway
        credentials = production_credentials(self.settings)
        config = KalshiGatewayConfig.for_environment(KalshiEnvironment.PRODUCTION)
        self._gateway = KalshiPortfolioGateway(build_sdk_client(config, credentials=credentials))
        return self._gateway

    def read(self, profile: str = "production_primary") -> AccountReadResponse:
        observed = self._clock()
        if profile != "production_primary":
            return self._unavailable(profile, observed, "unknown account profile")
        try:
            gateway = self._gateway_or_raise()
            balance = gateway.balance()
            positions = tuple(gateway.positions())
            orders = tuple(gateway.orders())
            fills = tuple(gateway.fills())
            settlements = tuple(getattr(gateway, "settlements", lambda: ())())
            deposits = tuple(getattr(gateway, "deposits", lambda: ())())
            withdrawals = tuple(getattr(gateway, "withdrawals", lambda: ())())
        except Exception as error:
            status = (
                "AUTH_ERROR"
                if isinstance(error, (KalshiGatewayError, PermissionError))
                else "UNAVAILABLE"
            )
            return self._unavailable(profile, observed, status)
        balance_cents = _int(balance, "balance", "balance_cents")
        portfolio_cents = _int(balance, "portfolio_value", "portfolio_value_cents")
        summary = AccountSummaryResponse(
            profile=profile,
            status="AVAILABLE",
            observed_at=observed,
            balance_cents=balance_cents,
            portfolio_value_cents=portfolio_cents,
        )
        ledger = [self._ledger(item, "SETTLEMENT") for item in settlements]
        ledger += [self._ledger(item, "DEPOSIT") for item in deposits]
        ledger += [self._ledger(item, "WITHDRAWAL") for item in withdrawals]
        return AccountReadResponse(
            profile=profile,
            status="AVAILABLE",
            observed_at=observed,
            summary=summary,
            positions=[self._position(item) for item in positions],
            orders=[self._order(item) for item in orders],
            fills=[self._fill(item) for item in fills],
            ledger=ledger,
        )

    def read_summary(self, profile: str = "production_primary") -> AccountReadResponse:
        """Read only balance/positions and append one truthful forward equity observation."""

        observed = self._clock()
        if profile != "production_primary":
            return self._unavailable(profile, observed, "unknown account profile")
        try:
            gateway = self._gateway_or_raise()
            balance = gateway.balance()
            positions = tuple(gateway.positions())
        except Exception as error:
            status = (
                "AUTH_ERROR"
                if isinstance(error, (KalshiGatewayError, PermissionError))
                else "UNAVAILABLE"
            )
            return self._unavailable(profile, observed, status)
        summary = AccountSummaryResponse(
            profile=profile,
            status="AVAILABLE",
            observed_at=observed,
            balance_cents=_int(balance, "balance", "balance_cents"),
            portfolio_value_cents=_int(balance, "portfolio_value", "portfolio_value_cents"),
        )
        projected_positions = [self._position(item) for item in positions]
        self._append_equity(summary, projected_positions)
        return AccountReadResponse(
            profile=profile,
            status="AVAILABLE",
            observed_at=observed,
            summary=summary,
            positions=projected_positions,
        )

    def sample_equity(self, profile: str = "production_primary") -> float:
        """Take one read-only account sample and return its next bounded cadence."""

        result = self.read_summary(profile)
        active = any((position.position or 0) != 0 for position in result.positions)
        return 60.0 if result.status == "AVAILABLE" and active else 900.0

    def orders(self, profile: str = "production_primary") -> list[AccountOrderResponse]:
        if profile != "production_primary":
            return []
        return [self._order(item) for item in self._gateway_or_raise().orders()]

    def fills(self, profile: str = "production_primary") -> list[AccountFillResponse]:
        if profile != "production_primary":
            return []
        return [self._fill(item) for item in self._gateway_or_raise().fills()]

    def equity_history(self, profile: str = "production_primary") -> AccountEquityHistoryResponse:
        if profile != "production_primary":
            return AccountEquityHistoryResponse(profile=profile, status="UNAVAILABLE")
        try:
            if not self._history_path.is_file():
                return AccountEquityHistoryResponse(
                    profile=profile,
                    status="NOT_MATERIALIZED",
                    notes=["history begins with the first successful Terminal summary read"],
                )
            size = self._history_path.stat().st_size
            with self._history_path.open("rb") as handle:
                handle.seek(max(0, size - 1_048_576))
                if handle.tell() > 0:
                    handle.readline()
                rows = deque(handle, maxlen=2000)
            points = [json.loads(row) for row in rows]
            return AccountEquityHistoryResponse(
                profile=profile,
                status="AVAILABLE",
                points=points,
                notes=["forward-collected only; no synthetic or backfilled observations"],
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return AccountEquityHistoryResponse(profile=profile, status="UNAVAILABLE")

    def _append_equity(
        self,
        summary: AccountSummaryResponse,
        positions: list[AccountPositionResponse],
    ) -> bool:
        active = any((position.position or 0) != 0 for position in positions)
        point = {
            "observed_at": summary.observed_at.astimezone(UTC).isoformat(),
            "balance_cents": summary.balance_cents,
            "portfolio_value_cents": summary.portfolio_value_cents,
            "active_positions": active,
        }
        with self._history_lock:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            previous = self._last_equity_point()
            if previous is not None:
                changed = any(
                    previous.get(key) != point[key]
                    for key in ("balance_cents", "portfolio_value_cents", "active_positions")
                )
                previous_at = _dt(previous, "observed_at")
                cadence = timedelta(seconds=60 if active else 900)
                if (
                    not changed
                    and previous_at is not None
                    and summary.observed_at.astimezone(UTC) - previous_at < cadence
                ):
                    return False
            with self._history_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(point, separators=(",", ":")) + "\n")
            return True

    def _last_equity_point(self) -> dict[str, object] | None:
        if not self._history_path.is_file():
            return None
        size = self._history_path.stat().st_size
        with self._history_path.open("rb") as handle:
            handle.seek(max(0, size - 65_536))
            if handle.tell() > 0:
                handle.readline()
            rows = deque(handle, maxlen=1)
        if not rows:
            return None
        parsed = json.loads(rows[0])
        return parsed if isinstance(parsed, dict) else None

    def _unavailable(self, profile: str, observed: datetime, message: str) -> AccountReadResponse:
        summary = AccountSummaryResponse(
            profile=profile, status="UNAVAILABLE", observed_at=observed, message=message
        )
        return AccountReadResponse(
            profile=profile,
            status="UNAVAILABLE",
            observed_at=observed,
            summary=summary,
            message=message,
        )

    @staticmethod
    def _position(item: Any) -> AccountPositionResponse:
        return AccountPositionResponse(
            ticker=str(_get(item, "ticker", "market_ticker") or "UNKNOWN"),
            position=_int(item, "position", "quantity", "market_position"),
            market_exposure_cents=_int(item, "market_exposure", "market_exposure_cents"),
            realized_pnl_cents=_int(item, "realized_pnl", "realized_pnl_cents"),
            fees_cents=_int(item, "fees", "fees_cents"),
        )

    @staticmethod
    def _order(item: Any) -> AccountOrderResponse:
        return AccountOrderResponse(
            order_id=str(_get(item, "order_id", "id") or "UNKNOWN"),
            ticker=_get(item, "ticker", "market_ticker"),
            status=_get(item, "status"),
            side=_get(item, "side"),
            action=_get(item, "action"),
            count=_int(item, "count", "quantity"),
            remaining_count=_int(item, "remaining_count"),
            yes_price_cents=_int(item, "yes_price"),
            no_price_cents=_int(item, "no_price"),
            created_at=_dt(item, "created_time", "created_at"),
        )

    @staticmethod
    def _fill(item: Any) -> AccountFillResponse:
        return AccountFillResponse(
            trade_id=str(_get(item, "trade_id", "id") or "UNKNOWN"),
            order_id=_get(item, "order_id"),
            ticker=_get(item, "ticker", "market_ticker"),
            side=_get(item, "side"),
            action=_get(item, "action"),
            count=_int(item, "count", "quantity"),
            yes_price_cents=_int(item, "yes_price"),
            no_price_cents=_int(item, "no_price"),
            created_at=_dt(item, "created_time", "created_at"),
        )

    @staticmethod
    def _ledger(item: Any, entry_type: str) -> AccountLedgerEntryResponse:
        return AccountLedgerEntryResponse(
            entry_type=entry_type,
            amount_cents=_int(item, "amount", "amount_cents", "pnl", "fee"),
            ticker=_get(item, "ticker", "market_ticker"),
            reference=_get(item, "id", "settlement_id", "transfer_id"),
            observed_at=_dt(item, "created_at", "created_time", "timestamp"),
            description=_get(item, "description", "status", "result"),
        )
