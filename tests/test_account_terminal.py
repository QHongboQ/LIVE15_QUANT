from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from live15_quant.account_service import ProductionAccountService
from live15_quant.control_center import create_app


class FakeAccountGateway:
    def balance(self, **_):
        return SimpleNamespace(balance=12500, portfolio_value=13100)

    def positions(self, **_):
        return (SimpleNamespace(ticker="KXBTC", position=4, market_exposure=400),)

    def orders(self, **_):
        return ()

    def fills(self, **_):
        return ()


def test_production_account_projection_is_typed_and_read_only() -> None:
    service = ProductionAccountService(
        SimpleNamespace(),
        gateway=FakeAccountGateway(),
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    result = service.read()
    assert result.status == "AVAILABLE"
    assert result.summary.balance_cents == 12500
    assert result.summary.portfolio_value_cents == 13100
    assert result.positions[0].ticker == "KXBTC"
    assert service.profiles()[0].environment == "PRODUCTION"


def test_missing_production_credentials_is_explicitly_unavailable() -> None:
    result = ProductionAccountService(SimpleNamespace()).read()
    assert result.status == "UNAVAILABLE"
    assert result.summary.balance_cents is None
    assert result.summary.portfolio_value_cents is None


def test_account_tabs_are_lazy_and_equity_history_is_forward_only(tmp_path) -> None:
    calls: list[str] = []
    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    balance_cents = [12500]

    class CountingGateway(FakeAccountGateway):
        def balance(self, **kwargs):
            calls.append("balance")
            return SimpleNamespace(balance=balance_cents[0], portfolio_value=13100)

        def positions(self, **kwargs):
            calls.append("positions")
            return super().positions(**kwargs)

        def orders(self, **kwargs):
            calls.append("orders")
            return super().orders(**kwargs)

        def fills(self, **kwargs):
            calls.append("fills")
            return super().fills(**kwargs)

    service = ProductionAccountService(
        SimpleNamespace(recorder_data_path=tmp_path / "raw.sqlite3"),
        gateway=CountingGateway(),
        clock=lambda: current[0],
    )
    summary = service.read_summary()
    assert summary.status == "AVAILABLE"
    assert calls == ["balance", "positions"]
    history = service.equity_history()
    assert history.status == "AVAILABLE"
    assert len(history.points) == 1
    assert "no synthetic or backfilled" in history.notes[0]
    service.orders()
    assert calls == ["balance", "positions", "orders"]

    service.read_summary()
    assert len(service.equity_history().points) == 1
    current[0] += timedelta(seconds=59)
    service.read_summary()
    assert len(service.equity_history().points) == 1
    current[0] += timedelta(seconds=1)
    service.read_summary()
    assert len(service.equity_history().points) == 2
    current[0] += timedelta(seconds=1)
    balance_cents[0] += 1
    assert service.sample_equity() == 60.0
    assert len(service.equity_history().points) == 3

    idle = ProductionAccountService(
        SimpleNamespace(recorder_data_path=tmp_path / "idle.sqlite3"),
        gateway=SimpleNamespace(
            balance=lambda: SimpleNamespace(balance=1, portfolio_value=1),
            positions=lambda: (),
        ),
        clock=lambda: current[0],
    )
    assert idle.sample_equity() == 900.0


def test_terminal_visual_foundation_has_packaged_react_shell() -> None:
    app = create_app()
    paths = {str(route.path) for route in app.routes}
    assert "/api/account" in paths
    from importlib.resources import files

    page = files("live15_quant").joinpath("terminal", "index.html").read_text(encoding="utf-8")
    frontend = (Path(__file__).parents[1] / "frontend" / "src" / "main.tsx").read_text(
        encoding="utf-8"
    )
    assert "LIVE15 Terminal" in page
    assert 'id="root"' in page
    assert "Overview" in frontend
    assert "Portfolio" in frontend
