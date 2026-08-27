# Research Data Authority

`ResearchUniverseSnapshot` is LIVE15's canonical, metadata-first account of
authorized research coverage. It is built from the source registry, not inferred from a
Dataset partition.

## Permanent distinctions

| Concept | Time scale | Contract |
|---|---:|---|
| Feature Freshness | seconds/minutes | At a decision, source and receive timestamps must both be at or before the decision and within the configured maximum age. Missing/stale/gapped input remains unavailable. |
| Training Recency | sessions/weeks | A validation policy may select an expanding, rolling-session, or age-weighted development window. Historical examples are not discarded merely because they are not fresh forward evidence. |
| Forward OOS Freshness | after a frozen specification | Evidence is forward OOS only when it arrived strictly after that factor/model specification was frozen. It is required for promotion/generalization evidence, not confused with development history. |

**A 15-minute forecast horizon is not a two-day training-history horizon.**
Historical development evidence is not fresh forward OOS evidence.

## Source registry and precedence

| Tier | Source | Allowed semantics |
|---|---|---|
| H0 | LIVE15 Recorder and verified cold archive | Native provenance; archive only when replay verified/purge eligible/purged. Quarantined archive ranges are excluded. |
| H1 | Official Kalshi historical data | Completed official market/trade/candlestick evidence only; it is not claimed as full historical L2. |
| H2 | DepthFeed historical L2 | Server-side credentialed snapshots/ticks only after identity and bounded overlap validation prove the relevant semantics. |

Equivalent observations use deterministic H0 → H1 → H2 precedence. Exact duplicates are
deduplicated. Conflicting equivalent observations are quarantined; the authority does not
arbitrarily select a source. An observation must match its registered source type/tier and its
source must be explicitly verified before it can enter the universe. Non-overlapping
source-specific evidence can coexist.

## Dataset policy

Dataset v1 and v2 are immutable reproduction artifacts, not source-registry truth. Their
metadata may establish experiment lineage and frozen holdout exclusions. The authority never
opens Dataset v2 holdout rows, labels, features, predictions, metrics, or performance. External
data that shares a frozen holdout event/time identity is excluded from development research.

Legacy Dataset-oriented Factor/Model commands require `--reproduction-only`. They cannot be
silently represented as current research selection. A future current-research runner must accept
a `ResearchUniverseSnapshot` and chronological whole-event walk-forward plan with the existing
600-second purge/embargo boundary.

## Session semantics

The authority publishes `utc_calendar_days` and `market_session_days` separately. For a
continuous instrument their present values can coincide, but their types and report labels do
not. `validation_days` is always presented separately from `eligible_development_days`; a short
validation partition cannot be relabeled as total coverage.

## Runtime/UI contract

`GET /api/research-data` is aggregate-only, localhost Control Center data. The `Research Data`
view reports source coverage, day/session labels, typed freshness policy, frozen-holdout
metadata-only status, and DepthFeed capability status. It has no credential, factor-run, model,
or production-write control.

The runtime authority uses read-only SQLite URI connections and process-local source-high-water
caching. It refreshes when aggregate source identity changes and does not write runtime state.

## DepthFeed credential boundary

The existing `DepthFeedHistoricalOrderbookProvider` remains the sole H2 adapter. It reads a
server-side key from the existing secret boundary and requires `DEPTHFEED_BASE_URL`; neither is
serialized, logged, hashed, or exposed by the API/UI. Before any bounded acquisition, an H0
overlap must validate market identity, timestamp/availability, snapshots, and tick/delta
semantics. A provider response cannot manufacture a replayable native book.

### DepthFeed closeout probe (2026-08-28)

The provider documentation identifies `https://api.depthfeed.com/v3` as the API base. The
LIVE15 adapter owns the `/v3` path suffix, so the server-side configuration value is the
non-secret root `DEPTHFEED_BASE_URL=https://api.depthfeed.com`. A bounded probe authenticated
successfully and discovered one exact Kalshi market. The snapshot request returned HTTP 429,
including one bounded retry; no snapshot payload was accepted and no historical acquisition was
performed. The tick request returned HTTP 402, which is recorded as a provider-plan limitation.
Because no snapshot was returned, H0 overlap semantics (identity, timestamps, ordering,
duplicates, and conflicts) remain unvalidated and H2 remains partial. The UI must continue to
surface this as unavailable/partial rather than implying sequence evidence.
