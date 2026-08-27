"use strict";

const ASSET_LABELS = Object.freeze({
  BTC: ["BTC", "Bitcoin"], ETH: ["ETH", "Ethereum"], Gold: ["GOLD", "Gold"],
  Silver: ["SILVER", "Silver"], XRP: ["XRP", "XRP"], "WTI Oil": ["WTI", "WTI Oil"],
  SOL: ["SOL", "Solana"], HYPE: ["HYPE", "Hyperliquid"], DOGE: ["DOGE", "Dogecoin"],
  BNB: ["BNB", "BNB"],
});

// Recorder collection cadence is configured server-side and is independent of these
// read-only API refresh intervals. The one-second countdown below performs no request.
const INTERVALS = Object.freeze({ health: 2500, markets: 2500, detail: 2500, events: 15000, system: 30000, coverage: 60000, data: 30000, training: 30000, archive: 10000, storage: 30000, operations: 10000, account: 10000 });
const state = { health: null, markets: null, detail: null, detailAsset: null, coverage: null, data: null, training: null, archive: null, storage: null, operations: null, events: null, system: null, account: null, controlBusy: false, eventFilters: { severity: "", asset: "", source: "", hours: "24" } };
const lastFetched = new Map();
const inFlight = new Map();

const view = document.querySelector("#view");
const notice = document.querySelector("#global-notice");
const title = document.querySelector("#page-title");
const eyebrow = document.querySelector("#page-eyebrow");
const lastRefresh = document.querySelector("#last-refresh");
const sidebarStatus = document.querySelector("#sidebar-status");

function node(tag, className = "", text = null) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== null) element.textContent = text;
  return element;
}

function append(parent, ...children) {
  parent.append(...children.filter(Boolean));
  return parent;
}

function valueOrDash(value, formatter = String) {
  return value === null || value === undefined || value === "" ? "—" : formatter(value);
}

function number(value, digits = 0) {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toLocaleString(undefined, { maximumFractionDigits: digits }) : "—";
}

function bytes(value) {
  if (value === null || value === undefined) return "—";
  let amount = Number(value);
  if (!Number.isFinite(amount)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
  return `${amount.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function naValue(value, formatter = String) {
  return value === null || value === undefined || value === "" ? "N/A" : formatter(value);
}

function percent(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${parsed.toFixed(2)}%` : "N/A";
}

function duration(value) {
  if (value === null || value === undefined) return "—";
  const total = Math.max(0, Math.floor(Number(value)));
  if (!Number.isFinite(total)) return "—";
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m ${seconds}s`;
}

function remainingAt(windowEnd, fallback = null) {
  const end = windowEnd ? new Date(windowEnd) : null;
  if (end && !Number.isNaN(end.valueOf())) return Math.max(0, (end.valueOf() - Date.now()) / 1000);
  return fallback;
}

function countdown(windowEnd, fallback, tag = "span", className = "numeric") {
  const element = node(tag, className, duration(remainingAt(windowEnd, fallback)));
  if (windowEnd) element.dataset.windowEnd = windowEnd;
  return element;
}

function countdownMetric(label, windowEnd, fallback) {
  const item = node("div", "metric");
  append(item, node("span", "label", label), countdown(windowEnd, fallback, "span", "value numeric"));
  return item;
}

function updateCountdowns() {
  if (document.hidden) return;
  document.querySelectorAll("[data-window-end]").forEach((element) => {
    element.textContent = duration(remainingAt(element.dataset.windowEnd));
  });
}

function age(value) {
  if (value === null || value === undefined) return "—";
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  return duration(seconds);
}

function timestamp(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? "—" : parsed.toLocaleString([], { hour12: false });
}

function predictionPrice(value) {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(2)}¢` : "—";
}

function marketPrice(value) {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  return parsed >= 1000 ? parsed.toLocaleString(undefined, { maximumFractionDigits: 4 }) : String(value);
}

function normalizeState(value) {
  return String(value || "missing").toLowerCase().replaceAll(" ", "_");
}

function badge(value, label = null) {
  const normalized = normalizeState(value);
  return node("span", `badge state-${normalized}`, label || String(value || "missing"));
}

function stateLabel(value) {
  return String(value || "missing").replaceAll("_", " ").toUpperCase();
}

function metric(label, value, detail = null) {
  const item = node("div", "metric");
  append(item, node("span", "label", label), node("span", "value", value));
  if (detail) item.append(node("span", "subvalue", detail));
  return item;
}

function kv(label, value, status = null) {
  const item = node("div", "kv");
  item.append(node("span", "label", label));
  item.append(status ? badge(status, value) : node("span", "value", value));
  return item;
}

function sectionHead(name, detail = "") {
  const head = node("div", "section-head");
  append(head, node("h2", "", name), node("span", "", detail));
  return head;
}

function emptyState(titleText, detail) {
  const empty = node("div", "empty");
  append(empty, node("strong", "", titleText), node("span", "", detail));
  return empty;
}

function currentRoute() {
  const parts = (location.hash.replace(/^#\/?/, "") || "overview").split("/").filter(Boolean);
  if (parts[0] === "markets" && parts[1]) return { name: "detail", asset: decodeURIComponent(parts.slice(1).join("/")) };
  if (["markets", "data", "training", "archive", "storage", "operations", "events", "system", "overview", "portfolio", "account", "orders", "history", "watchlist", "analytics", "signals", "models"].includes(parts[0])) return { name: parts[0] };
  return { name: "overview" };
}

function setHeading(route) {
  const headings = {
    dashboard: ["ADMIN · OVERVIEW", "Dashboard"], overview: ["TRADING", "Overview"], portfolio: ["TRADING", "Portfolio"], account: ["TRADING", "Account"], orders: ["TRADING", "Orders"], history: ["TRADING", "History"], watchlist: ["TRADING", "Watchlist"], analytics: ["INTELLIGENCE", "Analytics"], signals: ["INTELLIGENCE", "Signals"], models: ["INTELLIGENCE", "Models"], markets: ["MARKET DATA", "15-Minute Markets"],
    data: ["DATA", "Data Pipeline"], training: ["TRAINING", "Training Truth"], archive: ["ARCHIVE", "Archive"], storage: ["STORAGE", "Storage"], operations: ["OPERATIONS", "Operations"], events: ["OPERATIONS", "Warnings / Errors"], system: ["SYSTEM", "System / Health"],
    detail: ["MARKET DETAIL", `${ASSET_LABELS[route.asset]?.[0] || route.asset} Contract`],
  };
  [eyebrow.textContent, title.textContent] = headings[route.name];
  document.querySelectorAll("nav a").forEach((link) => {
    const target = route.name === "detail" ? "markets" : route.name;
    link.classList.toggle("active", link.dataset.route === target);
  });
}

async function recorderAction(action) {
  if (state.controlBusy || !["start", "pause", "resume"].includes(action)) return;
  state.controlBusy = true;
  render();
  try {
    const response = await fetch(`/api/recorder/${action}`, {
      method: "POST", cache: "no-store", credentials: "omit",
      headers: { "Content-Type": "application/json" }, body: "{}",
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    const outcome = payload.outcome === "already_in_state" ? "already complete" : "completed";
    showNotice(payload.message || `Recorder ${action} ${outcome}.`);
    lastFetched.delete("health");
    await refresh(true);
  } catch (error) {
    // Never repeat a mutating action merely because its HTTP response was lost. Reconcile
    // through the read-only health endpoint; the user can then see whether the requested
    // terminal state was reached without a second Pause/Resume changing control state.
    lastFetched.delete("health");
    try {
      const health = await fetchJson("health", "/api/health", INTERVALS.health, true);
      const reached = action === "pause"
        ? ["paused", "stopped"].includes(health?.recorder_state)
        : ["running", "starting"].includes(health?.recorder_state);
      showNotice(reached
        ? `Recorder ${action} completed; the action response was unavailable.`
        : `Recorder control failed: ${error instanceof Error ? error.message : "unknown error"}`);
    } catch (_healthError) {
      showNotice(`Recorder control result unknown: ${error instanceof Error ? error.message : "unknown error"}. Check status before retrying.`);
    }
  } finally {
    state.controlBusy = false;
    render();
  }
}

function controlButton(label, action, enabled) {
  const button = node("button", "control-button", label);
  button.type = "button";
  button.disabled = !enabled || state.controlBusy;
  button.addEventListener("click", () => recorderAction(action));
  return button;
}

function showNotice(message) {
  notice.textContent = message;
  notice.classList.toggle("hidden", !message);
}

async function fetchJson(key, url, interval, force = false) {
  const now = Date.now();
  if (!force && now - (lastFetched.get(key) || 0) < interval) return state[key];
  if (inFlight.has(key)) return inFlight.get(key);
  const request = (async () => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(url, { cache: "no-store", credentials: "omit", signal: controller.signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      state[key] = payload;
      lastFetched.set(key, Date.now());
      lastRefresh.textContent = `Updated ${new Date().toLocaleTimeString([], { hour12: false })}`;
      return payload;
    } finally {
      clearTimeout(timeout);
      inFlight.delete(key);
    }
  })();
  inFlight.set(key, request);
  return request;
}

function updateSidebar() {
  const health = state.health;
  if (!health) return;
  const status = normalizeState(health.recorder_state);
  sidebarStatus.className = `status-line state-${status}`;
  sidebarStatus.replaceChildren(node("span", "status-icon", status === "running" ? "●" : "◆"), document.createTextNode(`Recorder ${status}`));
}

function marketCard(market) {
  const labels = ASSET_LABELS[market.asset] || [market.asset, market.asset];
  const card = node("a", "market-card");
  card.href = `#/markets/${encodeURIComponent(market.asset)}`;
  card.dataset.asset = market.asset;
  const head = node("div", "card-head");
  const name = node("div");
  append(name, node("div", "asset-code", labels[0]), node("div", "asset-name", labels[1]));
  append(head, name, badge(market.lifecycle));
  const target = node("div", "target-row");
  append(target, append(node("div"), node("span", "label", "Target"), node("strong", "numeric", marketPrice(market.target))), append(node("div"), node("span", "label", "Remaining"), countdown(market.window_end, market.seconds_remaining, "strong")));
  const quotes = node("div", "quote-grid");
  const yes = node("div", "quote-side yes");
  append(yes, node("b", "", "YES · BID / ASK"), node("div", "quote-prices numeric", `${predictionPrice(market.yes_bid)}  ${predictionPrice(market.yes_ask)}`));
  const no = node("div", "quote-side no");
  append(no, node("b", "", "NO · BID / ASK"), node("div", "quote-prices numeric", `${predictionPrice(market.no_bid)}  ${predictionPrice(market.no_ask)}`));
  quotes.append(yes, no);
  const foot = node("div", "card-foot");
  append(foot, node("span", "", `Spread ${predictionPrice(market.spread)}`), node("span", "", `Quote ${age(market.quote_age_seconds)}`));
  const statuses = node("div", "card-status-grid");
  append(statuses, append(node("div"), node("span", "", `Underlying ${marketPrice(market.underlying_price)}`), badge(market.underlying_status, stateLabel(market.underlying_status))), append(node("div"), node("span", "", "Settlement follow-up"), badge(market.settlement_followup)));
  append(card, head, node("span", "ticker", valueOrDash(market.ticker)), target, quotes, foot, statuses);
  return card;
}

function warningItems(health) {
  const items = [];
  if (health?.fatal_task) items.push(["FATAL", `${health.fatal_task}: ${valueOrDash(health.fatal_error_type)}`]);
  for (const source of health?.stale_sources || []) items.push(["STALE", source]);
  for (const [source, reason] of Object.entries(health?.source_failures || {})) items.push(["ERROR", `${source}: ${reason}`]);
  if (health && health.recorder_state !== "running") items.push([health.recorder_state.toUpperCase(), `Recorder is ${health.recorder_state}`]);
  return items;
}

function renderDashboard() {
  const health = state.health;
  const markets = state.markets || [];
  if (!health) return emptyState("Health unavailable", "Waiting for the local heartbeat API.");
  const root = node("div");
  const hero = node("div", "hero-status");
  const stateBox = node("div", "hero-state");
  append(stateBox, node("span", `state-orb state-${normalizeState(health.recorder_state)}`, health.recorder_state === "running" ? "✓" : "!"), append(node("div"), node("span", "label", "Recorder"), node("span", "value", health.recorder_state.toUpperCase()), node("span", "subvalue", health.status)));
  append(hero, stateBox,
    metric("Uptime", duration(health.uptime_seconds)), metric("Heartbeat age", age(health.heartbeat_age_seconds), health.heartbeat_status),
    metric("Database", bytes(health.database_bytes)), metric("WAL", bytes(health.wal_bytes)), metric("Rows written", number(health.written_records)));
  root.append(hero);

  const controls = node("div", "control-strip");
  const recorderState = health.recorder_state;
  append(controls,
    append(node("div"), node("b", "", "Recorder Control"), node("span", "muted", "Graceful · fixed command · localhost only")),
    append(node("div", "control-actions"),
      controlButton("Start Collection", "start", ["stopped", "error"].includes(recorderState)),
      controlButton("Pause Collection", "pause", ["running", "stale"].includes(recorderState)),
      controlButton("Resume Collection", "resume", ["paused", "stopped", "error"].includes(recorderState))));
  root.append(controls);

  const training = state.training || {};
  const rawPool = training.raw_finalized_pool || {};
  const currentPool = training.current_trainable || {};
  const eventCounts = (state.events || []).reduce((counts, item) => { counts[item.severity] = (counts[item.severity] || 0) + 1; return counts; }, {});
  const summary = node("div", "metric-grid");
  append(summary, metric("Finalized events", valueOrDash(rawPool.events, number)), metric("Current trainable rows", valueOrDash(currentPool.rows, number)), metric("Warnings", number(eventCounts.warning || 0)), metric("Errors / fatal", number((eventCounts.error || 0) + (eventCounts.fatal || 0))));
  root.append(sectionHead("Recorder summary", "Operational events are bounded"), summary);

  root.append(sectionHead("Live 15-minute markets", `${markets.length}/10 assets · details in Markets`));
  const marketTable = node("table");
  const marketHead = node("tr");
  ["Asset", "Lifecycle", "Ticker", "Quote", "Underlying", "Remaining"].forEach((item) => marketHead.append(node("th", item === "Asset" ? "" : item === "Remaining" ? "num" : "", item)));
  const marketBody = node("tbody");
  markets.forEach((market) => {
    const link = node("a", "", ASSET_LABELS[market.asset]?.[0] || market.asset);
    link.href = `#/markets/${encodeURIComponent(market.asset)}`;
    append(marketBody, append(node("tr"), append(node("td"), link), append(node("td"), badge(market.lifecycle)), node("td", "ticker", valueOrDash(market.ticker)), append(node("td"), badge(market.quote_status)), append(node("td"), badge(market.underlying_status, stateLabel(market.underlying_status))), countdown(market.window_end, market.seconds_remaining, "td", "num")));
  });
  marketTable.append(append(node("thead"), marketHead), marketBody);
  root.append(markets.length ? append(node("div", "table-wrap"), marketTable) : emptyState("Markets unavailable", "No market projections are available."));

  root.append(sectionHead("Operations snapshot"));
  const split = node("div", "split-grid");
  const operations = node("div", "panel");
  operations.append(append(node("div", "panel-head"), node("h2", "", "Lifecycle & settlement")));
  const operationMetrics = node("div", "panel-body metric-grid");
  const active = Object.values(health.current_markets || {}).filter(Boolean).length;
  append(operationMetrics, metric("Active markets", `${active}/10`), metric("Settlement pending", number(health.active_settlement_followups)), metric("Finalized assets", `${Object.keys(health.last_finalized_settlement || {}).length}/10`), metric("Retries", number(Object.values(health.retry_counts || {}).reduce((sum, item) => sum + item, 0))));
  operations.append(operationMetrics);
  const settlements = node("div", "settlement-strip");
  for (const asset of Object.keys(ASSET_LABELS)) {
    const finalized = health.last_finalized_settlement?.[asset];
    const pill = node("div", "settlement-pill");
    append(pill, node("b", "", ASSET_LABELS[asset][0]), node("span", "", valueOrDash(finalized)));
    settlements.append(pill);
  }
  operations.append(settlements);
  const warnings = node("div", "panel");
  warnings.append(append(node("div", "panel-head"), node("h2", "", "Warnings / source failures"), badge(warningItems(health).length ? "warning" : "healthy")));
  const warningBody = node("div", "panel-body warning-list");
  const items = warningItems(health);
  if (!items.length) warningBody.append(emptyState("✓ All monitored sources nominal", "No stale sources or source failures reported."));
  items.forEach(([kind, message]) => warningBody.append(append(node("div", "warning-item"), node("span", "icon", "◆"), node("span", "", `${kind} · ${message}`))));
  warnings.append(warningBody);
  append(split, operations, warnings);
  root.append(split);
  return root;
}

function renderAccountPage(kind = "overview") {
  const account = state.account;
  if (!account) return emptyState("Account unavailable", "Waiting for the Production account read API.");
  const root = node("div");
  root.append(sectionHead(kind === "portfolio" ? "Portfolio" : "Account overview", "Production · Primary · read-only"));
  const summary = account.summary || {};
  const metrics = node("div", "metric-grid");
  append(metrics, metric("Balance", summary.balance_cents == null ? "N/A" : `${(summary.balance_cents / 100).toFixed(2)} USD`), metric("Portfolio value", summary.portfolio_value_cents == null ? "N/A" : `${(summary.portfolio_value_cents / 100).toFixed(2)} USD`), metric("Today P&L", summary.today_pnl_cents == null ? "N/A" : `${(summary.today_pnl_cents / 100).toFixed(2)} USD`), metric("Status", stateLabel(account.status)));
  root.append(metrics);
  if (kind === "overview") {
    const hero = node("div", "terminal-hero");
    append(hero, node("span", "eyebrow", "PORTFOLIO VALUE"), node("strong", "financial-headline", summary.portfolio_value_cents == null ? "N/A" : `${(summary.portfolio_value_cents / 100).toFixed(2)} USD`), node("span", "hero-change", summary.today_pnl_cents == null ? "N/A today" : `${summary.today_pnl_cents >= 0 ? "+" : ""}${(summary.today_pnl_cents / 100).toFixed(2)} USD today`));
    root.prepend(hero);
    const chart = node("div", "terminal-chart"); append(chart, node("div", "chart-title", "Portfolio · synchronized evidence"), node("div", "chart-grid", "No historical chart series available · N/A")); root.append(chart);
  }
  if (kind === "portfolio") {
    root.append(sectionHead("Positions", "Authoritative account positions; marks are not fabricated"));
    const wrap = node("div", "table-wrap"); const table = node("table"); const head = node("tr"); ["Market", "Position", "Exposure", "Realized P&L", "Mark"].forEach((x) => head.append(node("th", x === "Market" ? "" : "num", x))); const body = node("tbody");
    (account.positions || []).forEach((p) => append(body, append(node("tr"), node("td", "", p.ticker), node("td", "num", valueOrDash(p.position, number)), node("td", "num", p.market_exposure_cents == null ? "N/A" : `${(p.market_exposure_cents / 100).toFixed(2)}`), node("td", "num", p.realized_pnl_cents == null ? "N/A" : `${(p.realized_pnl_cents / 100).toFixed(2)}`), node("td", "num", p.mark_cents == null ? "N/A" : `${(p.mark_cents / 100).toFixed(2)}`))));
    table.append(append(node("thead"), head), body); wrap.append(table); root.append(wrap);
  } else {
    root.append(sectionHead("Account connectivity", "Credentials remain server-side; no order actions are exposed"));
    root.append(node("div", "panel panel-body", account.message || "Production account reads use the official Kalshi SDK boundary."));
  }
  return root;
}

function renderReadOnlyShell(name) {
  const account = state.account;
  if (!account) return emptyState(`${name} unavailable`, "Waiting for the Production account read API.");
  const root = node("div"); root.append(sectionHead(name, "Production · Primary · read-only"));
  if (["Watchlist", "Analytics", "Signals", "Models"].includes(name)) {
    const copy = { Watchlist: "Saved markets are kept locally; no remote writes are performed.", Analytics: "LIVE15 analytics are derived only from available recorder evidence.", Signals: "Approved signal outputs only; unavailable signals remain N/A.", Models: "Model registry evidence is read-only; no training or promotion controls are exposed." }[name];
    root.append(node("div", "panel panel-body", copy));
    const facts = node("div", "metric-grid"); append(facts, metric("Status", "N/A"), metric("Source", "LIVE15"), metric("Freshness", "N/A"), metric("Permission", "READ ONLY")); root.append(facts); return root;
  }
  const rows = name === "Orders" ? (account.orders || []) : name === "History" ? [...(account.fills || []), ...(account.ledger || [])] : (account.fills || []);
  const table = node("table"); const head = node("tr"); [name === "Orders" ? "Order" : "Fill", "Market", "Status / Side", "Count", "Price", "Time"].forEach((x) => head.append(node("th", x === "Count" || x === "Price" ? "num" : "", x))); const body = node("tbody");
  rows.forEach((item) => append(body, append(node("tr"), node("td", "", item.order_id || item.trade_id || item.reference || item.entry_type || "N/A"), node("td", "", item.ticker || "N/A"), node("td", "", item.status || item.side || item.entry_type || "N/A"), node("td", "num", valueOrDash(item.count, number)), node("td", "num", item.yes_price_cents == null ? (item.amount_cents == null ? "N/A" : `${(item.amount_cents / 100).toFixed(2)}`) : `${item.yes_price_cents}¢`), node("td", "", timestamp(item.created_at || item.observed_at)))));
  table.append(append(node("thead"), head), body); root.append(append(node("div", "table-wrap"), table));
  if (!rows.length) root.append(emptyState("N/A", `No verified ${name.toLowerCase()} facts are available.`));
  return root;
}

function renderEvents() {
  const root = node("div");
  root.append(sectionHead("Warnings / errors / fatal", "Newest 100 · refresh 15s · normal ticks are omitted"));
  const filters = node("div", "event-filters");
  const choices = [
    ["Severity", "severity", [["", "All"], ["info", "Info"], ["warning", "Warning"], ["error", "Error"], ["fatal", "Fatal"]]],
    ["Asset", "asset", [["", "All assets"], ...Object.keys(ASSET_LABELS).map((asset) => [asset, ASSET_LABELS[asset][0]])]],
    ["Time", "hours", [["1", "Last hour"], ["24", "Last 24 hours"], ["168", "Last 7 days"], ["", "All retained"]]],
  ];
  choices.forEach(([label, key, options]) => {
    const wrapper = node("label", "filter-control");
    wrapper.append(node("span", "label", label));
    const select = node("select");
    options.forEach(([value, text]) => {
      const option = node("option", "", text); option.value = value; option.selected = state.eventFilters[key] === value; select.append(option);
    });
    select.addEventListener("change", () => setEventFilter(key, select.value));
    wrapper.append(select); filters.append(wrapper);
  });
  const source = node("label", "filter-control");
  source.append(node("span", "label", "Exact source"));
  const sourceInput = node("input"); sourceInput.type = "text"; sourceInput.maxLength = 160; sourceInput.placeholder = "e.g. kalshi_quote:BTC"; sourceInput.value = state.eventFilters.source;
  sourceInput.addEventListener("change", () => setEventFilter("source", sourceInput.value.trim()));
  source.append(sourceInput); filters.append(source); root.append(filters);
  const events = state.events || [];
  const wrap = node("div", "table-wrap");
  const table = node("table");
  const head = node("tr");
  ["Timestamp", "Severity", "Asset / source", "Event", "Error type", "Message"].forEach((item) => head.append(node("th", "", item)));
  const body = node("tbody");
  events.forEach((item) => append(body, append(node("tr"), node("td", "", timestamp(item.timestamp)), append(node("td"), badge(item.severity)), node("td", "", valueOrDash(item.asset || item.source)), node("td", "", item.event_type.replaceAll("_", " ")), node("td", "", valueOrDash(item.error_type)), node("td", "", item.message))));
  table.append(append(node("thead"), head), body); wrap.append(table);
  root.append(events.length ? wrap : emptyState("No operational warnings", "No bounded diagnostic events match the current view."));
  return root;
}

async function setEventFilter(key, value) {
  state.eventFilters[key] = value;
  const pending = inFlight.get("events");
  if (pending) await pending.catch(() => null);
  lastFetched.delete("events");
  await refresh(true);
}

function eventsUrl() {
  const parameters = new URLSearchParams({ limit: "100" });
  if (state.eventFilters.severity) parameters.set("severity", state.eventFilters.severity);
  if (state.eventFilters.asset) parameters.set("asset", state.eventFilters.asset);
  if (state.eventFilters.source) parameters.set("source", state.eventFilters.source);
  if (state.eventFilters.hours) parameters.set("since", new Date(Date.now() - Number(state.eventFilters.hours) * 3600000).toISOString());
  return `/api/events?${parameters}`;
}

function renderMarkets() {
  const markets = state.markets || [];
  const root = node("div");
  root.append(sectionHead("All target assets", `${markets.length}/10 · Kalshi official market data`));
  const filter = node("input", "table-filter"); filter.type = "search"; filter.placeholder = "Filter assets or tickers…"; filter.addEventListener("input", () => { const query = filter.value.toLowerCase(); root.querySelectorAll("tbody tr").forEach((row) => { row.hidden = query && !row.textContent.toLowerCase().includes(query); }); }); root.append(filter);
  const wrap = node("div", "table-wrap");
  const table = node("table");
  const header = node("tr");
  ["Asset", "Lifecycle", "Ticker", "Target", "Remaining", "YES bid", "YES ask", "NO bid", "NO ask", "Spread", "Quote age", "Underlying", "Follow-up"].forEach((item) => header.append(node("th", item === "Asset" || item === "Lifecycle" || item === "Ticker" ? "" : "num", item)));
  const body = node("tbody");
  markets.forEach((market) => {
    const row = node("tr");
    const assetCell = node("td");
    const link = node("a", "", ASSET_LABELS[market.asset]?.[0] || market.asset);
    link.href = `#/markets/${encodeURIComponent(market.asset)}`;
    assetCell.append(link);
    append(row, assetCell, append(node("td"), badge(market.lifecycle)), node("td", "ticker", valueOrDash(market.ticker)), node("td", "num", marketPrice(market.target)), countdown(market.window_end, market.seconds_remaining, "td", "num"), node("td", "num", predictionPrice(market.yes_bid)), node("td", "num", predictionPrice(market.yes_ask)), node("td", "num", predictionPrice(market.no_bid)), node("td", "num", predictionPrice(market.no_ask)), node("td", "num", predictionPrice(market.spread)), node("td", "num", age(market.quote_age_seconds)), node("td", "num", valueOrDash(market.underlying_price, marketPrice)), append(node("td"), badge(market.settlement_followup)));
    body.append(row);
  });
  table.append(append(node("thead"), header), body);
  wrap.append(table);
  root.append(markets.length ? wrap : emptyState("Markets unavailable", "The raw recorder store has no current market rows."));
  return root;
}

function bookPanel(titleText, levels) {
  const book = node("div", "book");
  book.append(node("h3", "", titleText));
  (levels || []).slice(0, 5).forEach((level) => {
    const [price, quantity] = level;
    append(book, append(node("div", "book-row"), node("span", "", predictionPrice(price)), node("span", "", valueOrDash(quantity))));
  });
  if (!levels?.length) book.append(emptyState("Missing", "No public depth level available."));
  return book;
}

function renderDetail(route) {
  const market = state.detailAsset === route.asset ? state.detail : null;
  if (!market) return emptyState("Market detail unavailable", "Waiting for the typed asset API.");
  const root = node("div");
  const back = node("a", "back-link", "← Back to all markets");
  back.href = "#/markets";
  root.append(back);
  const metrics = node("div", "metric-grid");
  append(metrics, metric("Lifecycle", market.lifecycle.toUpperCase(), market.official_status || "official status missing"), countdownMetric("Time remaining", market.window_end, market.seconds_remaining), metric("Target", marketPrice(market.target)), metric("Quote age", age(market.quote_age_seconds), market.quote_status));
  root.append(metrics, sectionHead("Contract & prices", valueOrDash(market.ticker)));
  const tabs = node("div", "detail-tabs");
  ["Chart", "Order Book", "Position", "History", "LIVE15 Analysis"].forEach((label, index) => {
    const tab = node("a", `detail-tab${index === 0 ? " active" : ""}`, label);
    tab.href = index === 2 ? "#/portfolio" : `#/markets/${encodeURIComponent(route.asset)}`;
    tabs.append(tab);
  });
  root.append(tabs);
  const owned = (state.account?.positions || []).find((item) => item.ticker === market.ticker);
  root.append(node("div", "panel panel-body", owned ? `Position ${valueOrDash(owned.position, number)} · P&L ${owned.realized_pnl_cents == null ? "N/A" : `${(owned.realized_pnl_cents / 100).toFixed(2)} USD`}` : "Position: N/A · no verified holding for this market"));
  const chart = node("div", "panel panel-body chart-panel");
  chart.append(node("h2", "", "Chart"), node("p", "muted", "Real synchronized quote points are shown when available; no interpolation is applied."));
  const chartValues = [market.yes_bid, market.yes_ask, market.last_trade].filter((value) => value != null);
  chart.append(node("div", "chart-values", chartValues.length ? chartValues.map((value) => predictionPrice(value)).join("  ·  ") : "N/A"));
  root.append(chart);
  const detail = node("div", "detail-grid");
  const contract = node("div", "panel");
  contract.append(append(node("div", "panel-head"), node("h2", "", "Contract metadata")));
  const contractGrid = node("div", "panel-body kv-grid");
  append(contractGrid, kv("Asset", market.asset), kv("Series", valueOrDash(market.series)), kv("Ticker", valueOrDash(market.ticker)), kv("Availability", market.availability, market.availability), kv("Window start", timestamp(market.window_start)), kv("Window end", timestamp(market.window_end)), kv("Settlement follow-up", market.settlement_followup, market.settlement_followup), kv("Quote source time", valueOrDash(market.quote_source_timestamp)));
  contract.append(contractGrid);
  const quote = node("div", "panel");
  quote.append(append(node("div", "panel-head"), node("h2", "", "Kalshi quote"), badge(market.quote_status)));
  const quoteGrid = node("div", "panel-body kv-grid");
  append(quoteGrid, kv("YES bid", predictionPrice(market.yes_bid)), kv("YES ask", predictionPrice(market.yes_ask)), kv("NO bid", predictionPrice(market.no_bid)), kv("NO ask", predictionPrice(market.no_ask)), kv("Last trade", predictionPrice(market.last_trade)), kv("YES spread", predictionPrice(market.spread)), kv("Received", timestamp(market.quote_received_timestamp)), kv("Orderbook", market.orderbook_status, market.orderbook_status));
  quote.append(quoteGrid);
  append(detail, contract, quote);
  root.append(detail, sectionHead("Orderbook top levels", "Official public depth · no executable Robinhood claim"));
  const books = node("div", "panel panel-body orderbooks");
  append(books, bookPanel("YES bids · price / quantity", market.yes_bid_depth), bookPanel("NO bids · price / quantity", market.no_bid_depth));
  root.append(books, sectionHead("Underlying & leakage-safe features", "Existing FeatureEngine projection"));
  const featurePanel = node("div", "panel panel-body");
  const underlying = node("div", "metric-grid");
  append(underlying, metric("Primary", valueOrDash(market.primary_provider || market.underlying_provider)), metric("Primary product", valueOrDash(market.underlying_product)), metric("Primary price", marketPrice(market.underlying_price)), metric("Primary age", age(market.primary_age_seconds ?? market.underlying_age_seconds)), metric("Primary status", stateLabel(market.underlying_status)));
  if (market.secondary_provider) {
    append(underlying, metric("Secondary", valueOrDash(market.secondary_provider)), metric("Secondary instrument", valueOrDash(market.secondary_instrument)), metric("Secondary price", marketPrice(market.secondary_price)), metric("Secondary bid / ask", `${marketPrice(market.secondary_bid)} / ${marketPrice(market.secondary_ask)}`), metric("Secondary age", age(market.secondary_age_seconds)), metric("Secondary status", market.secondary_status.toUpperCase()), metric("Source clock", market.secondary_clock_skew ? "CLOCK SKEW · cross-clock latency invalid" : "ALIGNED"), metric("Secondary − primary", marketPrice(market.primary_secondary_price_diff)), metric("Age difference", age(market.primary_secondary_age_diff)), metric("Source → receive", market.secondary_source_receive_latency_ms === null ? "N/A" : `${market.secondary_source_receive_latency_ms} ms`), metric("Receive → persist", market.secondary_receive_persist_latency_ms === null ? "N/A" : `${market.secondary_receive_persist_latency_ms} ms`));
  }
  featurePanel.append(underlying);
  const featureList = node("div", "feature-list");
  for (const [name, observation] of Object.entries(market.features || {})) {
    const item = node("div", "feature");
    append(item, node("span", "label", name.replaceAll("_", " ")), node("b", "", valueOrDash(observation.value)), badge(observation.status));
    featureList.append(item);
  }
  featurePanel.append(featureList.childElementCount ? featureList : emptyState("Features unavailable", "Missing or insufficient source lookback is not filled with zero."));
  root.append(featurePanel, sectionHead("Recent finalized events", "Official settlement truth"));
  const previous = node("div", "table-wrap");
  const previousTable = node("table");
  const previousHead = node("tr");
  ["Ticker", "Target", "Result", "Window end", "Settled"].forEach((item) => previousHead.append(node("th", "", item)));
  const previousBody = node("tbody");
  (market.previous_events || []).forEach((event) => append(previousBody, append(node("tr"), node("td", "", valueOrDash(event.ticker)), node("td", "num", marketPrice(event.target)), append(node("td"), badge(event.result)), node("td", "", timestamp(event.window_end)), node("td", "", timestamp(event.settlement_timestamp)))));
  previousTable.append(append(node("thead"), previousHead), previousBody);
  previous.append(previousTable);
  root.append(previousBody.childElementCount ? previous : emptyState("No finalized events yet", "Settlement pending is never converted into a guessed label."));
  return root;
}

function ratesPanel(titleText, values) {
  const panel = node("div", "panel");
  panel.append(append(node("div", "panel-head"), node("h2", "", titleText)));
  const body = node("div", "panel-body");
  const entries = Object.entries(values || {});
  if (!entries.length || entries.every(([, value]) => value === null)) body.append(emptyState("N/A", "No completed dataset diagnostics available."));
  else for (const [name, raw] of entries) {
    const numeric = raw === null ? null : Number(raw);
    const row = node("div", "bar-row");
    const progress = node("progress", "bar-track");
    progress.max = 1;
    if (numeric !== null && Number.isFinite(numeric)) progress.value = Math.min(1, numeric);
    append(row, node("span", "", name.replaceAll("_", " ")), progress, node("span", "bar-value", numeric === null || !Number.isFinite(numeric) ? "N/A" : `${(numeric * 100).toFixed(1)}%`));
    body.append(row);
  }
  panel.append(body);
  return panel;
}

function renderTraining() {
  const coverage = state.coverage;
  if (!coverage) return emptyState("Training coverage unavailable", "Waiting for the low-frequency coverage API.");
  const root = node("div");
  const strip = node("div", "schema-strip");
  append(strip, node("span", "", `Dataset schema · ${coverage.dataset_version}`), node("span", "", `Feature schema · ${coverage.feature_schema_version}`), node("span", "", `Build · ${valueOrDash(coverage.build_id)}`), node("span", "", `Completed · ${timestamp(coverage.completed_timestamp)}`), node("span", "", `Snapshot · ${valueOrDash(coverage.snapshot_status)}`));
  root.append(strip);
  const insufficient = coverage.status !== "available" || coverage.training_rows === 0 || coverage.trainable_events === 0;
  if (insufficient) root.append(emptyState("Not enough training data yet", "The recorder can continue collecting raw truth; missing diagnostics remain N/A."));
  const metrics = node("div", "metric-grid");
  append(metrics, metric("Finalized events", number(coverage.finalized_events)), metric("Evaluated snapshot", number(coverage.snapshot_finalized_events)), metric("Unevaluated finalized", number(coverage.unevaluated_finalized_events)), metric("Trainable events", number(coverage.trainable_events)), metric("Training rows", number(coverage.training_rows)), metric("Coverage status", coverage.status));
  root.append(sectionHead("Historical dataset coverage", "Immutable snapshot · refresh 60s · live source readiness is shown on Markets/System"), metrics, sectionHead("Per-asset events and rows"));
  const wrap = node("div", "table-wrap");
  const table = node("table");
  const head = node("tr");
  ["Asset", "Finalized", "Evaluated", "Unevaluated", "Trainable", "Rows"].forEach((item) => head.append(node("th", item === "Asset" ? "" : "num", item)));
  const body = node("tbody");
  for (const asset of Object.keys(ASSET_LABELS)) {
    const item = coverage.per_asset?.[asset] || {};
    append(body, append(node("tr"), node("td", "", ASSET_LABELS[asset][0]), node("td", "num", number(item.finalized_events)), node("td", "num", number(item.evaluated_finalized_events)), node("td", "num", number(item.unevaluated_finalized_events)), node("td", "num", number(item.trainable_events)), node("td", "num", number(item.training_rows))));
  }
  table.append(append(node("thead"), head), body); wrap.append(table); root.append(wrap);
  root.append(sectionHead("Label & decision-time coverage"));
  const diagnostics = node("div", "detail-grid");
  const labelPanel = node("div", "panel");
  labelPanel.append(append(node("div", "panel-head"), node("h2", "", "Official YES / NO labels")));
  const labelBody = node("div", "panel-body metric-grid");
  append(labelBody, metric("YES", insufficient ? "N/A" : number(coverage.label_balance?.yes)), metric("NO", insufficient ? "N/A" : number(coverage.label_balance?.no)));
  labelPanel.append(labelBody);
  const buckets = node("div", "panel");
  buckets.append(append(node("div", "panel-head"), node("h2", "", "Decision buckets")));
  const bucketBody = node("div", "panel-body");
  if (!coverage.decision_time_bucket_coverage) bucketBody.append(emptyState("N/A", "No trainable decision rows yet."));
  else Object.entries(coverage.decision_time_bucket_coverage).forEach(([seconds, rows]) => bucketBody.append(append(node("div", "book-row"), node("span", "", `T−${seconds}s`), node("span", "", `${number(rows)} rows`))));
  buckets.append(bucketBody); append(diagnostics, labelPanel, buckets); root.append(diagnostics, sectionHead("Feature quality rates", "Missing is never shown as zero"));
  const rates = node("div", "detail-grid");
  append(rates, ratesPanel("Missing feature rates", coverage.missing_feature_rates), ratesPanel("Stale feature rates", coverage.stale_feature_rates));
  root.append(rates);
  root.append(sectionHead("Trainability diagnostics", "Persisted by the leakage-safe DatasetBuilder"));
  const rejectionPanel = node("div", "panel panel-body");
  if (coverage.snapshot_status === "outdated" && coverage.unevaluated_finalized_events > 0) {
    rejectionPanel.append(emptyState("Finalized events await evaluation", `${number(coverage.unevaluated_finalized_events)} finalized events arrived after the latest immutable dataset snapshot. They have not been rejected.`));
  }
  const rejections = Object.entries(coverage.trainability_rejections || {});
  rejections.forEach(([reason, count]) => rejectionPanel.append(append(node("div", "book-row"), node("span", "", reason.replaceAll("_", " ")), node("span", "", number(count)))));
  if (!rejections.length && coverage.snapshot_status !== "outdated") rejectionPanel.append(emptyState("No rejection breakdown available", "Run a current DatasetBuilder snapshot to persist machine-readable eligibility diagnostics."));
  if (coverage.skipped_decisions !== null && coverage.skipped_decisions !== undefined) rejectionPanel.append(node("p", "muted", `${number(coverage.skipped_decisions)} decision samples skipped; ${number(coverage.events_without_training_rows)} evaluated events produced no rows.`));
  root.append(rejectionPanel);
  return root;
}

function renderSystem() {
  const health = state.health;
  const system = state.system;
  const markets = state.markets || [];
  if (!health || !system) return emptyState("System data unavailable", "Waiting for health and system APIs.");
  const root = node("div");
  const metrics = node("div", "metric-grid");
  append(metrics, metric("Heartbeat", health.heartbeat_status.toUpperCase(), `${age(health.heartbeat_age_seconds)} ago`), metric("Raw store", system.raw_store.toUpperCase()), metric("Feature store", system.feature_store.toUpperCase()), metric("API mode", system.api_mode.toUpperCase(), system.bind_host));
  root.append(metrics, sectionHead("Recorder health", `Observed ${timestamp(health.observed_at)}`));
  const detail = node("div", "detail-grid");
  const heartbeat = node("div", "panel");
  heartbeat.append(append(node("div", "panel-head"), node("h2", "", "Heartbeat & storage"), badge(health.recorder_state)));
  const heartbeatGrid = node("div", "panel-body kv-grid");
  append(heartbeatGrid, kv("Recorder state", health.recorder_state, health.recorder_state), kv("Reported status", health.status, health.status), kv("Uptime", duration(health.uptime_seconds)), kv("Last activity", timestamp(health.observed_at)), kv("Database size", bytes(health.database_bytes)), kv("WAL size", bytes(health.wal_bytes)), kv("Rows written", number(health.written_records)), kv("Settlement pending", number(health.active_settlement_followups)), kv("Fatal task", valueOrDash(health.fatal_task)), kv("Fatal error", valueOrDash(health.fatal_error_type)));
  heartbeat.append(heartbeatGrid);
  const sources = node("div", "panel");
  sources.append(append(node("div", "panel-head"), node("h2", "", "Retries & source failures"), badge(warningItems(health).length ? "warning" : "healthy")));
  const sourceBody = node("div", "panel-body");
  const retryEntries = Object.entries(health.retry_counts || {});
  const failureEntries = Object.entries(health.source_failures || {});
  if (!retryEntries.length && !failureEntries.length) sourceBody.append(emptyState("✓ No active failures", "No retry or source-failure state reported."));
  retryEntries.forEach(([source, count]) => sourceBody.append(append(node("div", "book-row"), node("span", "", source), node("span", "", `${count} retries`))));
  failureEntries.forEach(([source, reason]) => sourceBody.append(append(node("div", "warning-item"), node("span", "icon", "◆"), node("span", "", `${source}: ${reason}`))));
  sources.append(sourceBody); append(detail, heartbeat, sources); root.append(detail);
  const runtimeComponents = Object.entries(system.runtime_components || {});
  root.append(sectionHead("Runtime components", "Supervisor-owned process and heartbeat truth"));
  const runtimeGrid = node("div", "metric-grid");
  if (!runtimeComponents.length) runtimeGrid.append(emptyState("Supervisor status unavailable", "Runtime components have not been adopted by the supervisor yet."));
  runtimeComponents.forEach(([name, component]) => runtimeGrid.append(metric(name.replaceAll("_", " ").toUpperCase(), stateLabel(component.status), `PID ${valueOrDash(component.pid)} · heartbeat ${age(component.heartbeat_age_seconds)}`)));
  root.append(runtimeGrid, sectionHead("Per-asset freshness", "Quote and predictive underlying are independent roles"));
  const wrap = node("div", "table-wrap");
  const table = node("table");
  const head = node("tr");
  ["Asset", "Lifecycle", "Quote", "Quote age", "Underlying", "Underlying age", "Settlement follow-up"].forEach((item) => head.append(node("th", item.includes("age") ? "num" : "", item)));
  const body = node("tbody");
  markets.forEach((market) => append(body, append(node("tr"), node("td", "", ASSET_LABELS[market.asset]?.[0] || market.asset), append(node("td"), badge(market.lifecycle)), append(node("td"), badge(market.quote_status)), node("td", "num", age(market.quote_age_seconds)), append(node("td"), badge(market.underlying_status, stateLabel(market.underlying_status))), node("td", "num", age(market.underlying_age_seconds)), append(node("td"), badge(market.settlement_followup)))));
  table.append(append(node("thead"), head), body); wrap.append(table); root.append(wrap);
  return root;
}

function renderProjectionCard(titleText, projection, detailText = "") {
  const panel = node("div", "panel projection-card");
  const head = node("div", "panel-head");
  append(head, node("h2", "", titleText), badge(projection?.state || "unknown", stateLabel(projection?.status || "UNKNOWN")));
  panel.append(head);
  const body = node("div", "panel-body");
  append(body,
    metric("Events", valueOrDash(projection?.events, number)),
    metric("Eligible", valueOrDash(projection?.eligible_events, number)),
    metric("Rows", valueOrDash(projection?.rows, number)),
    metric("Assets", valueOrDash(projection?.assets, number)),
    metric("Reason", valueOrDash(projection?.reason_code)),
  );
  if (detailText) body.append(node("p", "muted", detailText));
  panel.append(body);
  return panel;
}

function renderData() {
  const data = state.data;
  const training = state.training;
  if (!data || !training) return emptyState("Data projections unavailable", "Waiting for the read-only data APIs.");
  const root = node("div");
  root.append(sectionHead("Data pipeline", "Source truth and derived layers are intentionally separated."));
  const summary = node("div", "metric-grid");
  append(summary, metric("Finalized events", valueOrDash(data.finalized_events, number)), metric("Finalized assets", valueOrDash(data.finalized_assets, number)), metric("Raw store", stateLabel(data.raw_store)), metric("Observed through", timestamp(data.source_as_of)));
  root.append(summary);
  const grid = node("div", "detail-grid");
  grid.append(renderProjectionCard("Raw finalized pool", training.raw_finalized_pool, "Official settlement rows. This is not the current trainable projection."));
  grid.append(renderProjectionCard("Current trainable projection", training.current_trainable, "Mutable materializer output; unavailable means N/A, never zero."));
  root.append(grid, sectionHead("Data contract", "Missing, stale, and unavailable values remain explicit."));
  const contract = node("div", "panel panel-body prose");
  contract.append(node("p", "", "Raw finalized settlement truth is authoritative. Trainability is a separate projection with its own checkpoint and schema."));
  contract.append(node("p", "", "No settlement, future row, or synthetic value is exposed as a predictive feature by this view."));
  root.append(contract);
  return root;
}

function renderTrainingV2() {
  const training = state.training;
  if (!training) return emptyState("Training truth unavailable", "Waiting for the typed training API.");
  const root = node("div");
  const snapshot = training.latest_completed_dataset || {};
  root.append(sectionHead("Training truth", "Read-only evidence layers · no training controls"));
  const banner = node("div", `callout state-${normalizeState(snapshot.state || "unknown")}`);
  append(banner, node("strong", "", snapshot.status ? stateLabel(snapshot.status) : "UNKNOWN"), node("span", "", valueOrDash(snapshot.reason_code)));
  root.append(banner);
  const layers = node("div", "detail-grid");
  layers.append(renderProjectionCard("Raw finalized pool", training.raw_finalized_pool, "New finalized events remain visible even when a snapshot is stale."));
  layers.append(renderProjectionCard("Current trainable", training.current_trainable, "Mutable, checkpointed materializer output."));
  const snapshotPanel = node("div", "panel projection-card");
  snapshotPanel.append(append(node("div", "panel-head"), node("h2", "", "Latest completed snapshot"), badge(snapshot.snapshot_status || "unknown")));
  const snapshotBody = node("div", "panel-body metric-grid");
  append(snapshotBody, metric("Build", valueOrDash(snapshot.build_id)), metric("Dataset", valueOrDash(snapshot.dataset_version)), metric("Rows", valueOrDash(snapshot.rows, number)), metric("Events", valueOrDash(snapshot.events, number)), metric("Completed", timestamp(snapshot.completed_timestamp)));
  snapshotPanel.append(snapshotBody); layers.append(snapshotPanel);
  root.append(layers);
  root.append(sectionHead("Frozen experiment facts", "Only explicitly persisted experiment records appear here."));
  const facts = node("div", "panel panel-body");
  if (!training.frozen_experiment_facts?.length) facts.append(emptyState("N/A", "No frozen experiment fact records are persisted in the local read-only store."));
  (training.frozen_experiment_facts || []).forEach((fact) => facts.append(append(node("div", "book-row"), node("span", "", fact.experiment_id), badge(fact.status), node("span", "muted", valueOrDash(fact.dataset_id)))));
  root.append(facts);
  root.append(sectionHead("Per-asset current projection", "Rows and events are reported independently."));
  const table = node("table");
  const head = node("tr"); ["Asset", "Events", "Rows", "Eligible", "Ineligible"].forEach((item) => head.append(node("th", item === "Asset" ? "" : "num", item)));
  const body = node("tbody");
  for (const asset of Object.keys(ASSET_LABELS)) {
    const item = training.current_trainable?.per_asset?.[asset] || {};
    append(body, append(node("tr"), node("td", "", ASSET_LABELS[asset][0]), node("td", "num", valueOrDash(item.events, number)), node("td", "num", valueOrDash(item.rows, number)), node("td", "num", valueOrDash(item.eligible_events, number)), node("td", "num", valueOrDash(item.ineligible_events, number))));
  }
  table.append(append(node("thead"), head), body); root.append(append(node("div", "table-wrap"), table));
  return root;
}

function renderArchive() {
  const archive = state.archive;
  if (!archive) return emptyState("Archive state unavailable", "Waiting for the archive health API.");
  const root = node("div");
  root.append(sectionHead("Archive", "Verified history and purge eligibility · dry run only"));
  const status = node("div", "panel panel-body kv-grid");
  append(status, kv("Archive status", stateLabel(archive.state)), kv("Poll Mode", naValue(archive.poll_mode, stateLabel)), kv("Next poll", naValue(archive.next_poll_seconds, (v) => `${number(v, 1)} s`)), kv("Backlog", naValue(archive.backlog_events, number)), kv("Throughput", naValue(archive.throughput_events_per_second, (v) => `${number(v, 2)}/s`)), kv("Verified", naValue(archive.verified_chunks, number)), kv("Failed", naValue(archive.failed_chunks, number)), kv("Waiting", naValue(archive.waiting_chunks, number)), kv("Quarantine", naValue(archive.quarantined_chunks, number))); root.append(status);

  root.append(sectionHead("Adaptive cadence", "Current mode is highlighted; cadence remains read-only."));
  const cadence = node("div", "table-wrap");
  const cadenceTable = node("table");
  const cadenceHead = node("tr"); ["Mode", "Interval", "Meaning"].forEach((item) => cadenceHead.append(node("th", item)));
  const cadenceBody = node("tbody");
  [["CATCH_UP", "2 s", "Backlog is material"], ["ACTIVE", "5 s", "Archive is active"], ["NEAR_CAUGHT_UP", "10 s", "Backlog is small"], ["IDLE", "≤60 s", "No work is pending"], ["BACKPRESSURE", "2 s defer", "Recorder pressure"]].forEach(([mode, interval, meaning]) => { const row = node("tr", mode === archive.poll_mode ? "current-row" : ""); append(row, node("td", "", mode === archive.poll_mode ? `● ${mode}` : mode), node("td", "", interval), node("td", "muted", meaning)); cadenceBody.append(row); });
  cadenceTable.append(append(node("thead"), cadenceHead), cadenceBody); cadence.append(cadenceTable); root.append(cadence);

  root.append(sectionHead("Archive compression", "Only authoritative archive byte totals are used."));
  const compression = node("div", "panel panel-body kv-grid");
  append(compression, kv("Uncompressed equivalent", naValue(archive.uncompressed_archive_bytes, bytes)), kv("Compressed archive", naValue(archive.compressed_archive_bytes, bytes)), kv("Compression ratio", naValue(archive.compression_ratio, (v) => `${number(v, 2)}×`)), kv("Bytes saved", naValue(archive.compressed_bytes_saved, bytes)), kv("Saving", naValue(archive.compression_saving_percent, percent))); root.append(compression);

  root.append(sectionHead("Archive states", "Counts are source facts; unavailable values remain N/A."));
  const states = node("div", "table-wrap"); const stateTable = node("table"); const stateHead = node("tr"); ["State", "Chunks", "Meaning"].forEach((item) => stateHead.append(node("th", item))); const stateBody = node("tbody");
  [["VERIFIED", archive.verified_chunks, "Replay and checksum verified"], ["PURGE_ELIGIBLE", archive.purge_eligible_chunks, "Eligible for dry-run purge"], ["PURGED", archive.purged_chunks, "Purged chunk count"], ["QUARANTINED", archive.quarantined_chunks, "Preserved but unreplayable"], ["FAILED", archive.failed_chunks, "Verification failed"], ["WAITING", archive.waiting_chunks, "Waiting for replay baseline"]].forEach(([stateName, count, meaning]) => { const row = node("tr"); append(row, node("td", "", stateName), node("td", "num", naValue(count, number)), node("td", "muted", meaning)); stateBody.append(row); }); stateTable.append(append(node("thead"), stateHead), stateBody); states.append(stateTable); root.append(states);

  root.append(sectionHead("Purge facts", "Dry-run projection only; no destructive controls."));
  const purge = node("div", "panel panel-body kv-grid"); append(purge, kv("Purge eligible chunks", naValue(archive.purge_eligible_chunks, number)), kv("Purged chunks", naValue(archive.purged_chunks, number)), kv("Total purged events", naValue(archive.total_purged_events, number)), kv("Last deleted events", naValue(archive.last_purge_deleted_events, number)), kv("Last purge duration", naValue(archive.last_purge_duration_seconds, (v) => `${number(v, 2)} s`)), kv("Last reusable bytes", naValue(archive.last_purge_reusable_bytes, bytes))); purge.append(node("p", "muted", "Verified spans never cross failed or quarantined ranges. Purge and compaction actions are not exposed.")); root.append(purge);
  return root;
}

function renderStorage() {
  const storage = state.storage;
  if (!storage) return emptyState("Storage state unavailable", "Waiting for the storage health API.");
  const root = node("div"); root.append(sectionHead("Storage", "Capacity, growth, and retention evidence"));
  const top = node("div", "panel panel-body kv-grid"); append(top, kv("Database", naValue(storage.hot_sqlite_bytes, bytes)), kv("WAL", naValue(storage.wal_bytes, bytes)), kv("Cold archive", naValue(storage.cold_archive_bytes, bytes)), kv("Disk free", naValue(storage.disk_free_bytes, bytes)), kv("SQLite reusable", naValue(storage.sqlite_reusable_bytes, bytes)), kv("Net growth / day", naValue(storage.net_disk_growth_bytes_per_day, (v) => `${bytes(v)}/day`))); root.append(top);

  root.append(sectionHead("Storage efficiency", "Compression, SQLite reuse, and physical reclamation are distinct."));
  const efficiency = node("div", "panel panel-body kv-grid"); append(efficiency, kv("Compression savings", naValue(storage.compression_saved_bytes, bytes)), kv("Compression saving %", naValue(storage.compression_saving_percent, percent)), kv("SQLite reusable", naValue(storage.sqlite_reusable_bytes, bytes)), kv("Physical disk reclaimed", naValue(storage.physical_reclaimed_bytes, bytes)), kv("Disk free", naValue(storage.disk_free_bytes, bytes)), kv("Hot SQLite", naValue(storage.hot_sqlite_bytes, bytes))); root.append(efficiency);

  root.append(sectionHead("Purge", "Authoritative counters only."));
  const purge = node("div", "panel panel-body kv-grid"); append(purge, kv("Purge eligible chunks", naValue(storage.purge_eligible_chunks, number)), kv("Purged chunks", naValue(storage.purged_chunks, number)), kv("Total purged events", naValue(storage.total_purged_events, number)), kv("Last deleted events", naValue(storage.last_purge_deleted_events, number)), kv("Last purge duration", naValue(storage.last_purge_duration_seconds, (v) => `${number(v, 2)} s`)), kv("Last reusable bytes", naValue(storage.last_purge_reusable_bytes, bytes))); root.append(purge);

  root.append(sectionHead("Compaction gate", "Project threshold: 8 GiB and 25%; no action is exposed."));
  const compact = node("div", "panel panel-body kv-grid"); append(compact, kv("Reclaimable", naValue(storage.compaction_reclaimable_bytes, bytes)), kv("Reclaimable %", naValue(storage.compaction_reclaimable_percent, percent)), kv("Minimum required", naValue(storage.compaction_minimum_required_bytes, bytes)), kv("Required %", naValue(storage.compaction_minimum_required_percent, percent)), kv("Status", stateLabel(storage.compaction_status))); root.append(compact);

  root.append(sectionHead("Growth / trend", "No interpolation; missing observations remain N/A."));
  const growth = node("div", "table-wrap"); const growthTable = node("table"); const growthHead = node("tr"); ["Metric", "Per hour", "Per day"].forEach((item) => growthHead.append(node("th", item))); const growthBody = node("tbody"); [["Raw WS growth", storage.raw_ws_growth_bytes_per_hour, storage.raw_ws_growth_bytes_per_day], ["Cold archive growth", storage.cold_archive_growth_bytes_per_hour, storage.cold_archive_growth_bytes_per_day], ["Net disk growth", storage.net_disk_growth_bytes_per_hour, storage.net_disk_growth_bytes_per_day]].forEach(([label, hourly, daily]) => { const row = node("tr"); append(row, node("td", "", label), node("td", "num", naValue(hourly, (v) => `${bytes(v)}/h`)), node("td", "num", naValue(daily, (v) => `${bytes(v)}/day`))); growthBody.append(row); }); growthTable.append(append(node("thead"), growthHead), growthBody); growth.append(growthTable); root.append(growth);

  const note = node("div", "panel panel-body"); append(note, kv("Retention", naValue(storage.retention_seconds, duration)), kv("Purge mode", "DRY RUN", "available")); note.append(node("p", "muted", "Storage numbers are observations from the recorder heartbeat. No deletion or compaction control is available here.")); root.append(note); return root;
}

function renderOperations() {
  const operations = state.operations;
  if (!operations) return emptyState("Operations state unavailable", "Waiting for the operations API.");
  const root = node("div"); root.append(sectionHead("Operations", "Recorder lifecycle, bounded controls, and recent events"));
  const metrics = node("div", "metric-grid");
  append(metrics, metric("Recorder", stateLabel(operations.recorder_state)), metric("Heartbeat", stateLabel(operations.recorder_heartbeat)), metric("Active markets", valueOrDash(operations.active_markets, number)), metric("Pending settlements", valueOrDash(operations.pending_settlements, number)), metric("Retries", valueOrDash(operations.retries, number)));
  root.append(metrics);
  const control = node("div", "panel panel-body"); control.append(node("p", "muted", "Recorder controls remain the only mutating operations and are localhost-bound."));
  const controls = node("div", "control-actions"); const st = operations.recorder_state;
  append(controls, controlButton("Start Collection", "start", ["stopped", "error"].includes(st)), controlButton("Pause Collection", "pause", ["running", "stale"].includes(st)), controlButton("Resume Collection", "resume", ["paused", "stopped", "error"].includes(st))); control.append(controls); root.append(control);
  root.append(sectionHead("Recent bounded events", "Newest 20")); const events = node("div", "panel panel-body warning-list");
  if (!operations.recent_events?.length) events.append(emptyState("No recent events", "The recorder event stream is empty."));
  (operations.recent_events || []).forEach((item) => events.append(append(node("div", "warning-item"), badge(item.severity), node("span", "", `${item.event_type} · ${valueOrDash(item.message)}`)))); root.append(events); return root;
}

function render() {
  const route = currentRoute();
  setHeading(route);
  updateSidebar();
  const renderers = { dashboard: renderDashboard, overview: () => renderAccountPage("overview"), portfolio: () => renderAccountPage("portfolio"), account: () => renderAccountPage("overview"), orders: () => renderReadOnlyShell("Orders"), history: () => renderReadOnlyShell("History"), watchlist: () => renderReadOnlyShell("Watchlist"), analytics: () => renderReadOnlyShell("Analytics"), signals: () => renderReadOnlyShell("Signals"), models: () => renderReadOnlyShell("Models"), markets: renderMarkets, detail: () => renderDetail(route), data: renderData, training: renderTrainingV2, archive: renderArchive, storage: renderStorage, operations: renderOperations, events: renderEvents, system: renderSystem };
  let contents;
  try { contents = renderers[route.name](); }
  catch (error) { console.error("LIVE15 view render failed", error); contents = emptyState("Unable to render this view", "Retrying is safe; local data remains read-only."); }
  view.replaceChildren(contents || emptyState("Unable to render this view", "No renderer is registered for this route."));
  view.setAttribute("aria-busy", "false");
  updateCountdowns();
}

async function refresh(force = false) {
  if (document.hidden) return;
  const route = currentRoute();
  const tasks = [fetchJson("health", "/api/health", INTERVALS.health, force)];
  if (["overview", "portfolio", "account", "orders", "history", "dashboard", "detail"].includes(route.name)) tasks.push(fetchJson("account", "/api/account?profile=production_primary", INTERVALS.account, force));
  if (["dashboard", "events", "system", "operations"].includes(route.name)) tasks.push(fetchJson("events", eventsUrl(), INTERVALS.events, force));
  if (["dashboard", "training", "data"].includes(route.name)) tasks.push(fetchJson("training", "/api/training", INTERVALS.training, force));
  if (["dashboard", "data"].includes(route.name)) tasks.push(fetchJson("data", "/api/data", INTERVALS.data, force));
  if (["dashboard", "archive"].includes(route.name)) tasks.push(fetchJson("archive", "/api/archive", INTERVALS.archive, force));
  if (["dashboard", "storage"].includes(route.name)) tasks.push(fetchJson("storage", "/api/storage", INTERVALS.storage, force));
  if (["dashboard", "operations"].includes(route.name)) tasks.push(fetchJson("operations", "/api/operations", INTERVALS.operations, force));
  if (["dashboard", "markets", "system"].includes(route.name)) tasks.push(fetchJson("markets", "/api/markets", INTERVALS.markets, force));
  if (route.name === "system") tasks.push(fetchJson("system", "/api/system", INTERVALS.system, force));
  if (route.name === "detail") {
    if (state.detailAsset !== route.asset) { state.detail = null; state.detailAsset = route.asset; }
    const detailKey = `detail:${route.asset}`;
    tasks.push(fetchJson(detailKey, `/api/markets/${encodeURIComponent(route.asset)}`, INTERVALS.detail, force).then((payload) => {
      if (state.detailAsset === route.asset) state.detail = payload;
      return payload;
    }));
  }
  const results = await Promise.allSettled(tasks);
  const failures = results.filter((result) => result.status === "rejected");
  showNotice(failures.length ? `Some local data could not refresh (${failures.length} request${failures.length === 1 ? "" : "s"}). Last valid values are retained.` : "");
  render();
}

window.addEventListener("hashchange", () => { window.scrollTo({ top: 0, left: 0, behavior: "auto" }); refresh(true); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(true); });
setInterval(updateCountdowns, 1000);
setInterval(() => refresh(false), 500);
refresh(true);
