/// <reference types="vite/client" />
import type { DataProvider, GetOneParams } from 'react-admin';

export type Health = { id: string; status: string; recorder_state: string; heartbeat_status: string; heartbeat_age_seconds?: number | null; current_health_issues: string[]; current_markets: Record<string, string | null>; observed_at?: string | null; uptime_seconds?: number | null; kalshi_ws_synchronized_count: number; kalshi_ws_connection_state: string; kalshi_ws_seq_gaps: number; };
export type Market = { id: string; asset: string; availability: string; ticker?: string | null; series?: string | null; target?: string | null; window_start?: string | null; window_end?: string | null; seconds_remaining?: number | null; lifecycle: string; official_status?: string | null; yes_bid?: string | null; yes_ask?: string | null; no_bid?: string | null; no_ask?: string | null; last_trade?: string | null; spread?: string | null; quote_age_seconds?: number | null; quote_status: string; quote_source: string; book_verification_state: string; book_sequence?: number | null; quote_source_timestamp?: string | null; quote_received_timestamp?: string | null; projection_available_timestamp?: string | null; socket_event_age_seconds?: number | null; last_market_change_age_seconds?: number | null; source_transport_latency_ms?: string | null; orderbook_status: string; yes_bid_depth: string[][]; no_bid_depth: string[][]; underlying_provider?: string | null; underlying_product?: string | null; underlying_price?: string | null; underlying_age_seconds?: number | null; underlying_status: string; underlying_received_timestamp?: string | null; underlying_persisted_timestamp?: string | null; primary_provider?: string | null; primary_age_seconds?: number | null; secondary_provider?: string | null; secondary_instrument?: string | null; secondary_price?: string | null; secondary_bid?: string | null; secondary_ask?: string | null; secondary_status: string; settlement_followup: string; features: Record<string, Record<string, string | null>>; previous_events: Array<Record<string, string | null>>; };
export type MarketHistory = { schema_version: number; asset: string; ticker: string; window_start: string; window_end: string; underlying: Array<{ observed_at: string; source: string; close_price: string; minimum_price: string; maximum_price: string }>; probability: Array<{ observed_at: string; sequence: number; yes_bid?: string | null; yes_ask?: string | null; no_bid?: string | null; no_ask?: string | null }>; underlying_last_actual_change_at?: string | null; probability_last_actual_change_at?: string | null; notes: string[]; };
export type Account = { id: string; profile: string; status: string; observed_at: string; message?: string | null; summary: { balance_cents?: number | null; portfolio_value_cents?: number | null; today_pnl_cents?: number | null; realized_pnl_cents?: number | null; fees_cents?: number | null; }; positions: Array<Record<string, unknown>>; orders: Array<Record<string, unknown>>; fills: Array<Record<string, unknown>>; };
export type EquityHistory = { profile: string; status: string; points: Array<{ observed_at: string; balance_cents?: number | null; portfolio_value_cents?: number | null }>; notes: string[] };
export type TerminalEvent = { schema_version: number; event_type: 'snapshot' | 'update'; channel: string; asset?: string | null; ticker?: string | null; observed_at: string; authoritative_at?: string | null; sequence: number; payload: Health | Market | Market[] };

const baseUrl = import.meta.env.VITE_API_BASE_URL ?? '';
export async function request<T>(path: string): Promise<T> { const response = await fetch(`${baseUrl}${path}`, { credentials: 'omit' }); if (!response.ok) throw new Error(`API request failed (${response.status})`); return response.json() as Promise<T>; }

export const api = {
  health: () => request<Omit<Health, 'id'>>('/api/health'),
  markets: () => request<Omit<Market, 'id'>[]>('/api/markets'),
  market: (id: string) => request<Omit<Market, 'id'>>(`/api/markets/${encodeURIComponent(id)}`),
  marketHistory: (id: string) => request<MarketHistory>(`/api/markets/${encodeURIComponent(id)}/history`),
  accountSummary: () => request<Omit<Account, 'id'>>('/api/account/summary?profile=production_primary'),
  accountOrders: () => request<Array<Record<string, unknown>>>('/api/account/orders?profile=production_primary'),
  accountFills: () => request<Array<Record<string, unknown>>>('/api/account/fills?profile=production_primary'),
  accountEquityHistory: (range = '1D') => request<EquityHistory>(`/api/account/equity-history?profile=production_primary&range=${encodeURIComponent(range)}`),
  researchAuthority: () => request<Record<string, unknown>>('/api/research-data'),
  researchCoverage: () => request<Record<string, unknown>>('/api/coverage'),
  researchTraining: () => request<Record<string, unknown>>('/api/training'),
  adminData: () => request<Record<string, unknown>>('/api/data'),
  adminStorage: () => request<Record<string, unknown>>('/api/storage'),
  adminOperations: async () => ({ operations: await request<Record<string, unknown>>('/api/operations'), events: await request<Array<Record<string, unknown>>>('/api/events?limit=20') }),
  adminSystem: () => request<Record<string, unknown>>('/api/system'),
};

export const dataProvider: DataProvider = {
  getList: async (resource: string) => { if (resource !== 'markets') throw new Error(`Unsupported collection: ${resource}`); const data = (await api.markets()).map((market) => ({ ...market, id: market.asset })); return { data, total: data.length } as never; },
  getOne: async (resource: string, params: GetOneParams) => { if (resource === 'overview') return { data: { ...(await api.health()), id: 'current' } } as never; if (resource !== 'markets') throw new Error(`Unsupported resource: ${resource}`); const market = await api.market(String(params.id)); return { data: { ...market, id: market.asset } } as never; },
  getMany: async () => ({ data: [] }), getManyReference: async () => ({ data: [], total: 0 }),
  create: async () => { throw new Error('This terminal is read-only'); }, update: async () => { throw new Error('This terminal is read-only'); }, updateMany: async () => { throw new Error('This terminal is read-only'); }, delete: async () => { throw new Error('This terminal is read-only'); }, deleteMany: async () => { throw new Error('This terminal is read-only'); },
};
