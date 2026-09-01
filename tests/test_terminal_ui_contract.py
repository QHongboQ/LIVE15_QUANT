import re
from pathlib import Path

ROOT = Path(__file__).parents[1] / "frontend" / "src"


def test_terminal_has_exactly_five_top_level_navigation_destinations() -> None:
    app = (ROOT / "main.tsx").read_text(encoding="utf-8")

    assert 'DashboardMenuItem primaryText="Overview"' in app
    for route, label in (
        ("markets", "Markets"),
        ("portfolio", "Portfolio"),
        ("research", "Research"),
        ("admin", "Admin"),
    ):
        assert f'MenuItemLink to="/{route}" primaryText="{label}"' in app
    assert app.count("MenuItemLink to=") == 4


def test_terminal_failure_and_runtime_truth_contract() -> None:
    app = (ROOT / "main.tsx").read_text(encoding="utf-8")

    assert "Data is unavailable" in app
    assert "The local read-only API could not be reached." in app
    assert "Retry" in app
    assert "kalshi_ws_synchronized_count" in app
    assert "kalshi_ws_seq_gaps" in app


def test_react_admin_telemetry_is_disabled_at_the_terminal_root() -> None:
    app = (ROOT / "main.tsx").read_text(encoding="utf-8")

    assert re.search(r"<Admin\b[^>]*\bdisableTelemetry\b", app)
