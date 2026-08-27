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
    assert '"dashboard", "markets"' in script
    css = (ROOT / "app.css").read_text(encoding="utf-8")
    assert "overflow-y: scroll" in css
    assert "overflow-x: hidden" in css


def test_ui_refresh_fails_closed_instead_of_retaining_stale_projection() -> None:
    script = (ROOT / "app.js").read_text(encoding="utf-8")
    assert "const stateErrors = new Map();" in script
    assert "state[key] = null;" in script
    assert "failed refresh must never present an older payload as current truth" in script
    assert "Last valid values are retained" not in script
    assert "Recorder unknown" in script


def test_system_view_labels_ws_truth_fields_without_renaming_backend_metrics() -> None:
    script = (ROOT / "app.js").read_text(encoding="utf-8")
    assert "WS synchronized assets" in script
    assert "WS sequence gaps" in script
    assert "health.kalshi_ws_synchronized_count" in script
    assert "health.kalshi_ws_seq_gaps" in script
