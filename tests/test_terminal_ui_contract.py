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
    assert "Underlying latency" in app
    assert "Projection latency" in app
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


def test_realtime_chart_uses_incremental_update_without_refitting() -> None:
    charts = (ROOT / "charts.tsx").read_text(encoding="utf-8")

    assert "line.update(latestPoint)" in charts
    assert "line.setData(points)" in charts
    assert "latestPoint.time > previous.at(-1)!.time" in charts
    assert "previous.slice(divergence + 1)" in charts
    assert "if (fitRequired) chart.current.timeScale().fitContent()" in charts
    assert charts.count("fitContent()") == 1
    assert "resetKey" in charts


def test_rollover_history_is_ignored_until_it_matches_active_ticker() -> None:
    app = (ROOT / "main.tsx").read_text(encoding="utf-8")

    assert "history.data.ticker !== activeTicker.current" in app
    assert "reconcileTicker" in app
    assert "rolloverTarget.current === ticker" in app
    assert "history.data.ticker" in app
    assert app.index("history.data.ticker !== activeTicker.current") < app.index(
        "activeTicker.current = history.data.ticker"
    )


def test_overview_health_warning_is_truthful_and_scoped() -> None:
    app = (ROOT / "main.tsx").read_text(encoding="utf-8")

    assert "const issues = liveHealth.current_health_issues" in app
    assert "issues.length > 0 && issues.every(isKnownWtiPythIssue)" in app
    assert "healthIssueSummary(issues)" in app
    assert 'Status text="1 source issue"' not in app
    assert "WTI/Pyth needs attention" in app


def test_terminal_status_is_derived_from_health_and_market_authority() -> None:
    app = (ROOT / "main.tsx").read_text(encoding="utf-8")

    assert (
        "type TerminalStatus = 'LIVE' | 'RECONNECTING' | 'DELAYED' | 'STALE' | 'UNAVAILABLE'" in app
    )
    assert "overviewTerminalStatus" in app
    assert "marketTerminalStatus" in app
    assert "underlying.includes(state)" in app
    assert "Object.values(health.current_markets).filter((ticker) => ticker != null).length" in app
    assert "const allMarketsSynchronized = expectedActiveMarkets > 0" in app
    assert "health.kalshi_ws_synchronized_count === expectedActiveMarkets" in app
    assert "!allMarketsSynchronized) return 'DELAYED'" in app
    assert "connection === 'synchronized'" in app
    assert "recorder === 'running'" in app
    assert "'RECONNECTING'" in app
    assert "'DELAYED'" in app
    assert "'STALE'" in app
    assert "'UNAVAILABLE'" in app
    assert "detailStatus === 'LIVE' ? '●' : '○'" in app
    assert "● LIVE" not in app


def test_market_latency_is_mode_specific_and_truthful() -> None:
    app = (ROOT / "main.tsx").read_text(encoding="utf-8")

    assert "const probabilityLatency = Number(data.source_transport_latency_ms ?? NaN)" in app
    assert "const priceLatency = underlyingLatency(data)" in app
    assert "underlying_received_timestamp" in app
    assert "underlying_persisted_timestamp" in app
    assert "const detailLatency = mode === 0 ? priceLatency" in app
    assert "Number.isFinite(probabilityLatency)" in app
    assert "timing unavailable" in app


def test_non_wti_health_issues_use_a_generic_health_label() -> None:
    app = (ROOT / "main.tsx").read_text(encoding="utf-8")

    assert "const issueKind = knownWtiPythOnly ? 'source' : 'health'" in app
    assert "${issues.length} ${issueKind} issue" in app
    assert "Current health issues" in app


def test_non_live_statuses_use_the_warning_visual_class() -> None:
    app = (ROOT / "main.tsx").read_text(encoding="utf-8")

    assert "reconnect|delayed|recovery" in app
    assert "className={warning ? 'status warning' : 'status'}" in app
    assert (
        "const warning = /error|stale|unavailable|degraded|warning|behind|fallback|missing|"
        "reconnect|delayed|recovery/i.test(text)" in app
    )


def test_realtime_resource_bounds_and_market_structural_sharing_are_explicit() -> None:
    app = (ROOT / "main.tsx").read_text(encoding="utf-8")

    assert "const LIVE_TAIL_MAX_POINTS = 64" in app
    assert "const SPARKLINE_MAX_POINTS = 32" in app
    assert "const LATENCY_SAMPLE_MAX_POINTS = 1000" in app
    assert "const LATENCY_DATASET_MAX_POINTS = 100" in app
    assert "appendBoundedPoint" in app
    assert "marketCardValuesSame" in app
    assert "return changed ? updated : previous" in app
    assert (
        "const previousByAsset = new Map(previous.map((market) => [market.asset, market]))" in app
    )


def test_market_detail_keeps_immutable_history_out_of_realtime_allocations() -> None:
    app = (ROOT / "main.tsx").read_text(encoding="utf-8")

    assert "useMemo" in app
    assert "const historicalPricePoints = useMemo" in app
    assert "const historicalYesPoints = useMemo" in app
    assert "const historicalNoPoints = useMemo" in app
    assert "history: historicalPricePoints" in app
    assert "livePoints: liveMatchesHistory ? livePricePoints : []" in app
    assert "const pricePoints = [..." not in app
    assert "const yesPoints = [..." not in app
    assert "const noPoints = [..." not in app


def test_chart_reuses_reference_price_line_and_cleans_resource_lifecycles() -> None:
    charts = (ROOT / "charts.tsx").read_text(encoding="utf-8")

    assert "history?: ChartPoint[]" in charts
    assert "livePoints?: ChartPoint[]" in charts
    assert "const normalizedHistory = useRef" in charts
    assert "const renderedLivePoints = useRef" in charts
    assert "line.update(latestPoint)" in charts
    assert "referenceLine.current.applyOptions({ price: reference })" in charts
    assert "referenceLinePrice.current" in charts
    assert "referenceSeries.current !== nextReferenceSeries" in charts
    assert "referenceSeries.current.removePriceLine(referenceLine.current)" in charts
    assert "observer.disconnect()" in charts
    assert "instance.remove()" in charts
