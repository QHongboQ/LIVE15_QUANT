from datetime import UTC, datetime
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
