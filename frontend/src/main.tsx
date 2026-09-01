/* eslint-disable react-refresh/only-export-components */
import { Admin, AppBar, DashboardMenuItem, Layout as AdminLayout, Menu, MenuItemLink, Resource, useGetList, useGetOne, useRedirect, useRefresh, useSidebarState } from 'react-admin';
import { CacheProvider } from '@emotion/react';
import createCache from '@emotion/cache';
import { Box, Button, Card, CardContent, Chip, IconButton, Skeleton, Stack, Tab, Tabs, Tooltip, Typography, createTheme } from '@mui/material';
import { memo, type ComponentProps, type ReactNode, useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { FinancialChart, type ChartPoint } from './charts';
import { api, dataProvider, type Account, type EquityHistory, type Health, type Market, type TerminalEvent } from './api';
import './styles.css';

declare global { interface Window { __LIVE15_TERMINAL_LATENCY_MS__?: number[] } }
const theme = createTheme({ palette: { mode: 'dark', primary: { main: '#a68bff' }, background: { default: '#08080e', paper: '#11111a' }, text: { primary: '#f5f2fb', secondary: '#9996a8' } }, shape: { borderRadius: 10 }, typography: { fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif', h4: { fontWeight: 700, letterSpacing: '-0.045em' } } });
const emotionNonce = document.querySelector<HTMLMetaElement>('meta[name="csp-nonce"]')?.content;
const emotionCache = createCache({ key: 'live15', nonce: emotionNonce, prepend: true });
const dollars = (cents?: number | null) => cents == null ? '—' : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(cents / 100);
const formatNumber = (value?: string | number | null) => value == null ? '—' : Number(value).toLocaleString(undefined, { maximumFractionDigits: 4 });
const seconds = (n?: number | null) => n == null ? '—' : n <= 0 ? 'Closed' : `${Math.floor(n / 60)}m ${Math.floor(n % 60)}s`;
const age = (n?: number | null) => n == null ? '—' : `${n < 1 ? n.toFixed(2) : n.toFixed(1)}s ago`;
const point = (time: string | null | undefined, value: string | number | null | undefined): ChartPoint | null => time == null || !Number.isFinite(Date.parse(time)) || value == null || !Number.isFinite(Number(value)) ? null : { time, value: Number(value) };
type QuoteState = Pick<Market, 'yes_bid' | 'yes_ask' | 'no_bid' | 'no_ask'>;
const quoteState = (market: Market): QuoteState => ({ yes_bid: market.yes_bid, yes_ask: market.yes_ask, no_bid: market.no_bid, no_ask: market.no_ask });
const quoteChanged = (previous: QuoteState | null, next: QuoteState) => previous != null && (previous.yes_bid !== next.yes_bid || previous.yes_ask !== next.yes_ask || previous.no_bid !== next.no_bid || previous.no_ask !== next.no_ask);
const appendPoint = (previous: ChartPoint[], next: ChartPoint) => previous.at(-1)?.time === next.time ? [...previous.slice(0, -1), next] : [...previous.slice(-63), next];
const underlyingTimestamp = (market: Market) => market.underlying_persisted_timestamp ?? market.underlying_received_timestamp;
const probabilityTimestamp = (market: Market) => market.projection_available_timestamp ?? market.quote_received_timestamp ?? market.quote_source_timestamp;
const numeric = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value : null;
const formatBytes = (value: unknown) => { const raw = numeric(value); if (raw == null) return String(value ?? '—'); const units = ['B', 'KB', 'MB', 'GB', 'TB']; const index = raw === 0 ? 0 : Math.min(units.length - 1, Math.floor(Math.log(raw) / Math.log(1024))); return `${(raw / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`; };
const human = (label: string, value: unknown) => /bytes|disk|sqlite|archive|wal|growth|compressed|uncompressed/i.test(label) ? formatBytes(value) : /timestamp|_at$/i.test(label) && typeof value === 'string' ? new Date(value).toLocaleString() : /duration|seconds|age/i.test(label) && typeof value === 'number' ? age(value) : typeof value === 'object' ? 'Structured detail available' : String(value ?? '—');
const portfolioRanges = ['1D', '1W', '1M', '6M', 'ALL'];

function Status({ text }: { text: string }) { const warning = /error|stale|unavailable|degraded|warning|behind|fallback|missing/i.test(text); return <Chip className={warning ? 'status warning' : 'status'} label={text.replaceAll('_', ' ')} size="small" />; }
function Metric({ label, children, title }: { label: string; children: ReactNode; title?: string }) { return <Tooltip title={title ?? ''} disableHoverListener={!title}><div className="metric"><span>{label}</span><strong>{children}</strong></div></Tooltip>; }
function Loading() { return <div className="skeleton-stack"><Skeleton variant="rounded" height={92} /><Skeleton variant="rounded" height={240} /><Skeleton variant="rounded" height={120} /></div>; }
function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) { const refresh = useRefresh(); return <Box className="terminal-loading"><Typography variant="h6">Data is unavailable</Typography><Typography color="text.secondary">{error instanceof Error ? error.message : 'The local read-only API could not be reached.'}</Typography><Button onClick={retry ?? refresh}>Retry</Button></Box>; }
function Page({ title, subtitle, children, refresh, status }: { title: string; subtitle: string; children: ReactNode; refresh?: () => void; status?: ReactNode }) { const globalRefresh = useRefresh(); return <Box className="terminal-page"><Stack className="page-title" direction="row"><Box><Typography variant="overline">LIVE15 / LOCAL TERMINAL</Typography><Typography variant="h4">{title}</Typography><Typography color="text.secondary">{subtitle}</Typography></Box><Stack direction="row" gap={1} alignItems="center">{status}<Button className="quiet-button" onClick={refresh ?? globalRefresh}>Refresh</Button></Stack></Stack>{children}</Box>; }
function Segments({ value, labels, onChange }: { value: number; labels: string[]; onChange: (next: number) => void }) { return <Tabs className="segmented" value={value} onChange={(_, next) => onChange(next)}>{labels.map((label) => <Tab key={label} label={label} />)}</Tabs>; }

function useTerminalStream(channels: string[], receive: (event: TerminalEvent) => void, reconcile: () => void) {
  const receiveRef = useRef(receive); const reconcileRef = useRef(reconcile); useEffect(() => { receiveRef.current = receive; reconcileRef.current = reconcile; }, [receive, reconcile]); const channelKey = channels.join('|');
  useEffect(() => { let socket: WebSocket | undefined; let reconnectTimer: number | undefined; let stopped = false; let lastSequence = 0; const selected = channelKey.split('|').filter(Boolean); const connect = () => { if (stopped || document.hidden || socket?.readyState === WebSocket.OPEN) return; socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/terminal`); socket.onopen = () => socket?.send(JSON.stringify({ action: 'subscribe', channels: selected })); socket.onmessage = (message) => { let event: TerminalEvent; try { event = JSON.parse(String(message.data)) as TerminalEvent; } catch { return; } if (event.schema_version !== 1 || event.sequence <= lastSequence || !selected.includes(event.channel)) return; lastSequence = event.sequence; if (event.event_type === 'update' && event.authoritative_at) { const sample = Math.max(0, Date.now() - Date.parse(event.authoritative_at)); const samples = [...(window.__LIVE15_TERMINAL_LATENCY_MS__ ?? []).slice(-999), sample]; window.__LIVE15_TERMINAL_LATENCY_MS__ = samples; document.documentElement.dataset.live15LatencySamples = samples.slice(-100).join(','); } receiveRef.current(event); }; socket.onclose = () => { socket = undefined; if (!stopped && !document.hidden) { reconcileRef.current(); reconnectTimer = window.setTimeout(connect, 500); } }; }; const visibility = () => { if (document.hidden) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ action: 'unsubscribe', channels: selected })); socket?.close(); } else { lastSequence = 0; reconcileRef.current(); connect(); } }; document.addEventListener('visibilitychange', visibility); connect(); return () => { stopped = true; document.removeEventListener('visibilitychange', visibility); if (reconnectTimer) window.clearTimeout(reconnectTimer); socket?.close(); }; }, [channelKey]);
}
function useLazyData<T>(loader: () => Promise<T>) { const [data, setData] = useState<T>(); const [error, setError] = useState<unknown>(); const [pending, setPending] = useState(true); const load = useCallback(() => { if (document.hidden) return; setPending(true); setError(undefined); void loader().then(setData).catch(setError).finally(() => setPending(false)); }, [loader]); useEffect(() => { void Promise.resolve().then(load); const visible = () => { if (!document.hidden) load(); }; document.addEventListener('visibilitychange', visible); return () => document.removeEventListener('visibilitychange', visible); }, [load]); return { data, error, pending, load }; }

const MarketCard = memo(function MarketCard({ market, trend, onOpen }: { market: Market; trend: ChartPoint[]; onOpen: () => void }) { return <button className="market-card" onClick={onOpen}><div className="market-card-top"><div><strong>{market.asset}</strong><small>{market.ticker ?? 'Awaiting contract'}</small></div><Status text={market.lifecycle} /></div><div className="market-card-middle"><span className="market-price">{formatNumber(market.underlying_price ?? market.target)}</span><MiniSparkline points={trend} /></div><div className="quote-line"><span>YES <b>{formatNumber(market.yes_bid)}</b></span><span>NO <b>{formatNumber(market.no_bid)}</b></span></div><small>{seconds(market.seconds_remaining)} · {market.quote_source}</small></button>; });
function MiniSparkline({ points }: { points: ChartPoint[] }) { const values = points.map((item) => item.value); if (values.length < 2) return <span className="sparkline-empty">LIVE</span>; const min = Math.min(...values); const max = Math.max(...values); const range = max - min || 1; const coordinates = points.slice(-32).map((item, index, all) => `${(index / (all.length - 1)) * 100},${100 - ((item.value - min) / range) * 88 - 6}`).join(' '); return <svg className="sparkline" viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="Live market trend"><polyline points={coordinates} /></svg>; }

function Overview() { const health = useGetOne<Health>('overview', { id: 'current' }); const markets = useGetList<Market>('markets', { pagination: { page: 1, perPage: 20 }, sort: { field: 'asset', order: 'ASC' }, filter: {} }); const redirect = useRedirect(); const [streamHealth, setStreamHealth] = useState<Health>(); const [streamMarkets, setStreamMarkets] = useState<Market[]>(); const reconcile = useCallback(() => { void health.refetch(); void markets.refetch(); }, [health, markets]); useTerminalStream(['overview', 'markets'], (event) => { if (event.channel === 'overview') setStreamHealth({ ...(event.payload as Health), id: 'current' }); else setStreamMarkets((event.payload as Market[]).map((market) => ({ ...market, id: market.asset }))); }, reconcile); const liveHealth = streamHealth ?? health.data; const liveMarkets = streamMarkets ?? markets.data; if (health.isPending || markets.isPending || !liveHealth || !liveMarkets) return <Loading />; if (health.error || markets.error) return <ErrorState error={health.error ?? markets.error} />; return <Page title="Overview" subtitle="Synchronized market state from local LIVE15 authority." refresh={reconcile} status={<Status text={liveHealth.status} />}><div className="terminal-status"><span className="live-dot" /> LIVE <span>Recorder {liveHealth.recorder_state}</span><span>{liveHealth.kalshi_ws_synchronized_count} markets synchronized</span><span>WS {liveHealth.kalshi_ws_connection_state}</span><span>{liveHealth.kalshi_ws_seq_gaps} sequence gaps</span></div>{liveHealth.current_health_issues.length > 0 && <Card className="warning-card"><CardContent><Status text="1 source issue" /><Box><strong>WTI/Pyth needs attention</strong><Typography>Recorder and synchronized market feeds remain live. WTI has a source-specific degradation; no substitute is shown.</Typography></Box></CardContent></Card>}<div className="market-grid overview-markets">{liveMarkets.map((market) => <MarketCard key={market.id} market={market} trend={[]} onOpen={() => redirect('show', 'markets', market.id)} />)}</div></Page>; }
function Markets() {
  const query = useGetList<Market>('markets', { pagination: { page: 1, perPage: 20 }, sort: { field: 'asset', order: 'ASC' }, filter: {} });
  const redirect = useRedirect();
  const [stream, setStream] = useState<Market[]>();
  const [trends, setTrends] = useState<Record<string, ChartPoint[]>>({});
  const reconcile = useCallback(() => { void query.refetch(); }, [query]);
  useTerminalStream(['markets'], (event) => {
    const next = (event.payload as Market[]).map((market) => ({ ...market, id: market.asset }));
    setStream(next);
    setTrends((previous) => {
      const updated = { ...previous };
      for (const market of next) {
        const item = point(underlyingTimestamp(market), market.underlying_price);
        if (!item) continue;
        const existing = updated[market.asset] ?? [];
        if (!existing.length || existing.at(-1)?.value !== item.value) updated[market.asset] = [...existing.slice(-31), item];
      }
      return updated;
    });
  }, reconcile);
  const live = stream ?? query.data;
  if (query.isPending || !live) return <Loading />;
  if (query.error) return <ErrorState error={query.error} />;
  return <Page title="Markets" subtitle="Live contracts, compact trends, and synchronized Kalshi books." refresh={reconcile}><div className="market-grid">{live.map((market) => {
    const initial = point(underlyingTimestamp(market), market.underlying_price);
    return <MarketCard key={market.id} market={market} trend={trends[market.asset] ?? (initial ? [initial] : [])} onOpen={() => redirect('show', 'markets', market.id)} />;
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
  const reconcile = useCallback(() => { void query.refetch(); history.load(); }, [query, history]);
  useEffect(() => {
    if (!history.data) return;
    activeTicker.current = history.data.ticker;
    const historicalPrice = history.data.underlying.at(-1);
    lastPricePoint.current = point(historicalPrice?.observed_at, historicalPrice?.close_price) ?? point(query.data?.underlying_persisted_timestamp ?? query.data?.underlying_received_timestamp, query.data?.underlying_price) ?? undefined;
    lastQuote.current = history.data.probability.at(-1) ?? (query.data ? quoteState(query.data) : null);
  }, [history.data, query.data]);
  useTerminalStream([`market:${id}`], (event) => {
    const market = event.payload as Market;
    if (activeTicker.current && market.ticker && market.ticker !== activeTicker.current) {
      activeTicker.current = market.ticker;
      lastPricePoint.current = undefined;
      lastQuote.current = null;
      setLiveBaseKey(undefined);
      setLivePricePoints([]);
      setLiveYesPoints([]);
      setLiveNoPoints([]);
      setLivePriceLastChange(undefined);
      setLiveProbabilityLastChange(undefined);
      void query.refetch();
      history.load();
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
  if (query.isPending || !data || history.pending || !history.data) return <Loading />;
  if (query.error || history.error) return <ErrorState error={query.error ?? history.error} retry={reconcile} />;
  const sourceLatency = Number(data.source_transport_latency_ms ?? NaN);
  const historyMatchesTicker = history.data.ticker === data.ticker;
  const liveMatchesHistory = liveBaseKey === history.data.generated_at;
  const pricePoints = [...(historyMatchesTicker ? history.data.underlying.map((item) => point(item.observed_at, item.close_price)).filter((item): item is ChartPoint => item != null) : []), ...(liveMatchesHistory ? livePricePoints : [])];
  const yesPoints = [...(historyMatchesTicker ? history.data.probability.map((item) => point(item.observed_at, item.yes_bid)).filter((item): item is ChartPoint => item != null) : []), ...(liveMatchesHistory ? liveYesPoints : [])];
  const noPoints = [...(historyMatchesTicker ? history.data.probability.map((item) => point(item.observed_at, item.no_bid)).filter((item): item is ChartPoint => item != null) : []), ...(liveMatchesHistory ? liveNoPoints : [])];
  const historicalLastChange = historyMatchesTicker ? mode === 0 ? history.data.underlying_last_actual_change_at : history.data.probability_last_actual_change_at : undefined;
  const lastChange = mode === 0 ? (liveMatchesHistory ? livePriceLastChange : undefined) ?? historicalLastChange : (liveMatchesHistory ? liveProbabilityLastChange : undefined) ?? historicalLastChange;
  return <Page title={`${data.asset} market`} subtitle={data.ticker ?? 'Awaiting active contract'} refresh={reconcile} status={<Status text={data.quote_source} />}><div className="detail-hero"><Metric label="Underlying">{formatNumber(data.underlying_price)}</Metric><Metric label="YES">{formatNumber(data.yes_bid)} / {formatNumber(data.yes_ask)}</Metric><Metric label="NO">{formatNumber(data.no_bid)} / {formatNumber(data.no_ask)}</Metric><Metric label="Remaining">{seconds(data.seconds_remaining)}</Metric><Metric label="Feed latency">{Number.isFinite(sourceLatency) ? `${sourceLatency.toFixed(0)}ms` : '—'}</Metric></div><div className="chart-card"><div className="chart-head"><Box><Typography variant="overline">REALTIME CONTRACT WINDOW</Typography><Typography variant="h6">{mode === 0 ? 'Underlying price' : 'Kalshi probability'}</Typography></Box><Segments value={mode} labels={['PRICE', 'PROBABILITY']} onChange={setMode} /></div>{mode === 0 ? <FinancialChart ariaLabel="Realtime underlying price chart" reference={Number(data.target)} series={[{ id: 'price', label: 'Price', color: '#a68bff', points: pricePoints }]} /> : <FinancialChart ariaLabel="Realtime Kalshi probability chart" series={[{ id: 'yes', label: 'YES', color: '#79e7b1', points: yesPoints }, { id: 'no', label: 'NO', color: '#f0a6cf', points: noPoints }]} /> }<div className="chart-footer"><span>● LIVE · {Number.isFinite(sourceLatency) ? `${sourceLatency.toFixed(0)}ms` : 'feed timing unavailable'}</span><span>Last actual change {lastChange ? new Date(lastChange).toLocaleString() : 'unavailable'}</span><span>Current point has right-side chart room</span></div></div><div className="detail-grid"><Depth title="YES bid depth" levels={data.yes_bid_depth} /><Depth title="NO bid depth" levels={data.no_bid_depth} /><Card className="surface-card"><CardContent><Typography variant="overline">Market state</Typography><Metric label="Order book"><Status text={data.orderbook_status} /></Metric><Metric label="Underlying">{data.underlying_provider ?? '—'} · {data.underlying_status}</Metric><Metric label="Settlement">{data.settlement_followup}</Metric></CardContent></Card></div></Page>;
}
function Depth({ title, levels }: { title: string; levels: string[][] }) { return <Card className="surface-card depth"><CardContent><Typography variant="overline">{title}</Typography>{levels.length ? levels.slice(0, 8).map((row, index) => <div className="depth-row" key={`${title}-${index}`}><span>{row[0] ?? '—'}</span><span>{row[1] ?? '—'}</span></div>) : <Typography color="text.secondary">No current depth projection.</Typography>}</CardContent></Card>; }
function Rows({ rows, columns }: { rows: Array<Record<string, unknown>>; columns: string[] }) { if (!rows.length) return <Card className="empty-card"><CardContent>No verified records are currently available.</CardContent></Card>; return <div className="record-list">{rows.map((row, index) => <div className="record-row" key={`${index}-${String(row[columns[0]])}`}>{columns.map((column) => <span key={column}><small>{column.replaceAll('_', ' ')}</small>{String(row[column] ?? '—')}</span>)}</div>)}</div>; }
function PortfolioSummary() { const [range, setRange] = useState(0); const loader = useCallback(async () => ({ account: await api.accountSummary(), history: await api.accountEquityHistory(portfolioRanges[range]) }), [range]); const query = useLazyData(loader); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; const data = query.data.account as Account; const history = query.data.history as EquityHistory; const points = history.points.map((item) => point(item.observed_at, item.portfolio_value_cents == null ? null : item.portfolio_value_cents / 100)).filter((item): item is ChartPoint => item != null); return <><div className="detail-hero"><Metric label="Balance">{dollars(data.summary.balance_cents)}</Metric><Metric label="Portfolio value">{dollars(data.summary.portfolio_value_cents)}</Metric><Metric label="Available history">{history.points.length} actual samples</Metric><Metric label="Account"><Status text={data.status} /></Metric></div><div className="chart-card"><div className="chart-head"><Box><Typography variant="overline">ACCOUNT VALUE</Typography><Typography variant="h6">Forward-collected equity</Typography></Box><Segments value={range} labels={portfolioRanges} onChange={setRange} /></div><FinancialChart ariaLabel="Account equity history" series={[{ id: 'equity', label: 'Portfolio', color: '#a68bff', points }]} />{history.notes.map((note) => <small className="chart-note" key={note}>{note}</small>)}</div><Rows rows={data.positions} columns={['ticker', 'position', 'market_exposure_cents', 'realized_pnl_cents', 'mark_cents']} /></>; }
function PortfolioOrders() { const query = useLazyData(api.accountOrders); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; return <Rows rows={query.data} columns={['order_id', 'ticker', 'status', 'side', 'count', 'created_at']} />; }
function PortfolioFills() { const query = useLazyData(api.accountFills); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; return <Rows rows={query.data} columns={['trade_id', 'ticker', 'side', 'count', 'yes_price_cents', 'created_at']} />; }
function Portfolio() { const [tab, setTab] = useState(0); return <Page title="Portfolio" subtitle="Read-only account value, positions, orders, and fills."><Segments value={tab} labels={['Account', 'Orders', 'Fills']} onChange={setTab} />{tab === 0 ? <PortfolioSummary /> : tab === 1 ? <PortfolioOrders /> : <PortfolioFills />}</Page>; }
function RecordPanel({ loader, fields }: { loader: () => Promise<Record<string, unknown>>; fields: Array<[string, string]> }) { const query = useLazyData(loader); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; return <div className="metric-grid">{fields.map(([label, key]) => <Metric key={key} label={label}>{human(key, query.data![key])}</Metric>)}</div>; }
function ResearchPage() { const [tab, setTab] = useState(0); return <Page title="Research" subtitle="Read-only authority, coverage, and gated training evidence."><Segments value={tab} labels={['Authority', 'Coverage', 'Training']} onChange={setTab} />{tab === 0 ? <RecordPanel loader={api.researchAuthority} fields={[["Research universe", 'universe_id'], ["Eligible events", 'eligible_events'], ["Eligible observations", 'eligible_observations'], ["Holdout access", 'holdout_accessed']]} /> : tab === 1 ? <RecordPanel loader={api.researchCoverage} fields={[["Coverage status", 'status'], ["Finalized events", 'finalized_events'], ["Trainable events", 'trainable_events'], ["Training rows", 'training_rows']]} /> : <><RecordPanel loader={api.researchTraining} fields={[["Sequence readiness", 'sequence_readiness'], ["Generated at", 'generated_at']]} /><Card className="surface-card"><CardContent><Status text="Gated" /> Gated / not exposed: training controls are intentionally unavailable in the terminal.</CardContent></Card></>}</Page>; }
function AdminRecordPanel({ loader }: { loader: () => Promise<Record<string, unknown>> }) { const query = useLazyData(loader); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; return <div className="metric-grid admin-grid">{Object.entries(query.data).slice(0, 24).map(([key, item]) => <Metric key={key} label={key.replaceAll('_', ' ')} title={typeof item === 'object' ? JSON.stringify(item) : String(item ?? '')}>{human(key, item)}</Metric>)}</div>; }
function OperationsPanel() { const query = useLazyData(api.adminOperations); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; return <><div className="metric-grid admin-grid">{Object.entries(query.data.operations).slice(0, 18).map(([key, item]) => <Metric key={key} label={key.replaceAll('_', ' ')} title={typeof item === 'object' ? JSON.stringify(item) : String(item ?? '')}>{human(key, item)}</Metric>)}</div><Card className="surface-card"><CardContent><Typography variant="overline">Recent warnings / events</Typography>{query.data.events.slice(0, 10).map((event, index) => <div className="event-row" key={index}><Status text={String(event.severity ?? 'info')} /><strong>{String(event.event_type ?? 'event')}</strong><span>{String(event.message ?? 'No message')}</span></div>)}</CardContent></Card></>; }
function AdminPage() { const [tab, setTab] = useState(0); return <Page title="Admin" subtitle="Read-only operational projections with human-readable units."><Segments value={tab} labels={['Data', 'Storage', 'Operations', 'System']} onChange={setTab} />{tab === 0 ? <AdminRecordPanel loader={api.adminData} /> : tab === 1 ? <AdminRecordPanel loader={api.adminStorage} /> : tab === 2 ? <OperationsPanel /> : <AdminRecordPanel loader={api.adminSystem} />}</Page>; }
function TerminalMenu() { return <Menu><DashboardMenuItem primaryText="Overview" /><MenuItemLink to="/markets" primaryText="Markets" /><MenuItemLink to="/portfolio" primaryText="Portfolio" /><MenuItemLink to="/research" primaryText="Research" /><MenuItemLink to="/admin" primaryText="Admin" /></Menu>; }
function TerminalBar() { const [open, setOpen] = useSidebarState(); useEffect(() => { document.documentElement.dataset.live15Sidebar = open ? 'open' : 'closed'; }, [open]); return <AppBar><IconButton className="sidebar-toggle" aria-label={open ? 'Hide sidebar' : 'Show sidebar'} onClick={() => setOpen(!open)}><span aria-hidden="true">☰</span></IconButton><Typography className="terminal-brand">LIVE15 <span>TERMINAL</span></Typography><span className="bar-status"><i /> LOCAL · READ ONLY</span></AppBar>; }
function TerminalLayout(props: ComponentProps<typeof AdminLayout>) { return <AdminLayout {...props} appBar={TerminalBar} menu={TerminalMenu} />; }
function App() { return <CacheProvider value={emotionCache}><Admin title="LIVE15 Terminal" theme={theme} dataProvider={dataProvider} dashboard={Overview} layout={TerminalLayout} requireAuth={false} disableTelemetry><Resource name="markets" list={Markets} show={MarketDetail} /><Resource name="portfolio" list={Portfolio} /><Resource name="research" list={ResearchPage} /><Resource name="admin" list={AdminPage} /></Admin></CacheProvider>; }
const root = document.getElementById('root')!; if (!root.dataset.mounted) { root.dataset.mounted = 'true'; createRoot(root).render(<App />); }
