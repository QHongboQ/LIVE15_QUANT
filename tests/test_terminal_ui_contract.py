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
