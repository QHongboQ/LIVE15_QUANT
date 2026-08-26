# HIST-001 — Historical Research and Walk-Forward Foundation

Status: **DEVELOPMENT RESEARCH SUBSTRATE ONLY**.

HIST-001 separates reusable historical evidence from genuinely new live evidence. The
authoritative Recorder remains the live data path; historical data never replaces it and is not
treated as fresh out-of-sample validation. The canonical capability snapshot is
`hist001_historical_source_capability.json`.

## Current audit

The local evidence available for a bounded proof is H0 live-native data already retained by
LIVE15: Coinbase ticks, Pyth observations, secondary underlying observations, Kalshi quote and
lifecycle records, finalized settlements, current-trainable rows, and SDK-authoritative WS
orderbook events. The WS archive is independently manifested and its coverage boundary is
visible. The current integration can request historical Kalshi market metadata through
`/historical/markets`, but no historical trades/candles/full orderbook source was acquired or
proven, so those capabilities remain unavailable rather than inferred.

The proof uses a read-only row-ID-bounded snapshot of `data/current_trainable.sqlite3` as a
separate `HistoricalResearchDataset` lineage. It is not Dataset v3 and it does not read or alter
Dataset v2 (`live15-dataset-v2-4bb4934bf328b6b024ff`); its holdout remains
`UNREVEALED_FROZEN` and unaccessed.

## Contract

Each historical sample retains source ID, H0/H1/H2 provenance tier, event/window identity,
decision timestamp, source timestamp, optional receive timestamp, optional future target
timestamp, feature names, and typed exclusion reason. Available rows require
`source_timestamp <= decision_timestamp`; receive timestamps are checked when present; future
targets must be strictly after the decision and within the event window. Settlement/label fields
are rejected from feature names. Missing information is excluded with a reason; no forward-fill,
backward-fill, interpolation, or implicit zero fill is allowed.

Walk-forward folds are expanding or rolling UTC-day windows over whole event groups. The proof
uses 3 training days, 1 validation day, a 1-day step, and the existing 600-second purge/embargo.
Transforms for future model work must be fit on train only. Random row splits are not part of the
API.

## Research boundary

This milestone does not train a model, run AUTO-ML, modify Dataset v2, unlock microstructure or
sequence models, wire Paper/Production, or execute storage optimization. Historical source
acquisition is explicitly `HISTORICAL_SOURCE_ACQUISITION_PENDING`; a future task may add a
verified H1 source or limited H2 contract data after a separate capability and licensing review.

The durable product requirement remains background operation: Recorder, materialization, evidence
checking, freeze readiness, bounded training/evaluation, and Champion/Challenger reporting should
eventually run without routine manual Codex intervention, while human approval remains required
for promotion, Production authority, Hard Risk, and protected architecture changes.

The full proof manifest is a local ignored artifact at
`data/research/hist001_proof.json`; raw history is never committed to Git.

## HIST-002R — verified provider hierarchy

HIST-002R adds a read-only provider boundary without changing the Recorder or Dataset v2:

1. `kalshi_official` / `H1_KALSHI_OFFICIAL_HISTORY` is the authoritative source for official
   historical markets, metadata, public trades, and completed candlesticks. The installed
   `kalshi-sdk==12.0.0` historical resource is used directly for cutoff, markets, trades, and
   candles.
2. `depthfeed_kalshi_l2` / `H2_DEPTHFEED_RECORDED_L2` is optional third-party evidence for
   historical full-depth snapshots and separately sourced ticks. Snapshot rows are classified
   `HISTORICAL_L2_SNAPSHOT`; tick rows are classified `HISTORICAL_L2_DELTA`. Snapshots are never
   converted into deltas or synthetic ladders.
3. `live15_recorder_h0` remains the authoritative source for future LIVE15-owned realtime
   microstructure. The new adapters do not import or write Recorder state.

DepthFeed is independently failing and requires a project secret reference named
`.secrets/depthfeed-api-key.txt` plus an explicitly configured `DEPTHFEED_BASE_URL`. Without the
key, the adapter returns `DEPTHFEED_NOT_CONFIGURED`; no credential is logged or committed. The
bounded probe is intentionally key-gated and does not perform bulk acquisition. The official
Kalshi probe uses one representative page and short cutoff/trade/candle requests only.

All observations retain provider provenance and endpoint family. Candles are eligible only after
their period end; L2 selection uses the latest observation with provider receive time at or before
the decision. There is no future-nearest join, forward fill, interpolation, or fake orderbook
reconstruction. Acquisition manifests hash provider, endpoint, bounds, tickers, archive floor,
rows, and code SHA while excluding acquisition time from scientific content identity.

Coverage is source-aware: crypto BTC/ETH/SOL/XRP/DOGE/BNB/HYPE may use official history plus H2
L2 where available; Gold/Silver/WTI use official markets/trades/candles only, with historical L2
`UNAVAILABLE_FROM_VERIFIED_PROVIDER`. This remains historical research infrastructure, not fresh
current-regime validation or model eligibility promotion. HIST-003 is the future bounded bulk
acquisition task; no bulk archive was downloaded here.
