/* eslint-disable react-refresh/only-export-components */
import { Admin, AppBar, DashboardMenuItem, Layout as AdminLayout, Menu, MenuItemLink, Resource, useGetList, useGetOne, useRedirect, useRefresh, useSidebarState } from 'react-admin';
import { CacheProvider } from '@emotion/react';
import createCache from '@emotion/cache';
import { Box, Button, Card, CardContent, Chip, IconButton, Skeleton, Stack, Tab, Tabs, Tooltip, Typography, createTheme } from '@mui/material';
import { memo, type ComponentProps, type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { FinancialChart, PortfolioEquityChart, type ChartPoint, usePresentationClock } from './charts';
import { api, dataProvider, type Account, type EquityHistory, type Health, type Market, type TerminalEvent } from './api';
import './styles.css';
import './polish.css';

declare global { interface Window { __LIVE15_TERMINAL_LATENCY_MS__?: number[] } }
const LIVE_TAIL_MAX_POINTS = 64;
const SPARKLINE_MAX_POINTS = 32;
const LATENCY_SAMPLE_MAX_POINTS = 1000;
const LATENCY_DATASET_MAX_POINTS = 100;
const EMPTY_CHART_POINTS: ChartPoint[] = [];
const theme = createTheme({ palette: { mode: 'dark', primary: { main: '#a68bff' }, background: { default: '#08080e', paper: '#11111a' }, text: { primary: '#f5f2fb', secondary: '#9996a8' } }, shape: { borderRadius: 10 }, typography: { fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif', h4: { fontWeight: 700, letterSpacing: '-0.045em' } } });
const emotionNonce = document.querySelector<HTMLMetaElement>('meta[name="csp-nonce"]')?.content;
const emotionCache = createCache({ key: 'live15', nonce: emotionNonce, prepend: true });
const dollars = (cents?: number | null) => cents == null ? '—' : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(cents / 100);
const formatNumber = (value?: string | number | null) => value == null ? '—' : Number(value).toLocaleString(undefined, { maximumFractionDigits: 4 });
const probabilityCents = (value?: string | number | null) => value == null || !Number.isFinite(Number(value)) ? '—' : `${(Number(value) * 100).toLocaleString(undefined, { maximumFractionDigits: 1 })}¢`;
type TargetDifference = { label: 'ABOVE TARGET' | 'BELOW TARGET' | 'AT TARGET'; amount: string; percent?: string };
const targetDifference = (now?: string | number | null, target?: string | number | null): TargetDifference | null => {
  const current = Number(now); const reference = Number(target);
  if (!Number.isFinite(current) || !Number.isFinite(reference)) return null;
  const difference = current - reference;
  const amount = `${difference > 0 ? '+' : difference < 0 ? '-' : ''}$${Math.abs(difference).toLocaleString(undefined, { maximumFractionDigits: 4 })}`;
  return { label: difference > 0 ? 'ABOVE TARGET' : difference < 0 ? 'BELOW TARGET' : 'AT TARGET', amount, percent: reference === 0 ? undefined : `${difference > 0 ? '+' : ''}${(difference / reference * 100).toFixed(2)}%` };
};
const seconds = (n?: number | null) => n == null ? '—' : n <= 0 ? 'Closed' : `${Math.floor(n / 60)}m ${Math.floor(n % 60)}s`;
const age = (n?: number | null) => n == null ? '—' : `${n < 1 ? n.toFixed(2) : n.toFixed(1)}s ago`;
const point = (time: string | null | undefined, value: string | number | null | undefined): ChartPoint | null => time == null || !Number.isFinite(Date.parse(time)) || value == null || !Number.isFinite(Number(value)) ? null : { time, value: Number(value) };
type QuoteState = Pick<Market, 'yes_bid' | 'yes_ask' | 'no_bid' | 'no_ask'>;
const quoteState = (market: Market): QuoteState => ({ yes_bid: market.yes_bid, yes_ask: market.yes_ask, no_bid: market.no_bid, no_ask: market.no_ask });
const quoteChanged = (previous: QuoteState | null, next: QuoteState) => previous != null && (previous.yes_bid !== next.yes_bid || previous.yes_ask !== next.yes_ask || previous.no_bid !== next.no_bid || previous.no_ask !== next.no_ask);
const displayCountdownBucket = (secondsRemaining?: number | null) => secondsRemaining == null ? null : Math.max(0, Math.floor(secondsRemaining));
const marketCardValuesSame = (left: Market, right: Market) => left.asset === right.asset && left.ticker === right.ticker && left.lifecycle === right.lifecycle && left.underlying_price === right.underlying_price && left.target === right.target && left.yes_bid === right.yes_bid && left.no_bid === right.no_bid && displayCountdownBucket(left.seconds_remaining) === displayCountdownBucket(right.seconds_remaining) && left.quote_source === right.quote_source;
const shareMarketReferences = (previous: Market[], next: Market[]) => {
  const previousByAsset = new Map(previous.map((market) => [market.asset, market]));
  let changed = previous.length !== next.length;
  const shared = next.map((market) => {
    const prior = previousByAsset.get(market.asset);
    const value = prior && marketCardValuesSame(prior, market) ? prior : market;
    changed ||= value !== prior;
    return value;
  });
  return changed ? shared : previous;
};
const appendBoundedPoint = (previous: ChartPoint[], next: ChartPoint, maxPoints: number) => {
  const last = previous.at(-1);
  if (last?.time === next.time) return last.value === next.value ? previous : [...previous.slice(0, -1), next];
  if (last?.value === next.value) return previous;
  return [...previous.slice(-(maxPoints - 1)), next];
};
const mergeChartPoints = (...sets: ChartPoint[][]) => [...new Map(sets.flat().map((item) => [item.time, item])).values()].sort((left, right) => Date.parse(left.time) - Date.parse(right.time));
const appendPoint = (previous: ChartPoint[], next: ChartPoint) => appendBoundedPoint(previous, next, LIVE_TAIL_MAX_POINTS);
const underlyingTimestamp = (market: Market) => market.underlying_persisted_timestamp ?? market.underlying_received_timestamp;
const probabilityTimestamp = (market: Market) => market.projection_available_timestamp ?? market.quote_received_timestamp ?? market.quote_source_timestamp;
const numeric = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value : null;
const formatBytes = (value: unknown) => { const raw = numeric(value); if (raw == null) return String(value ?? '—'); const units = ['B', 'KB', 'MB', 'GB', 'TB']; const index = raw === 0 ? 0 : Math.min(units.length - 1, Math.floor(Math.log(raw) / Math.log(1024))); return `${(raw / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`; };
const duration = (value: number) => value >= 3600 ? `${(value / 3600).toFixed(value % 3600 ? 1 : 0)}h` : value >= 60 ? `${(value / 60).toFixed(value % 60 ? 1 : 0)}m` : `${value.toFixed(value < 10 ? 2 : 0)}s`;
const humanLabel = (value: string) => value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
const human = (label: string, value: unknown) => /bytes|disk|sqlite|archive|wal|growth|compressed|uncompressed/i.test(label) ? formatBytes(value) : /duration|retention_seconds/i.test(label) && typeof value === 'number' ? duration(value) : /percent|ratio/i.test(label) && typeof value === 'number' ? `${value.toFixed(1)}%` : /timestamp|_at$/i.test(label) && typeof value === 'string' ? new Date(value).toLocaleString() : /age/i.test(label) && typeof value === 'number' ? age(value) : Array.isArray(value) ? value.length ? `${value.length} ${value.length === 1 ? 'item' : 'items'}` : 'None' : typeof value === 'boolean' ? value ? 'Yes' : 'No' : typeof value === 'object' ? 'Available' : typeof value === 'string' && /^[a-z0-9_ -]+$/.test(value) ? humanLabel(value) : String(value ?? '—');
const portfolioRanges = ['1D', '1W', '1M', '3M', '6M', '1Y', 'ALL'];
type PortfolioTimeRange = { from: string; to: string };
const portfolioCalendarRange = (label: string, current = new Date()): PortfolioTimeRange | undefined => {
  if (label === 'ALL') return undefined;
  const from = new Date(current);
  if (label === '1D') from.setHours(0, 0, 0, 0);
  else if (label === '1W') from.setDate(from.getDate() - 7);
  else if (label === '1M') from.setMonth(from.getMonth() - 1);
  else if (label === '3M') from.setMonth(from.getMonth() - 3);
  else if (label === '6M') from.setMonth(from.getMonth() - 6);
  else if (label === '1Y') from.setFullYear(from.getFullYear() - 1);
  return { from: from.toISOString(), to: current.toISOString() };
};
const localCalendarBoundary = (date: string, end = false) => { const [year, month, day] = date.split('-').map(Number); return new Date(year, month - 1, day, end ? 23 : 0, end ? 59 : 0, end ? 59 : 0, end ? 999 : 0).toISOString(); };
const customPortfolioRange = (from: string, to: string): PortfolioTimeRange | undefined => from && to ? { from: localCalendarBoundary(from), to: localCalendarBoundary(to, true) } : undefined;
const isKnownWtiPythIssue = (issue: string) => /^(source_failure|stale_source):pyth:WTI Oil$/.test(issue);
const healthIssueSummary = (issues: string[]) => issues.map((issue) => issue.replaceAll('_', ' ')).join('; ');
type TerminalStatus = 'LIVE' | 'RECONNECTING' | 'DELAYED' | 'STALE' | 'UNAVAILABLE';
const normalized = (value: unknown) => String(value ?? '').toLowerCase().replace(/[\s-]+/g, '_');
const overviewTerminalStatus = (health: Health): TerminalStatus => {
  const recorder = normalized(health.recorder_state);
  const connection = normalized(health.kalshi_ws_connection_state);
  const healthStatus = normalized(health.status);
  const issues = health.current_health_issues;
  const knownWtiPythOnly = issues.length > 0 && issues.every(isKnownWtiPythIssue);
  const expectedActiveMarkets = Object.values(health.current_markets).filter((ticker) => ticker != null).length;
  const allMarketsSynchronized = expectedActiveMarkets > 0 && health.kalshi_ws_synchronized_count === expectedActiveMarkets;
  if (['stopped', 'failed', 'unavailable', 'error'].some((state) => recorder.includes(state) || healthStatus.includes(state))) return 'UNAVAILABLE';
  if (['reconnect', 'connect', 'disconnect'].some((state) => recorder.includes(state) || connection.includes(state))) return 'RECONNECTING';
  if (recorder !== 'running' || connection.includes('stale') || healthStatus.includes('stale')) return 'STALE';
  if (connection.includes('rest') || connection.includes('fallback') || connection.includes('degraded') || (healthStatus.includes('degraded') && !knownWtiPythOnly)) return 'DELAYED';
  if (recorder === 'running' && connection === 'synchronized' && !allMarketsSynchronized) return 'DELAYED';
  if (recorder === 'running' && connection === 'synchronized' && allMarketsSynchronized && (healthStatus === 'healthy' || knownWtiPythOnly)) return 'LIVE';
  return 'UNAVAILABLE';
};
const marketTerminalStatus = (market: Market): TerminalStatus => {
  const source = normalized(market.quote_source);
  const quote = normalized(market.quote_status);
  const orderbook = normalized(market.orderbook_status);
  const underlying = normalized(market.underlying_status);
  if (['reconnect', 'connect', 'disconnect'].some((state) => source.includes(state) || quote.includes(state) || orderbook.includes(state))) return 'RECONNECTING';
  if (['unavailable', 'missing', 'error', 'failed'].some((state) => source.includes(state) || quote.includes(state) || orderbook.includes(state))) return 'UNAVAILABLE';
  if (['unavailable', 'missing', 'error', 'failed'].some((state) => underlying.includes(state))) return 'UNAVAILABLE';
  if ([source, quote, orderbook, underlying].some((state) => state.includes('stale'))) return 'STALE';
  if (source.includes('rest') || source.includes('recovery') || source.includes('fallback')) return 'DELAYED';
  if (source.includes('ws') || source.includes('synchronized') || quote.includes('synchronized') || orderbook.includes('synchronized')) return 'LIVE';
  return 'STALE';
};
const underlyingLatency = (market: Market) => {
  if (!market.underlying_received_timestamp || !market.underlying_persisted_timestamp) return null;
  const elapsed = Date.parse(market.underlying_persisted_timestamp) - Date.parse(market.underlying_received_timestamp);
  return Number.isFinite(elapsed) && elapsed >= 0 ? elapsed : null;
};
const displayLatency = (value: number | null) => value == null ? 'timing unavailable' : `${value.toFixed(0)}ms`;

function Status({ text }: { text: string }) { const warning = /error|stale|unavailable|degraded|warning|behind|fallback|missing|reconnect|delayed|recovery/i.test(text); return <Chip className={warning ? 'status warning' : 'status'} label={text.replaceAll('_', ' ')} size="small" />; }
function Metric({ label, children, title }: { label: string; children: ReactNode; title?: string }) { return <Tooltip title={title ?? ''} disableHoverListener={!title}><div className="metric"><span>{label}</span><strong>{children}</strong></div></Tooltip>; }
function Loading() { return <div className="skeleton-stack"><Skeleton variant="rounded" height={92} /><Skeleton variant="rounded" height={240} /><Skeleton variant="rounded" height={120} /></div>; }
function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) { const refresh = useRefresh(); return <Box className="terminal-loading"><Typography variant="h6">Data is unavailable</Typography><Typography color="text.secondary">{error instanceof Error ? error.message : 'The local read-only API could not be reached.'}</Typography><Button onClick={retry ?? refresh}>Retry</Button></Box>; }
function Page({ title, subtitle, children, status }: { title: string; subtitle: string; children: ReactNode; status?: ReactNode }) { return <Box className="terminal-page"><Stack className="page-title" direction="row"><Box><Typography variant="overline">LIVE15 / LOCAL TERMINAL</Typography><Typography variant="h4">{title}</Typography><Typography color="text.secondary">{subtitle}</Typography></Box>{status}</Stack>{children}</Box>; }
function Segments({ value, labels, onChange }: { value: number; labels: string[]; onChange: (next: number) => void }) { return <Tabs className="segmented" value={value} onChange={(_, next) => onChange(next)}>{labels.map((label) => <Tab key={label} label={label} />)}</Tabs>; }

function useTerminalStream(channels: string[], receive: (event: TerminalEvent) => void, reconcile: () => void) {
  const receiveRef = useRef(receive); const reconcileRef = useRef(reconcile); useEffect(() => { receiveRef.current = receive; reconcileRef.current = reconcile; }, [receive, reconcile]); const channelKey = channels.join('|');
  useEffect(() => { let socket: WebSocket | undefined; let reconnectTimer: number | undefined; let stopped = false; let lastSequence = 0; const selected = channelKey.split('|').filter(Boolean); const connect = () => { if (stopped || document.hidden || socket?.readyState === WebSocket.OPEN) return; socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/terminal`); socket.onopen = () => socket?.send(JSON.stringify({ action: 'subscribe', channels: selected })); socket.onmessage = (message) => { let event: TerminalEvent; try { event = JSON.parse(String(message.data)) as TerminalEvent; } catch { return; } if (event.schema_version !== 1 || event.sequence <= lastSequence || !selected.includes(event.channel)) return; lastSequence = event.sequence; if (event.event_type === 'update' && event.authoritative_at) { const sample = Math.max(0, Date.now() - Date.parse(event.authoritative_at)); const samples = [...(window.__LIVE15_TERMINAL_LATENCY_MS__ ?? []).slice(-(LATENCY_SAMPLE_MAX_POINTS - 1)), sample]; window.__LIVE15_TERMINAL_LATENCY_MS__ = samples; document.documentElement.dataset.live15LatencySamples = samples.slice(-LATENCY_DATASET_MAX_POINTS).join(','); } receiveRef.current(event); }; socket.onclose = () => { socket = undefined; if (!stopped && !document.hidden) { reconcileRef.current(); reconnectTimer = window.setTimeout(connect, 500); } }; }; const visibility = () => { if (document.hidden) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ action: 'unsubscribe', channels: selected })); socket?.close(); } else { lastSequence = 0; reconcileRef.current(); connect(); } }; document.addEventListener('visibilitychange', visibility); connect(); return () => { stopped = true; document.removeEventListener('visibilitychange', visibility); if (reconnectTimer) window.clearTimeout(reconnectTimer); socket?.close(); }; }, [channelKey]);
}
function useLazyData<T>(loader: () => Promise<T>) { const [data, setData] = useState<T>(); const [error, setError] = useState<unknown>(); const [pending, setPending] = useState(true); const load = useCallback(() => { if (document.hidden) return; setPending(true); setError(undefined); void loader().then(setData).catch(setError).finally(() => setPending(false)); }, [loader]); useEffect(() => { void Promise.resolve().then(load); const visible = () => { if (!document.hidden) load(); }; document.addEventListener('visibilitychange', visible); return () => document.removeEventListener('visibilitychange', visible); }, [load]); return { data, error, pending, load }; }

const MarketCard = memo(function MarketCard({ market, trend, onOpen }: { market: Market; trend: ChartPoint[]; onOpen: (id: string) => void }) { const difference = targetDifference(market.underlying_price, market.target); return <button className="market-card" onClick={() => onOpen(market.id)}><div className="market-card-top"><strong>{market.asset}</strong><MiniSparkline points={trend} contractStart={market.window_start} contractEnd={market.window_end} /></div><div className="market-card-middle"><span className="market-price">{formatNumber(market.underlying_price ?? market.target)}</span><small className={difference?.label === 'ABOVE TARGET' ? 'positive' : 'negative'}>{difference ? <>{difference.label} <b>{difference.amount}</b>{difference.percent && ` · ${difference.percent}`}</> : 'Target unavailable'}</small></div><div className="quote-line"><span>UP <b>{probabilityCents(market.yes_bid)}</b></span><span>DOWN <b>{probabilityCents(market.no_bid)}</b></span></div><small>{seconds(market.seconds_remaining)}</small></button>; });
function MiniSparkline({ points, contractStart, contractEnd }: { points: ChartPoint[]; contractStart?: string | null; contractEnd?: string | null }) { if (points.length < 2) return <span className="sparkline-empty" aria-label="No current contract trend">—</span>; const boundedPoints = points.length <= SPARKLINE_MAX_POINTS ? points : Array.from({ length: SPARKLINE_MAX_POINTS }, (_, index) => points[Math.round(index * (points.length - 1) / (SPARKLINE_MAX_POINTS - 1))]); const boundedValues = boundedPoints.map((item) => item.value); const min = Math.min(...boundedValues); const max = Math.max(...boundedValues); const range = max - min || 1; const domainStart = Date.parse(contractStart ?? boundedPoints[0].time); const domainEnd = Math.max(domainStart + 1, Date.parse(contractEnd ?? boundedPoints.at(-1)!.time)); const coordinates = boundedPoints.map((item) => `${Math.max(0, Math.min(100, (Date.parse(item.time) - domainStart) / (domainEnd - domainStart) * 100))},${100 - ((item.value - min) / range) * 88 - 6}`).join(' '); return <svg className="sparkline" viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="Current contract market trend"><polyline points={coordinates} /></svg>; }
function useContractTrends(markets: Market[] | undefined) {
  const [contractTrends, setContractTrends] = useState<Record<string, ChartPoint[]>>({});
  const contractKey = markets?.map(({ asset, ticker }) => `${asset}:${ticker ?? ''}`).join('|') ?? '';
  const marketContracts = useMemo(() => contractKey ? contractKey.split('|').map((entry) => { const [asset, ticker] = entry.split(':'); return { asset, ticker: ticker || undefined }; }) : [], [contractKey]);
  useEffect(() => {
    let stopped = false;
    if (!marketContracts.length) return () => { stopped = true; };
    void Promise.all(marketContracts.map(async ({ asset, ticker }) => ({ asset, ticker, history: await api.marketHistory(asset) }))).then((items) => {
      if (stopped) return;
      setContractTrends(Object.fromEntries(items.filter(({ ticker, history }) => ticker != null && history.ticker === ticker).map(({ asset, history }) => [asset, history.underlying.map((item) => point(item.observed_at, item.close_price)).filter((item): item is ChartPoint => item != null)])));
    }).catch(() => { if (!stopped) setContractTrends({}); });
    return () => { stopped = true; };
  }, [marketContracts]);
  return contractTrends;
}

function Overview() {
  const health = useGetOne<Health>('overview', { id: 'current' });
  const markets = useGetList<Market>('markets', { pagination: { page: 1, perPage: 20 }, sort: { field: 'asset', order: 'ASC' }, filter: {} });
  const redirect = useRedirect();
  const openMarket = useCallback((id: string) => redirect('show', 'markets', id), [redirect]);
  const [streamHealth, setStreamHealth] = useState<Health>();
  const [streamMarkets, setStreamMarkets] = useState<Market[]>();
  const reconcile = useCallback(() => { void health.refetch(); void markets.refetch(); }, [health, markets]);
  useTerminalStream(['overview', 'markets'], (event) => {
    if (event.channel === 'overview') {
      setStreamHealth({ ...(event.payload as Health), id: 'current' });
    } else {
      const next = (event.payload as Market[]).map((market) => ({ ...market, id: market.asset }));
      setStreamMarkets((previous) => previous ? shareMarketReferences(previous, next) : next);
    }
  }, reconcile);
  const liveHealth = streamHealth ?? health.data;
  const liveMarkets = streamMarkets ?? markets.data;
  const contractTrends = useContractTrends(liveMarkets);
  if (health.isPending || markets.isPending || !liveHealth || !liveMarkets) return <Loading />;
  if (health.error || markets.error) return <ErrorState error={health.error ?? markets.error} />;
  const issues = liveHealth.current_health_issues;
  const knownWtiPythOnly = issues.length > 0 && issues.every(isKnownWtiPythIssue);
  const terminalStatus = overviewTerminalStatus(liveHealth);
  const issueKind = knownWtiPythOnly ? 'source' : 'health';
  return <Page title="Overview" subtitle="Live market state from your local LIVE15 service." status={<Status text={terminalStatus} />}>
    <div className="terminal-status"><span className={terminalStatus === 'LIVE' ? 'live-dot' : 'status-dot'} /> {terminalStatus} <span>Recorder {human(liveHealth.recorder_state, liveHealth.recorder_state).toLowerCase()}</span><span>{liveHealth.kalshi_ws_synchronized_count} markets in sync</span><span>Market feed {human(liveHealth.kalshi_ws_connection_state, liveHealth.kalshi_ws_connection_state).toLowerCase()}</span><span>{liveHealth.kalshi_ws_seq_gaps} sequence gaps</span></div>
    {issues.length > 0 && <Card className={knownWtiPythOnly ? 'warning-card warning-card-compact' : 'warning-card'}><CardContent><Status text={knownWtiPythOnly ? 'WTI source degraded' : `${issues.length} ${issueKind} issue${issues.length === 1 ? '' : 's'}`} /><Box><strong>{knownWtiPythOnly ? 'WTI source degraded' : 'Current health issues'}</strong><Typography>{knownWtiPythOnly ? 'Pyth is unavailable for WTI; no substitute is shown.' : healthIssueSummary(issues)}</Typography></Box></CardContent></Card>}
    <div className="market-grid overview-markets">{liveMarkets.map((market) => <MarketCard key={market.id} market={market} trend={contractTrends[market.asset] ?? EMPTY_CHART_POINTS} onOpen={openMarket} />)}</div>
  </Page>;
}
function Markets() {
  const query = useGetList<Market>('markets', { pagination: { page: 1, perPage: 20 }, sort: { field: 'asset', order: 'ASC' }, filter: {} });
  const redirect = useRedirect();
  const openMarket = useCallback((id: string) => redirect('show', 'markets', id), [redirect]);
  const [stream, setStream] = useState<Market[]>();
  const [trends, setTrends] = useState<Record<string, ChartPoint[]>>({});
  const reconcile = useCallback(() => { void query.refetch(); }, [query]);
  useTerminalStream(['markets'], (event) => {
    const next = (event.payload as Market[]).map((market) => ({ ...market, id: market.asset }));
    setStream((previous) => {
      if (!previous) return next;
      return shareMarketReferences(previous, next);
    });
    setTrends((previous) => {
      const updated = { ...previous };
      let changed = false;
      for (const market of next) {
        const item = point(underlyingTimestamp(market), market.underlying_price);
        if (!item) continue;
        const existing = updated[market.asset] ?? [];
        const trend = appendBoundedPoint(existing, item, SPARKLINE_MAX_POINTS);
        if (trend !== existing) { updated[market.asset] = trend; changed = true; }
      }
      return changed ? updated : previous;
    });
  }, reconcile);
  const live = stream ?? query.data;
  const contractTrends = useContractTrends(live);
  if (query.isPending || !live) return <Loading />;
  if (query.error) return <ErrorState error={query.error} />;
  return <Page title="Markets" subtitle="Live 15-minute contracts, prices, and market depth."><div className="market-grid">{live.map((market) => {
    const initial = point(underlyingTimestamp(market), market.underlying_price);
    const trend = contractTrends[market.asset] ?? (initial ? [initial] : EMPTY_CHART_POINTS);
    return <MarketCard key={market.id} market={market} trend={trends[market.asset] ? mergeChartPoints(trend, trends[market.asset]) : trend} onOpen={openMarket} />;
  })}</div></Page>;
}
function MarketDetail() {
  const id = decodeURIComponent(window.location.hash.split('/')[2] ?? '');
  const query = useGetOne<Market>('markets', { id });
  const historyLoader = useCallback(() => api.marketHistory(id), [id]);
  const history = useLazyData(historyLoader);
  const [stream, setStream] = useState<Market>();
  const [mode, setMode] = useState(0);
  const [livePricePoints, setLivePricePoints] = useState<ChartPoint[]>([]);
  const [liveYesPoints, setLiveYesPoints] = useState<ChartPoint[]>([]);
  const [liveNoPoints, setLiveNoPoints] = useState<ChartPoint[]>([]);
  const [livePriceLastChange, setLivePriceLastChange] = useState<string>();
  const [liveProbabilityLastChange, setLiveProbabilityLastChange] = useState<string>();
  const [liveBaseKey, setLiveBaseKey] = useState<string>();
  const activeTicker = useRef<string>();
  const lastPricePoint = useRef<ChartPoint>();
  const lastQuote = useRef<QuoteState | null>(null);
  const rolloverTarget = useRef<string>();
  const reconcile = useCallback(() => { void query.refetch(); history.load(); }, [query, history]);
  const reconcileTicker = useCallback((ticker: string) => {
    if (rolloverTarget.current === ticker) return;
    rolloverTarget.current = ticker;
    activeTicker.current = ticker;
    lastPricePoint.current = undefined;
    lastQuote.current = null;
    void query.refetch();
    history.load();
  }, [history, query]);
  useEffect(() => {
    const snapshotTicker = query.data?.ticker;
    if (snapshotTicker && activeTicker.current && snapshotTicker !== activeTicker.current) {
      if (!rolloverTarget.current) reconcileTicker(snapshotTicker);
    }
    if (snapshotTicker && !activeTicker.current) activeTicker.current = snapshotTicker;
    if (!history.data) return;
    if (activeTicker.current && history.data.ticker !== activeTicker.current) return;
    activeTicker.current = history.data.ticker;
    rolloverTarget.current = undefined;
    const historicalPrice = history.data.underlying.at(-1);
    lastPricePoint.current = point(historicalPrice?.observed_at, historicalPrice?.close_price) ?? point(query.data?.underlying_persisted_timestamp ?? query.data?.underlying_received_timestamp, query.data?.underlying_price) ?? undefined;
    lastQuote.current = history.data.probability.at(-1) ?? (query.data ? quoteState(query.data) : null);
  }, [history.data, query.data, reconcileTicker]);
  useTerminalStream([`market:${id}`], (event) => {
    const market = event.payload as Market;
    if (activeTicker.current && market.ticker && market.ticker !== activeTicker.current) {
      reconcileTicker(market.ticker);
      activeTicker.current = market.ticker;
      lastPricePoint.current = undefined;
      lastQuote.current = null;
      setLiveBaseKey(undefined);
      setLivePricePoints([]);
      setLiveYesPoints([]);
      setLiveNoPoints([]);
      setLivePriceLastChange(undefined);
      setLiveProbabilityLastChange(undefined);
      return;
    }
    if (!activeTicker.current && market.ticker) activeTicker.current = market.ticker;
    setStream((current) => ({ ...market, id, features: current?.features ?? query.data?.features ?? {}, previous_events: current?.previous_events ?? query.data?.previous_events ?? [] }));
    const historyKey = history.data?.generated_at;
    if (liveBaseKey !== historyKey) {
      setLiveBaseKey(historyKey);
      setLivePricePoints([]);
      setLiveYesPoints([]);
      setLiveNoPoints([]);
      setLivePriceLastChange(undefined);
      setLiveProbabilityLastChange(undefined);
    }
    const price = point(underlyingTimestamp(market), market.underlying_price);
    if (price && lastPricePoint.current?.value !== price.value) {
      lastPricePoint.current = price;
      setLivePricePoints((previous) => appendPoint(previous, price));
      setLivePriceLastChange(price.time);
    }
    const quote = quoteState(market);
    if (quoteChanged(lastQuote.current, quote)) {
      lastQuote.current = quote;
      const timestamp = probabilityTimestamp(market);
      const yes = point(timestamp, market.yes_bid);
      const no = point(timestamp, market.no_bid);
      if (yes) setLiveYesPoints((previous) => appendPoint(previous, yes));
      if (no) setLiveNoPoints((previous) => appendPoint(previous, no));
      if (timestamp && (yes || no)) setLiveProbabilityLastChange(timestamp);
    }
  }, reconcile);
  const data = stream ?? query.data;
  const historyMatchesTicker = Boolean(history.data && data && history.data.ticker === data.ticker);
  const historicalPricePoints = useMemo(() => history.data && historyMatchesTicker ? history.data.underlying.map((item) => point(item.observed_at, item.close_price)).filter((item): item is ChartPoint => item != null) : [], [history.data, historyMatchesTicker]);
  const historicalYesPoints = useMemo(() => history.data && historyMatchesTicker ? history.data.probability.map((item) => point(item.observed_at, item.yes_bid)).filter((item): item is ChartPoint => item != null) : [], [history.data, historyMatchesTicker]);
  const historicalNoPoints = useMemo(() => history.data && historyMatchesTicker ? history.data.probability.map((item) => point(item.observed_at, item.no_bid)).filter((item): item is ChartPoint => item != null) : [], [history.data, historyMatchesTicker]);
  if (query.isPending || !data || history.pending || !history.data) return <Loading />;
  if (query.error || history.error) return <ErrorState error={query.error ?? history.error} retry={reconcile} />;
  const probabilityLatency = Number(data.source_transport_latency_ms ?? NaN);
  const priceLatency = underlyingLatency(data);
  const detailStatus = marketTerminalStatus(data);
  const detailStatusLabel = detailStatus === 'DELAYED' && normalized(data.quote_source).includes('recovery') ? 'DELAYED / RECOVERY' : detailStatus;
  const detailLatency = mode === 0 ? priceLatency : Number.isFinite(probabilityLatency) ? probabilityLatency : null;
  const liveMatchesHistory = liveBaseKey === history.data.generated_at;
  const historicalLastChange = historyMatchesTicker ? mode === 0 ? history.data.underlying_last_actual_change_at : history.data.probability_last_actual_change_at : undefined;
  const lastChange = mode === 0 ? (liveMatchesHistory ? livePriceLastChange : undefined) ?? historicalLastChange : (liveMatchesHistory ? liveProbabilityLastChange : undefined) ?? historicalLastChange;
  const targetDelta = targetDifference(data.underlying_price, data.target);
  return <Page title={`${data.asset} · 15m`} subtitle="Current 15-minute contract" status={<Status text={detailStatusLabel} />}><div className="detail-hero"><Metric label="Now">{formatNumber(data.underlying_price)}</Metric><Metric label="Target">{formatNumber(data.target)}</Metric><Metric label={targetDelta?.label ?? 'TARGET DIFFERENCE'}>{targetDelta ? `${targetDelta.amount}${targetDelta.percent ? ` · ${targetDelta.percent}` : ''}` : 'Target unavailable'}</Metric><Metric label="UP probability">{probabilityCents(data.yes_bid)}</Metric><Metric label="DOWN probability">{probabilityCents(data.no_bid)}</Metric><Metric label="Remaining">{seconds(data.seconds_remaining)}</Metric></div><div className="terminal-detail"><div className="chart-card"><div className="chart-head"><Box><Typography variant="overline">CURRENT 15-MINUTE WINDOW</Typography><Typography variant="h6">{mode === 0 ? 'Underlying price' : 'Kalshi probability'}</Typography></Box><Segments value={mode} labels={['PRICE', 'PROBABILITY']} onChange={setMode} /></div>{mode === 0 ? <FinancialChart key={`${data.ticker}:${data.window_start}:price`} ariaLabel="Realtime underlying price chart" reference={Number(data.target)} contractStart={data.window_start} contractEnd={data.window_end} resetKey={`${data.ticker}:${history.data.generated_at}:price`} series={[{ id: 'price', label: 'Price', color: '#a68bff', history: historicalPricePoints, livePoints: liveMatchesHistory ? livePricePoints : [] }]} /> : <FinancialChart key={`${data.ticker}:${data.window_start}:probability`} ariaLabel="Realtime Kalshi probability chart" contractStart={data.window_start} contractEnd={data.window_end} resetKey={`${data.ticker}:${history.data.generated_at}:probability`} series={[{ id: 'yes', label: 'YES', color: '#79e7b1', history: historicalYesPoints, livePoints: liveMatchesHistory ? liveYesPoints : [] }, { id: 'no', label: 'NO', color: '#f0a6cf', history: historicalNoPoints, livePoints: liveMatchesHistory ? liveNoPoints : [] }]} /> }<div className="chart-footer"><span>{detailStatusLabel} · {displayLatency(detailLatency)}</span><span>Last price or quote change {lastChange ? new Date(lastChange).toLocaleString() : 'unavailable'}</span></div></div><div className="depth-panel"><Depth title="UP bid depth" levels={data.yes_bid_depth} /><Depth title="DOWN bid depth" levels={data.no_bid_depth} /></div></div></Page>;
}
function Depth({ title, levels }: { title: string; levels: string[][] }) { const visibleLevels = levels.slice(0, 8); const maxVisibleQuantity = Math.max(0, ...visibleLevels.map((row) => Number(row[1])).filter(Number.isFinite)); return <Card className="surface-card depth"><CardContent><Typography variant="overline">{title}</Typography>{visibleLevels.length ? visibleLevels.map((row, index) => { const quantity = Number(row[1]); const width = maxVisibleQuantity > 0 && Number.isFinite(quantity) ? Math.max(0, quantity / maxVisibleQuantity * 100) : 0; return <div className="depth-row" key={`${title}-${index}`}><i className="depth-bar" aria-hidden="true" style={{ width: `${width}%` }} /><span>{row[0] ?? '—'}</span><span>{row[1] ?? '—'}</span></div>; }) : <Typography color="text.secondary">No current depth projection.</Typography>}</CardContent></Card>; }
function Rows({ rows, columns }: { rows: Array<Record<string, unknown>>; columns: string[] }) { if (!rows.length) return <Card className="empty-card"><CardContent>No verified records are available right now.</CardContent></Card>; return <div className="record-list">{rows.map((row, index) => <div className="record-row" key={`${index}-${String(row[columns[0]])}`}>{columns.map((column) => <span key={column}><small>{humanLabel(column)}</small>{human(column, row[column])}</span>)}</div>)}</div>; }
function PortfolioSummary() { const [range, setRange] = useState(0); const [custom, setCustom] = useState(false); const [from, setFrom] = useState(''); const [to, setTo] = useState(''); const presentationNow = usePresentationClock(); const loader = useCallback(async () => ({ account: await api.accountSummary(), history: await api.accountEquityHistory(custom ? 'ALL' : portfolioRanges[range]) }), [range, custom]); const query = useLazyData(loader); const selectedRange = useMemo(() => custom ? customPortfolioRange(from, to) : portfolioCalendarRange(portfolioRanges[range], new Date(presentationNow)), [custom, from, presentationNow, range, to]); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; const data = query.data.account as Account; const history = query.data.history as EquityHistory; const points = history.points.filter((item) => (!selectedRange?.from || item.observed_at >= selectedRange.from) && (!selectedRange?.to || item.observed_at <= selectedRange.to)).map((item) => point(item.observed_at, item.portfolio_value_cents == null ? null : item.portfolio_value_cents / 100)).filter((item): item is ChartPoint => item != null); const domain = selectedRange ?? (points[0] ? { from: points[0].time, to: new Date(presentationNow).toISOString() } : undefined); const flatPortfolio = points.length > 0 && points.every((item) => item.value === points[0].value); return <><div className="detail-hero"><Metric label="Total equity">{dollars(data.summary.portfolio_value_cents)}</Metric><Metric label="Cash">{dollars(data.summary.balance_cents)}</Metric><Metric label="Recorded samples">{points.length}</Metric><Metric label="Account"><Status text={data.status} /></Metric></div><div className="chart-card"><div className="chart-head"><Box><Typography variant="overline">ACCOUNT VALUE</Typography><Typography variant="h6">Recorded equity history</Typography></Box><Stack direction="row" gap={1}><Segments value={range} labels={portfolioRanges} onChange={(next) => { setCustom(false); setRange(next); }} /><Button className="quiet-button" onClick={() => setCustom(!custom)}>Custom</Button></Stack></div>{custom && <div className="date-range"><label>From <input type="date" value={from} onChange={(event) => setFrom(event.target.value)} /></label><label>To <input type="date" value={to} onChange={(event) => setTo(event.target.value)} /></label></div>}{domain ? <PortfolioEquityChart points={points} from={domain.from} to={domain.to} /> : <div className="chart-empty">No recorded account values are available for this range.</div>}<small className="chart-note">{points.length} recorded samples · {flatPortfolio ? 'no value change' : 'live account reads only'}</small>{history.notes.map((note) => <small className="chart-note" key={note}>{note}</small>)}</div><Rows rows={data.positions} columns={['ticker', 'position', 'market_exposure_cents', 'realized_pnl_cents', 'mark_cents']} /></>; }
function PortfolioOrders() { const query = useLazyData(api.accountOrders); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; return <Rows rows={query.data} columns={['order_id', 'ticker', 'status', 'side', 'count', 'created_at']} />; }
function PortfolioFills() { const query = useLazyData(api.accountFills); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; return <Rows rows={query.data} columns={['trade_id', 'ticker', 'side', 'count', 'yes_price_cents', 'created_at']} />; }
function Portfolio() { const [tab, setTab] = useState(0); return <Page title="Portfolio" subtitle="Account value, positions, orders, and fills — view only."><Segments value={tab} labels={['Account', 'Orders', 'Fills']} onChange={setTab} />{tab === 0 ? <PortfolioSummary /> : tab === 1 ? <PortfolioOrders /> : <PortfolioFills />}</Page>; }
function RecordPanel({ loader, fields }: { loader: () => Promise<Record<string, unknown>>; fields: Array<[string, string]> }) { const query = useLazyData(loader); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; return <div className="metric-grid">{fields.map(([label, key]) => <Metric key={key} label={label}>{human(key, query.data![key])}</Metric>)}</div>; }
function ResearchPage() { const [tab, setTab] = useState(0); return <Page title="Research" subtitle="Read-only authority, coverage, and gated training evidence."><Segments value={tab} labels={['Authority', 'Coverage', 'Training']} onChange={setTab} />{tab === 0 ? <RecordPanel loader={api.researchAuthority} fields={[["Research universe", 'universe_id'], ["Eligible events", 'eligible_events'], ["Eligible observations", 'eligible_observations'], ["Holdout access", 'holdout_accessed']]} /> : tab === 1 ? <RecordPanel loader={api.researchCoverage} fields={[["Coverage status", 'status'], ["Finalized events", 'finalized_events'], ["Trainable events", 'trainable_events'], ["Training rows", 'training_rows']]} /> : <><RecordPanel loader={api.researchTraining} fields={[["Sequence readiness", 'sequence_readiness'], ["Generated at", 'generated_at']]} /><Card className="surface-card"><CardContent><Status text="Gated" /> Gated / not exposed: training controls are intentionally unavailable in the terminal.</CardContent></Card></>}</Page>; }
function AdminRecordPanel({ loader }: { loader: () => Promise<Record<string, unknown>> }) { const query = useLazyData(loader); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; return <div className="metric-grid admin-grid">{Object.entries(query.data).slice(0, 24).map(([key, item]) => <Metric key={key} label={humanLabel(key)} title={typeof item === 'object' ? JSON.stringify(item) : String(item ?? '')}>{human(key, item)}</Metric>)}</div>; }
function OperationsPanel() { const query = useLazyData(api.adminOperations); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; return <><div className="metric-grid admin-grid">{Object.entries(query.data.operations).slice(0, 18).map(([key, item]) => <Metric key={key} label={humanLabel(key)} title={typeof item === 'object' ? JSON.stringify(item) : String(item ?? '')}>{human(key, item)}</Metric>)}</div><Card className="surface-card"><CardContent><Typography variant="overline">Recent warnings and events</Typography>{query.data.events.slice(0, 10).map((event, index) => <div className="event-row" key={index}><Status text={String(event.severity ?? 'info')} /><strong>{humanLabel(String(event.event_type ?? 'event'))}</strong><span>{String(event.message ?? 'No message')}</span></div>)}</CardContent></Card></>; }
function AdminPage() { const [tab, setTab] = useState(0); return <Page title="Admin" subtitle="Read-only operational projections with human-readable units."><Segments value={tab} labels={['Data', 'Storage', 'Operations', 'System']} onChange={setTab} />{tab === 0 ? <AdminRecordPanel loader={api.adminData} /> : tab === 1 ? <AdminRecordPanel loader={api.adminStorage} /> : tab === 2 ? <OperationsPanel /> : <AdminRecordPanel loader={api.adminSystem} />}</Page>; }
function TerminalMenu() { return <Menu><DashboardMenuItem primaryText="Overview" /><MenuItemLink to="/markets" primaryText="Markets" /><MenuItemLink to="/portfolio" primaryText="Portfolio" /><MenuItemLink to="/research" primaryText="Research" /><MenuItemLink to="/admin" primaryText="Admin" /></Menu>; }
function TerminalBar() { const [open, setOpen] = useSidebarState(); useEffect(() => { document.documentElement.dataset.live15Sidebar = open ? 'open' : 'closed'; }, [open]); return <AppBar toolbar={false} userMenu={false}><IconButton className="sidebar-toggle" aria-label={open ? 'Hide sidebar' : 'Show sidebar'} onClick={() => setOpen(!open)}><span aria-hidden="true">☰</span></IconButton><Typography className="terminal-brand">LIVE15 <span>TERMINAL</span></Typography><span className="bar-status"><i /> LOCAL · READ ONLY</span></AppBar>; }
function TerminalLayout(props: ComponentProps<typeof AdminLayout>) { return <AdminLayout {...props} appBar={TerminalBar} menu={TerminalMenu} />; }
function App() { return <CacheProvider value={emotionCache}><Admin title="LIVE15 Terminal" theme={theme} dataProvider={dataProvider} dashboard={Overview} layout={TerminalLayout} requireAuth={false} disableTelemetry><Resource name="overview" list={Overview} /><Resource name="markets" list={Markets} show={MarketDetail} /><Resource name="portfolio" list={Portfolio} /><Resource name="research" list={ResearchPage} /><Resource name="admin" list={AdminPage} /></Admin></CacheProvider>; }
const root = document.getElementById('root')!; if (!root.dataset.mounted) { root.dataset.mounted = 'true'; createRoot(root).render(<App />); }
