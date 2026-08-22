"use strict";

const ASSET_LABELS = Object.freeze({
  BTC: ["BTC", "Bitcoin"], ETH: ["ETH", "Ethereum"], Gold: ["GOLD", "Gold"],
  Silver: ["SILVER", "Silver"], XRP: ["XRP", "XRP"], "WTI Oil": ["WTI", "WTI Oil"],
  SOL: ["SOL", "Solana"], HYPE: ["HYPE", "Hyperliquid"], DOGE: ["DOGE", "Dogecoin"],
  BNB: ["BNB", "BNB"],
});

// Recorder collection cadence is configured server-side and is independent of these
// read-only API refresh intervals. The one-second countdown below performs no request.
const INTERVALS = Object.freeze({ health: 2500, markets: 2500, detail: 2500, events: 15000, system: 30000, coverage: 60000 });
const state = { health: null, markets: null, detail: null, detailAsset: null, coverage: null, events: null, system: null, controlBusy: false, eventFilters: { severity: "", asset: "", source: "", hours: "24" } };
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
  const parts = (location.hash.replace(/^#\/?/, "") || "dashboard").split("/").filter(Boolean);
  if (parts[0] === "markets" && parts[1]) return { name: "detail", asset: decodeURIComponent(parts.slice(1).join("/")) };
  if (["markets", "training", "events", "system"].includes(parts[0])) return { name: parts[0] };
  return { name: "dashboard" };
}

function setHeading(route) {
  const headings = {
    dashboard: ["OVERVIEW", "Dashboard"], markets: ["MARKET DATA", "15-Minute Markets"],
    training: ["DATASET", "Training Data"], events: ["OPERATIONS", "Warnings / Errors"], system: ["OPERATIONS", "System / Health"],
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

  const coverage = state.coverage || {};
  const eventCounts = (state.events || []).reduce((counts, item) => { counts[item.severity] = (counts[item.severity] || 0) + 1; return counts; }, {});
  const summary = node("div", "metric-grid");
  append(summary, metric("Finalized events", number(coverage.finalized_events)), metric("Trainable events", number(coverage.trainable_events)), metric("Warnings", number(eventCounts.warning || 0)), metric("Errors / fatal", number((eventCounts.error || 0) + (eventCounts.fatal || 0))));
  root.append(sectionHead("Recorder summary", "Operational events are bounded"), summary);

  root.append(sectionHead("Live 15-minute markets", `${markets.length}/10 assets · refresh 2.5s`));
  const grid = node("div", "market-grid");
  markets.forEach((market) => grid.append(marketCard(market)));
  root.append(markets.length ? grid : emptyState("Markets unavailable", "No market projections are available."));

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
  sources.append(sourceBody); append(detail, heartbeat, sources); root.append(detail, sectionHead("Per-asset freshness", "Quote and predictive underlying are independent roles"));
  const wrap = node("div", "table-wrap");
  const table = node("table");
  const head = node("tr");
  ["Asset", "Lifecycle", "Quote", "Quote age", "Underlying", "Underlying age", "Settlement follow-up"].forEach((item) => head.append(node("th", item.includes("age") ? "num" : "", item)));
  const body = node("tbody");
  markets.forEach((market) => append(body, append(node("tr"), node("td", "", ASSET_LABELS[market.asset]?.[0] || market.asset), append(node("td"), badge(market.lifecycle)), append(node("td"), badge(market.quote_status)), node("td", "num", age(market.quote_age_seconds)), append(node("td"), badge(market.underlying_status, stateLabel(market.underlying_status))), node("td", "num", age(market.underlying_age_seconds)), append(node("td"), badge(market.settlement_followup)))));
  table.append(append(node("thead"), head), body); wrap.append(table); root.append(wrap);
  return root;
}

function render() {
  const route = currentRoute();
  setHeading(route);
  updateSidebar();
  const contents = { dashboard: renderDashboard, markets: renderMarkets, detail: () => renderDetail(route), training: renderTraining, events: renderEvents, system: renderSystem }[route.name]();
  view.replaceChildren(contents);
  view.setAttribute("aria-busy", "false");
  updateCountdowns();
}

async function refresh(force = false) {
  if (document.hidden) return;
  const route = currentRoute();
  const tasks = [fetchJson("health", "/api/health", INTERVALS.health, force)];
  if (["dashboard", "events", "system"].includes(route.name)) tasks.push(fetchJson("events", eventsUrl(), INTERVALS.events, force));
  if (["dashboard", "training"].includes(route.name)) tasks.push(fetchJson("coverage", "/api/coverage", INTERVALS.coverage, force));
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

window.addEventListener("hashchange", () => refresh(true));
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(true); });
setInterval(updateCountdowns, 1000);
setInterval(() => refresh(false), 500);
refresh(true);
