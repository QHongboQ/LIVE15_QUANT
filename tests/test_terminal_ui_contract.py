from pathlib import Path

ROOT = Path(__file__).parents[1] / "src" / "live15_quant" / "web"


def test_terminal_route_and_icon_contract_is_deterministic() -> None:
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "app.js").read_text(encoding="utf-8")
    routes = (
        "overview",
        "markets",
        "portfolio",
        "account",
        "orders",
        "history",
        "watchlist",
        "analytics",
        "signals",
        "models",
        "dashboard",
        "data",
        "training",
        "archive",
        "storage",
        "operations",
        "events",
        "system",
    )
    for route in routes:
        assert f'href="#/{route}"' in page
        assert f'"{route}"' in script
    assert '|| "overview"' in script
    assert '<use href="#i-' in page
    assert "⌁" not in page and "◇" not in page and "▦" not in page


def test_terminal_polling_and_failure_containment_contract() -> None:
    script = (ROOT / "app.js").read_text(encoding="utf-8")
    assert script.count("setInterval(") == 2
    assert "Unable to render this view" in script
    assert "inFlight" in script
    css = (ROOT / "app.css").read_text(encoding="utf-8")
    assert "overflow-y: scroll" in css
    assert "overflow-x: hidden" in css
