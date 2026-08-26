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
visible. Before HIST-003, the current integration could request historical Kalshi market metadata
through `/historical/markets`, but no historical trades/candles/full orderbook source had been
acquired or proven; those capabilities were kept unavailable rather than inferred.

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
current-regime validation or model eligibility promotion.

## HIST-003 — bounded 90-day acquisition (development only)

HIST-003 completed the bounded official-history acquisition against the resolved Kalshi cutoff
`2026-06-26T00:00:00Z`, covering `2026-03-28T00:00:00Z` through that cutoff. The complete
in-window market metadata set contains 59,056 markets across 90 independent UTC days, with
886,454 official public trades and 5,242 completed 1-minute candles retained in the ignored raw
SQLite store. Detail trades/candles were intentionally capped at 500 markets; this is partial
detail coverage, not an uncontrolled full-tape download. Two cutoff, 132 market-page, 700 trade,
and 700 candle GET calls were recorded across the initial run and bounded candle retry run; no
conflicts were observed. The store occupied 675,569,664 bytes at materialization.

Gold, Silver, and WTI had no verified in-window market rows in this cutoff snapshot. DepthFeed was
not configured (`H2_PENDING_DEPTHFEED_CREDENTIALS`), so no historical L2 or tick/delta data was
requested. The deterministic `HistoricalResearchDataset` identity is
`historical-research-f2d529adfb95080971becdaf`; its ignored full manifest is
`data/research/hist003/hist003_manifest.json` and the tracked summary is
`hist003_acquisition_summary.json`. A plan-only expanding walk-forward layout has eight folds
(30 train days, 7 validation days, 7-day step, 600-second purge/embargo, whole-event groups).
Structured H1 path/terminal research is eligible; microstructure and event-delta research remain
H2-gated, and sequence readiness remains `INSUFFICIENT_SEQUENCE_EVIDENCE`. No model was trained,
Dataset v2 or its holdout was read, and Recorder/Production/Paper paths were unchanged.

## FLOW-005B1 — causal evidence re-evaluation (development only)

The read-only H1 detail was materialized into causal sequence evidence at 5s, 15s, and 30s
grids: 13,632, 14,597, and 8,943 rows respectively (37,172 total), across 350 markets and
the single detail day `2026-06-25`. Per-asset totals across all grids were BNB 3,072; BTC
11,461; DOGE 3,446; ETH 7,678; HYPE 4,056; SOL 3,753; XRP 3,706. With one independent day
there are zero chronological validation folds, so path readiness remains
`SEQUENCE_PARTIAL_MORE_DATA_OR_REPRESENTATION_NEEDED` and no model training is unlocked.

The bounded seven-day DepthFeed attempt discovered 50 metadata rows; its first snapshot request
returned HTTP 429 and was not retried. The known free-plan tick/delta limitation remains HTTP
402, so snapshot readiness and TLOB readiness remain blocked. H1 trades were not treated as L2,
and no model, Dataset v2, holdout, Recorder, Paper, or Production state was changed.

## EVID-RECON-001 — layered evidence reconciliation

The one-day detail result was audited and its exact cause is
`HIST003_DETAIL_CAP_FIRST_N_PER_ASSET_TEMPORAL_CONCENTRATION`: the old 350-market cap selected
the first records per asset in API/storage order, all of which traded on `2026-06-25`. The bounded
replacement is deterministic and stratified by evenly spaced UTC day, asset, and event. It used
147 already-known H1 markets across `2026-03-28`, `2026-04-12`, `2026-04-27`, `2026-05-11`,
`2026-05-26`, `2026-06-10`, and `2026-06-25`; no full 90-day replay was performed.

The new ignored research store contains 201,424 official trades, 2,187 candles, and zero conflicts.
The causal sequence materialization contains 10,653 rows, 144 independent event identities, and
four expanding folds (3 train days, 1 validation day, 1-day step, 600-second purge/embargo).
This is `SEQUENCE_READY_FOR_BOUNDED_MODEL_TRAINING` as a research readiness result, not a model
promotion or OOS claim. H0 was audited independently: six real live-native days and 4,350 event
identities, with quotes/underlying/L2 retained in their authoritative stores; H0 path rows were not
fabricated or blended into H1. H2 remains blocked after bounded provider timestamp and rate-limit
failures. The compact tracked summary is `docs/evid_recon001_report.json`.

## DATA-READINESS-001 — canonical reconciliation gate

The permanent machine-readable contract is `CanonicalEvidenceSnapshot` in
`src/live15_quant/canonical_evidence.py`. It retains H0 live-native, H1 official history, H2
DepthFeed L2, current trainable-pool, and frozen-dataset records as separate provenance-bearing
objects with explicit scope (`FULL_SOURCE`, `BOUNDED_WINDOW`, `STRATIFIED_SAMPLE`,
`SAMPLED_SUBSET`, `FROZEN_DATASET`, or `EXPERIMENT_CUTOFF`). It exposes separate H0/H1/H2 path,
snapshot, and delta day counts and never lets a sampled/capped artifact overwrite global source
coverage.

The confirmed first-N temporal bug is now guarded in code and regression tests. API-order,
storage-order, and first-N policies are rejected; unexplained source-to-artifact temporal collapse
returns `EVIDENCE_RECONCILIATION_REQUIRED` and blocks training preflight. H0 is the hard future
priority for current-regime validation and promotion reality checks; H1/H2 can accelerate research
only with row-level provenance and source-specific semantics. Dataset v2 and its holdout remain
immutable and unaccessed.
