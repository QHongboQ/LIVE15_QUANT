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


def test_terminal_passes_the_server_nonce_to_emotion() -> None:
    app = (ROOT / "main.tsx").read_text(encoding="utf-8")

    assert "CacheProvider" in app
    assert "createCache" in app
    assert 'meta[name="csp-nonce"]' in app
    assert "nonce: emotionNonce" in app


def test_terminal_stream_and_lazy_network_contract_is_fail_closed() -> None:
    app = (ROOT / "main.tsx").read_text(encoding="utf-8")
    api = (ROOT / "api.ts").read_text(encoding="utf-8")

    assert "location.host}/ws/terminal" in app
    assert "event.sequence <= lastSequence" in app
    assert "!selected.includes(event.channel)" in app
    assert "action: 'unsubscribe'" in app
    assert "document.hidden" in app
    assert "reconcileRef.current(); connect();" in app
    assert "const load = useCallback" in app and "[loader]" in app
    for endpoint in (
        "/api/account/summary",
        "/api/account/orders",
        "/api/account/fills",
        "/api/research-data",
        "/api/coverage",
        "/api/training",
        "/api/data",
        "/api/storage",
        "/api/operations",
        "/api/system",
    ):
        assert endpoint in api
    combined = f"{app}\n{api}".lower()
    for host in ("kalshi.com", "coinbase.com", "pyth.network", "depthfeed"):
        assert host not in combined


def test_terminal_v2_chart_and_view_contracts_remain_local_and_truthful() -> None:
    app = (ROOT / "main.tsx").read_text(encoding="utf-8")
    api = (ROOT / "api.ts").read_text(encoding="utf-8")
    charts = (ROOT / "charts.tsx").read_text(encoding="utf-8")

    assert "lightweight-charts" in charts
    assert "No persisted history is available" in charts
    assert "FinancialChart" in app
    assert "PRICE" in app and "PROBABILITY" in app
    assert "Last actual change" in app
    assert "Feed latency" in app
    assert "live15Sidebar" in app
    assert "accountEquityHistory(portfolioRanges[range])" in app
    assert "close_price" in api
    assert "third_party" not in app.lower()


def test_market_detail_realtime_updates_do_not_refetch_history_per_event() -> None:
    app = (ROOT / "main.tsx").read_text(encoding="utf-8")

    assert "window.setTimeout(history.load" not in app
    assert "setLivePricePoints" in app
    assert "setLiveYesPoints" in app
    assert "setLiveNoPoints" in app
    assert "historyMatchesTicker" in app
    assert "activeTicker.current && market.ticker && market.ticker !== activeTicker.current" in app
    # Explicit refresh/reconciliation and ticker rollover are the only loads.
    assert app.count("history.load();") == 2
    assert "reconcileRef.current(); reconnectTimer" in app
    assert "const historicalLastChange = historyMatchesTicker ?" in app


def test_realtime_market_points_require_authoritative_timestamps_and_changes() -> None:
    app = (ROOT / "main.tsx").read_text(encoding="utf-8")

    assert "new Date().toISOString()" not in app
    assert "Date.now() - Date.parse(event.authoritative_at)" in app  # display telemetry only
    assert "underlyingTimestamp(market)" in app
    assert "probabilityTimestamp(market)" in app
    assert "lastPricePoint.current?.value !== price.value" in app
    assert "quoteChanged(lastQuote.current, quote)" in app
    assert "timestamp && (yes || no)" in app


def test_lightweight_chart_marker_plugin_is_reused_and_detached() -> None:
    charts = (ROOT / "charts.tsx").read_text(encoding="utf-8")

    assert "ISeriesMarkersPluginApi" in charts
    assert "const seriesMarkers = useRef" in charts
    assert "seriesMarkers.current.get(item.id)" in charts
    assert "markers.setMarkers" in charts
    assert "markers.detach()" in charts
    assert "const markersForCleanup = seriesMarkers.current" in charts
    assert "for (const markers of markersForCleanup.values()) markers.detach()" in charts
