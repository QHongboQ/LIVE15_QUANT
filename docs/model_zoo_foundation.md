# FLOW-005A — Model Zoo Foundation

Status: **DEVELOPMENT RESEARCH FOUNDATION ONLY**. This document defines contracts and readiness;
it does not train models, promote candidates, or wire any model into Paper, Shadow, Production,
Recorder, execution, or Hard Risk.

## Approved upstream roles

| Upstream | LIVE15 role | Status | License/dependency boundary |
|---|---|---|---|
| Time-Series-Library | Path Expert candidates: TimeXer, PatchTST, iTransformer, TimeMixer, TimesNet, DLinear | foundation approved; full training gated | provenance only; exact revision and license review required before vendoring |
| TLOB | Microstructure Expert; DeepLOB/MLPLOB bounded baselines | partial | provenance only; no external repository copied |
| Qlib | rolling-retrain, research orchestration, and RL reference | research reference only | not a current runtime dependency |
| EarnHFT | hierarchical trading architecture reference | architecture reference only | not a current runtime dependency |

Upstream provenance is the FLOW-005A approved decision plus the pinned LIVE15 contracts. Exact
upstream commit revisions must be recorded before implementation; this milestone deliberately
does not vendor code or add dependencies. License review is a gate for any future import.

## Layered LIVE15 architecture

```text
Path Expert ───────────────┐
Terminal / decision expert ├─> future Regime / Router layer ─> future execution / risk layer
Microstructure Expert ─────┘
```

The Path Expert consumes source-aware historical path representations. The Terminal expert is a
small offline baseline. The Microstructure Expert consumes only validated H0 Recorder orderbook
or H2 DepthFeed evidence. Router and execution/risk layers are future interfaces, not active
runtime components.

## Readiness gates

- Path foundation requires an approved historical representation, at least 30 independent UTC
  days and 1,000 independent events, and remains development-only.
- Full sequence training additionally requires complete sequence representation; HIST-003 detail
  coverage is bounded, so this is `PARTIAL`.
- Microstructure requires H0/H2 evidence. Current H2 snapshot capability is available, while H2
  ticks are unavailable (provider HTTP 402), so readiness is `PARTIAL`.
- Dataset v2 holdout remains `UNREVEALED_FROZEN`; it is never an approved source and was not read.
- No research artifact silently promotes to runtime.

## Current readiness decisions

| Layer/family | Decision | Evidence / blocker |
|---|---|---|
| Path Expert foundation | `READY` / `APPROVED_FOR_FOUNDATION` | HIST-003: 90 UTC days, 59,056 events; detail cap still applies |
| Path full training | `PARTIAL` | complete sequence representation and bounded-detail review remain |
| Terminal baseline | `READY` offline | MVN-001/MVN-002 structured contract; no promotion |
| Microstructure Expert | `PARTIAL` | H0 Recorder + H2 snapshot; H2 ticks unavailable |
| Commodity microstructure | `BLOCKED` | no verified historical L2 for Gold/Silver/WTI |
| Qlib | `RESEARCH_ONLY` | no runtime dependency |
| EarnHFT | `ARCHITECTURE_ONLY` | router/execution not implemented |

## Allowed data by family

Path models may use official H1 Kalshi history and approved featureized derivatives with strict
as-of timestamps. Microstructure models may use H0 Recorder orderbook and H2 snapshots/ticks
only where present; snapshots are never represented as deltas. Commodity microstructure stays
blocked without verified L2. No family may use settlement-derived predictive fields, future-nearest
joins, interpolation, forward fill, implicit zero substitution, or the unrevealed holdout.

The machine-readable source of truth is `docs/model_zoo_foundation.json`; executable contracts are
in `live15_quant.model_zoo`, and readiness evaluation is in `live15_quant.model_readiness`.
