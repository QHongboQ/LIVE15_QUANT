/* eslint-disable react-refresh/only-export-components */
import { Admin, AppBar, DashboardMenuItem, Layout as AdminLayout, Menu, MenuItemLink, Resource, useGetList, useGetOne, useRedirect, useRefresh } from 'react-admin';
import { CacheProvider } from '@emotion/react';
import createCache from '@emotion/cache';
import { Box, Button, Card, CardContent, Chip, CircularProgress, Divider, Stack, Tab, Tabs, Typography, createTheme } from '@mui/material';
import type { ComponentProps, ReactNode } from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api, dataProvider, type Account, type Health, type Market, type TerminalEvent } from './api';
import './styles.css';

declare global { interface Window { __LIVE15_TERMINAL_LATENCY_MS__?: number[] } }

const theme = createTheme({ palette: { mode: 'dark', primary: { main: '#9b7bff' }, background: { default: '#07070a', paper: '#0d0e13' }, text: { primary: '#f3f1f8', secondary: '#92929e' } }, shape: { borderRadius: 8 }, typography: { fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif', h4: { fontWeight: 650, letterSpacing: '-0.035em' } } });
const emotionNonce = document.querySelector<HTMLMetaElement>('meta[name="csp-nonce"]')?.content;
const emotionCache = createCache({ key: 'live15', nonce: emotionNonce, prepend: true });
const value = (item: Record<string, unknown>, key: string) => item[key] == null ? '—' : String(item[key]);
const dollars = (cents?: number | null) => cents == null ? '—' : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(cents / 100);
const seconds = (n?: number | null) => n == null ? '—' : n <= 0 ? 'Closed' : `${Math.floor(n / 60)}m ${Math.floor(n % 60)}s`;
const age = (n?: number | null) => n == null ? '—' : `${n.toFixed(1)}s`;

function Status({ text }: { text: string }) { const bad = /error|stale|unavailable|degraded|warning|behind|fallback/i.test(text); return <Chip className={bad ? 'status warning' : 'status'} label={text.replaceAll('_', ' ')} size="small" />; }
function Metric({ label, children }: { label: string; children: ReactNode }) { return <div className="metric"><span>{label}</span><strong>{children}</strong></div>; }
function Loading() { return <Box className="terminal-loading"><CircularProgress size={24} /></Box>; }
function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) { const refresh = useRefresh(); return <Box className="terminal-loading"><Typography variant="h6">Data is unavailable</Typography><Typography color="text.secondary">{error instanceof Error ? error.message : 'The local read-only API could not be reached.'}</Typography><Button onClick={retry ?? refresh}>Retry</Button></Box>; }
function Page({ title, subtitle, children, refresh }: { title: string; subtitle: string; children: ReactNode; refresh?: () => void }) { const globalRefresh = useRefresh(); return <Box className="terminal-page"><Stack className="page-title" direction="row"><Box><Typography variant="overline">LIVE15 / LOCAL</Typography><Typography variant="h4">{title}</Typography><Typography color="text.secondary">{subtitle}</Typography></Box><Button className="quiet-button" onClick={refresh ?? globalRefresh}>Refresh</Button></Stack>{children}</Box>; }

function useTerminalStream(channels: string[], receive: (event: TerminalEvent) => void, reconcile: () => void) {
  const receiveRef = useRef(receive); const reconcileRef = useRef(reconcile);
  useEffect(() => { receiveRef.current = receive; reconcileRef.current = reconcile; }, [receive, reconcile]);
  const channelKey = channels.join('|');
  useEffect(() => {
    let socket: WebSocket | undefined; let reconnectTimer: number | undefined; let stopped = false; let lastSequence = 0;
    const selected = channelKey.split('|').filter(Boolean);
    const connect = () => {
      if (stopped || document.hidden || socket?.readyState === WebSocket.OPEN) return;
      socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/terminal`);
      socket.onopen = () => socket?.send(JSON.stringify({ action: 'subscribe', channels: selected }));
      socket.onmessage = (message) => {
        let event: TerminalEvent;
        try { event = JSON.parse(String(message.data)) as TerminalEvent; } catch { return; }
        if (event.schema_version !== 1 || event.sequence <= lastSequence || !selected.includes(event.channel)) return;
        lastSequence = event.sequence;
        if (event.event_type === 'update' && event.authoritative_at) {
          const sample = Math.max(0, Date.now() - Date.parse(event.authoritative_at));
          const samples = window.__LIVE15_TERMINAL_LATENCY_MS__ ?? [];
          const updatedSamples = [...samples.slice(-999), sample];
          window.__LIVE15_TERMINAL_LATENCY_MS__ = updatedSamples;
          document.documentElement.dataset.live15LatencySamples = updatedSamples.slice(-100).join(',');
        }
        receiveRef.current(event);
      };
      socket.onclose = () => { socket = undefined; if (!stopped && !document.hidden) reconnectTimer = window.setTimeout(connect, 500); };
    };
    const visibility = () => {
      if (document.hidden) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ action: 'unsubscribe', channels: selected })); socket?.close(); }
      else { lastSequence = 0; reconcileRef.current(); connect(); }
    };
    document.addEventListener('visibilitychange', visibility); connect();
    return () => { stopped = true; document.removeEventListener('visibilitychange', visibility); if (reconnectTimer) window.clearTimeout(reconnectTimer); socket?.close(); };
  }, [channelKey]);
}

function useLazyData<T>(loader: () => Promise<T>) {
  const [data, setData] = useState<T>(); const [error, setError] = useState<unknown>(); const [pending, setPending] = useState(true);
  const load = useCallback(() => { if (document.hidden) return; setPending(true); setError(undefined); void loader().then(setData).catch(setError).finally(() => setPending(false)); }, [loader]);
  useEffect(() => { void Promise.resolve().then(load); const visible = () => { if (!document.hidden) load(); }; document.addEventListener('visibilitychange', visible); return () => document.removeEventListener('visibilitychange', visible); }, [load]);
  return { data, error, pending, load };
}

const loadPortfolioSummary = async () => ({ account: await api.accountSummary(), history: await api.accountEquityHistory() });

function Overview() {
  const health = useGetOne<Health>('overview', { id: 'current' }); const markets = useGetList<Market>('markets', { pagination: { page: 1, perPage: 20 }, sort: { field: 'asset', order: 'ASC' }, filter: {} }); const redirect = useRedirect();
  const [streamHealth, setStreamHealth] = useState<Health>(); const [streamMarkets, setStreamMarkets] = useState<Market[]>();
  const reconcile = useCallback(() => { void health.refetch(); void markets.refetch(); }, [health, markets]);
  useTerminalStream(['overview', 'markets'], (event) => { if (event.channel === 'overview') setStreamHealth({ ...(event.payload as Health), id: 'current' }); else setStreamMarkets((event.payload as Market[]).map((market) => ({ ...market, id: market.asset }))); }, reconcile);
  const liveHealth = streamHealth ?? health.data; const liveMarkets = streamMarkets ?? markets.data;
  if (health.isPending || markets.isPending || !liveHealth || !liveMarkets) return <Loading />; if (health.error || markets.error) return <ErrorState error={health.error ?? markets.error} />; const h = liveHealth;
  return <Page title="Overview" subtitle="Current market state, synchronized locally." refresh={reconcile}><div className="overview-strip"><Status text={h.status} /><span>Recorder {h.recorder_state}</span><span>WS {h.kalshi_ws_connection_state}</span><span>{h.kalshi_ws_synchronized_count} markets synchronized</span><span>{h.kalshi_ws_seq_gaps} sequence gaps</span></div>{h.current_health_issues.length > 0 && <Card className="warning-card"><CardContent><strong>Current warnings</strong><Typography>{h.current_health_issues.join(' · ')}</Typography></CardContent></Card>}<div className="market-grid">{liveMarkets.map((market) => <button className="market-tile" key={market.id} onClick={() => redirect('show', 'markets', market.id)}><div><strong>{market.asset}</strong><Status text={market.lifecycle} /></div><span className="underlying">{market.underlying_price ?? market.target ?? '—'}</span><div className="quote-line"><span>YES {market.yes_bid ?? '—'} / {market.yes_ask ?? '—'}</span><span>NO {market.no_bid ?? '—'} / {market.no_ask ?? '—'}</span></div><small>{seconds(market.seconds_remaining)} · {market.quote_source} · {age(market.quote_age_seconds)}</small></button>)}</div></Page>;
}

function Markets() {
  const query = useGetList<Market>('markets', { pagination: { page: 1, perPage: 20 }, sort: { field: 'asset', order: 'ASC' }, filter: {} }); const [stream, setStream] = useState<Market[]>(); const redirect = useRedirect();
  const reconcile = useCallback(() => { void query.refetch(); }, [query]);
  useTerminalStream(['markets'], (event) => setStream((event.payload as Market[]).map((market) => ({ ...market, id: market.asset }))), reconcile); const live = stream ?? query.data;
  if (query.isPending || !live) return <Loading />; if (query.error) return <ErrorState error={query.error} />;
  return <Page title="Markets" subtitle="Authoritative current market and quote projections." refresh={reconcile}><div className="terminal-table"><div className="table-head"><span>Market</span><span>Underlying</span><span>YES</span><span>NO</span><span>Remaining</span><span>State</span></div>{live.map((m) => <button className="table-row" key={m.id} onClick={() => redirect('show', 'markets', m.id)}><strong>{m.asset}<small>{m.ticker ?? 'No active ticker'}</small></strong><span>{m.underlying_price ?? '—'}<small>{m.underlying_status}</small></span><span>{m.yes_bid ?? '—'} / {m.yes_ask ?? '—'}</span><span>{m.no_bid ?? '—'} / {m.no_ask ?? '—'}</span><span>{seconds(m.seconds_remaining)}<small>{m.quote_source} · {age(m.quote_age_seconds)}</small></span><Status text={m.lifecycle} /></button>)}</div></Page>;
}

function Depth({ title, levels }: { title: string; levels: string[][] }) { return <Card className="depth"><CardContent><Typography variant="overline">{title}</Typography>{levels.length ? levels.slice(0, 8).map((row, index) => <div className="depth-row" key={`${title}-${index}`}><span>{row[0] ?? '—'}</span><span>{row[1] ?? '—'}</span></div>) : <Typography color="text.secondary">No current depth projection.</Typography>}</CardContent></Card>; }
function MarketDetail() {
  const id = decodeURIComponent(window.location.hash.split('/')[2] ?? ''); const query = useGetOne<Market>('markets', { id }); const [stream, setStream] = useState<Market>();
  const reconcile = useCallback(() => { void query.refetch(); }, [query]);
  useTerminalStream([`market:${id}`], (event) => setStream((current) => ({ ...(event.payload as Market), id, features: current?.features ?? query.data?.features ?? {}, previous_events: current?.previous_events ?? query.data?.previous_events ?? [] })), reconcile); const live = stream ?? query.data;
  if (query.isPending || !live) return <Loading />; if (query.error) return <ErrorState error={query.error} />; const data = live;
  return <Page title={`${data.asset} market`} subtitle={data.ticker ?? 'No active contract'} refresh={reconcile}><div className="detail-hero"><Metric label="Underlying"><>{data.underlying_price ?? '—'}</></Metric><Metric label="YES"><>{data.yes_bid ?? '—'} / {data.yes_ask ?? '—'}</></Metric><Metric label="NO"><>{data.no_bid ?? '—'} / {data.no_ask ?? '—'}</></Metric><Metric label="Remaining"><>{seconds(data.seconds_remaining)}</></Metric><Metric label="Quote"><><Status text={data.quote_status} /> {data.quote_source}</></Metric></div><div className="detail-grid"><Depth title="YES bid depth" levels={data.yes_bid_depth} /><Depth title="NO bid depth" levels={data.no_bid_depth} /><Card><CardContent><Typography variant="overline">Market state</Typography><Metric label="Lifecycle"><Status text={data.lifecycle} /></Metric><Metric label="Order book"><Status text={data.orderbook_status} /></Metric><Metric label="Underlying"><>{data.underlying_provider ?? '—'} · {data.underlying_status}</></Metric><Metric label="Settlement"><>{data.settlement_followup}</></Metric></CardContent></Card></div></Page>;
}

function Rows({ rows, columns }: { rows: Array<Record<string, unknown>>; columns: string[] }) { if (!rows.length) return <Card className="empty-card"><CardContent>No verified records are currently available.</CardContent></Card>; return <div className="terminal-table"><div className="table-head">{columns.map((column) => <span key={column}>{column.replaceAll('_', ' ')}</span>)}</div>{rows.map((row, index) => <div className="table-row" key={`${index}-${value(row, columns[0])}`}>{columns.map((column) => <span key={column}>{value(row, column)}</span>)}</div>)}</div>; }
function PortfolioSummary() { const query = useLazyData(loadPortfolioSummary); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; const data = query.data.account as Account; return <><div className="detail-hero"><Metric label="Balance">{dollars(data.summary.balance_cents)}</Metric><Metric label="Portfolio value">{dollars(data.summary.portfolio_value_cents)}</Metric><Metric label="History points">{query.data.history.points.length}</Metric><Metric label="Account"><Status text={data.status} /></Metric></div><Rows rows={data.positions} columns={['ticker', 'position', 'market_exposure_cents', 'realized_pnl_cents', 'mark_cents']} /></>; }
function PortfolioOrders() { const query = useLazyData(api.accountOrders); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; return <Rows rows={query.data} columns={['order_id', 'ticker', 'status', 'side', 'count', 'created_at']} />; }
function PortfolioFills() { const query = useLazyData(api.accountFills); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; return <Rows rows={query.data} columns={['trade_id', 'ticker', 'side', 'count', 'yes_price_cents', 'created_at']} />; }
function Portfolio() { const [tab, setTab] = useState(0); return <Page title="Portfolio" subtitle="Read-only account, orders, and fills."><Tabs value={tab} onChange={(_, next) => setTab(next)}><Tab label="Positions / Account" /><Tab label="Orders" /><Tab label="Fills" /></Tabs>{tab === 0 ? <PortfolioSummary /> : tab === 1 ? <PortfolioOrders /> : <PortfolioFills />}</Page>; }

function RecordPanel({ loader, fields }: { loader: () => Promise<Record<string, unknown>>; fields: Array<[string, string]> }) { const query = useLazyData(loader); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; return <div className="metric-grid-wide">{fields.map(([label, key]) => <Metric key={key} label={label}>{value(query.data!, key)}</Metric>)}</div>; }
function ResearchPage() { const [tab, setTab] = useState(0); return <Page title="Research" subtitle="Authority, coverage, and gated training evidence."><Tabs value={tab} onChange={(_, next) => setTab(next)}><Tab label="Authority" /><Tab label="Coverage" /><Tab label="Training" /></Tabs>{tab === 0 ? <RecordPanel loader={api.researchAuthority} fields={[["Research universe", 'universe_id'], ["Eligible events", 'eligible_events'], ["Eligible observations", 'eligible_observations'], ["Holdout access", 'holdout_accessed']]} /> : tab === 1 ? <RecordPanel loader={api.researchCoverage} fields={[["Coverage status", 'status'], ["Finalized events", 'finalized_events'], ["Trainable events", 'trainable_events'], ["Training rows", 'training_rows']]} /> : <><RecordPanel loader={api.researchTraining} fields={[["Sequence readiness", 'sequence_readiness'], ["Generated at", 'generated_at']]} /><Metric label="Training controls">Gated / not exposed</Metric></>}</Page>; }

function AdminRecordPanel({ loader }: { loader: () => Promise<Record<string, unknown>> }) { const query = useLazyData(loader); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; return <div className="admin-grid">{Object.entries(query.data).slice(0, 18).map(([key, item]) => <Metric key={key} label={key.replaceAll('_', ' ')}>{typeof item === 'object' ? JSON.stringify(item) : String(item ?? '—')}</Metric>)}</div>; }
function OperationsPanel() { const query = useLazyData(api.adminOperations); if (query.pending) return <Loading />; if (query.error || !query.data) return <ErrorState error={query.error} retry={query.load} />; return <><div className="admin-grid">{Object.entries(query.data.operations).slice(0, 18).map(([key, item]) => <Metric key={key} label={key.replaceAll('_', ' ')}>{typeof item === 'object' ? JSON.stringify(item) : String(item ?? '—')}</Metric>)}</div><Card><CardContent><Typography variant="overline">Recent warnings / events</Typography>{query.data.events.slice(0, 10).map((event, index) => <div className="event-row" key={index}><Status text={value(event, 'severity')} /><span>{value(event, 'event_type')}</span><span>{value(event, 'message')}</span></div>)}</CardContent></Card></>; }
function AdminPage() { const [tab, setTab] = useState(0); return <Page title="Admin" subtitle="Owner-only, read-only operational projections."><Tabs value={tab} onChange={(_, next) => setTab(next)}><Tab label="Data" /><Tab label="Storage" /><Tab label="Operations" /><Tab label="System" /></Tabs>{tab === 0 ? <AdminRecordPanel loader={api.adminData} /> : tab === 1 ? <AdminRecordPanel loader={api.adminStorage} /> : tab === 2 ? <OperationsPanel /> : <AdminRecordPanel loader={api.adminSystem} />}</Page>; }

function TerminalMenu() { return <Menu><DashboardMenuItem primaryText="Overview" /><Divider /><MenuItemLink to="/markets" primaryText="Markets" /><MenuItemLink to="/portfolio" primaryText="Portfolio" /><MenuItemLink to="/research" primaryText="Research" /><MenuItemLink to="/admin" primaryText="Admin" /></Menu>; }
function TerminalBar() { return <AppBar><Typography className="terminal-brand">LIVE15 <span>TERMINAL</span></Typography></AppBar>; }
function TerminalLayout(props: ComponentProps<typeof AdminLayout>) { return <AdminLayout {...props} appBar={TerminalBar} menu={TerminalMenu} />; }
function App() { return <CacheProvider value={emotionCache}><Admin title="LIVE15 Terminal" theme={theme} dataProvider={dataProvider} dashboard={Overview} layout={TerminalLayout} requireAuth={false} disableTelemetry><Resource name="markets" list={Markets} show={MarketDetail} /><Resource name="portfolio" list={Portfolio} /><Resource name="research" list={ResearchPage} /><Resource name="admin" list={AdminPage} /></Admin></CacheProvider>; }

const root = document.getElementById('root')!; if (!root.dataset.mounted) { root.dataset.mounted = 'true'; createRoot(root).render(<App />); }
