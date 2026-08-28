# NIGHT-001 training readiness — `night-001-20260828-6f91d6fc`

Status: `NIGHT001_TRAINING_READINESS_PARTIAL`. This is an offline, development-only result and does not authorize formal training.

## Baseline and scope

- `origin/main`: `6f91d6fc9e8bc86c363e473726b48e18a05ba923`
- branch: `agent/night-001-training-readiness`
- isolated worktree: `D:/LIVE15_QUANT/.worktrees/night-001-training-readiness`
- resume checkpoint: `.local-tools/night-001/checkpoint.json` (ignored and re-readable)
- the unrelated `tools/bootstrap_winsw.ps1` working-tree change was preserved and excluded.

## Current training data truth

| Tier | Current isolated-worktree result | Training status |
| --- | --- | --- |
| H0 | `current_trainable` unavailable; verified archive not configured | `BLOCKED` for this run; no H0 rows were opened or changed |
| H1 | verified historical source metadata is present (59,056 markets / 886,454 trades), but no local detailed capability store is available | `PARTIAL`; no current canonical training input could be built |
| H2 | no accepted historical snapshot or delta rows; no isolated-environment credential | `BLOCKED`; retained bounded provider facts are snapshot HTTP 429 and tick HTTP 402 |

The runtime `ResearchUniverseSnapshot` reported `holdout_accessed=false`. No frozen payload, row, label, feature, prediction, or metric was read.

## H2-TRAIN-001 — DepthFeed L2 training materialization

Status: `PARTIAL`.

The new typed offline boundary converts a `HistoricalL2Snapshot` plus explicit authority metadata into a deterministic microstructure example. It retains provider/tier, ticker, event/window, source/receive/decision timestamps, sequence identity, raw YES/NO levels, gap/quality/availability states, artifact hash, and cutoff. It computes only bounded book-derived features and accepts no settlement/result field.

Snapshot sequences are event-local, end at decision time, and reject future, gapped, unavailable, mismatched-window, cross-event, and holdout-identity-excluded rows. They never fill or interpolate. H0/H2 overlap is explicit: matches validate, no comparable data is partial, and conflicts fail closed rather than selecting H2.

`CODE_PIPELINE_READY` is proven only with structural fixtures. `REAL_H2_DATA_READY` remains false until real provider rows pass equivalent H0 overlap validation. Delta/tick capability remains `H2_DELTA_SEQUENCE_UNAVAILABLE`; snapshots are never represented as deltas. See `docs/evidence/h2_train_001_pipeline.json`.

## Legacy Dataset isolation

`PASS`. The current runner rejects Dataset and Dataset-path inputs and accepts typed `ResearchUniverseSnapshot` plus `CanonicalEvidenceSnapshot` only. Dataset v1/v2 are not an input to the H2 materializer, sequence builder, or capability preflight.

## Model-family preflight matrix

| Family | Result in this run | Exact blocker |
| --- | --- | --- |
| Terminal structured baseline | `BLOCKED` | no current canonical terminal evidence |
| Structured XGBoost Path Expert | `BLOCKED` | no current canonical path-capability evidence |
| causal TCN Path Expert | `BLOCKED` | no current causal sequence-capability input |
| MLPLOB | `BLOCKED` | `MATERIALIZED_SNAPSHOT_EVIDENCE_REQUIRED` |
| DeepLOB | `BLOCKED` | `SNAPSHOT_SEQUENCE_EVIDENCE_REQUIRED` |
| TLOB | `BLOCKED` | `H2_DELTA_SEQUENCE_UNAVAILABLE` |
| Regime baseline | `BLOCKED` | no current canonical evidence input |
| Factor research | `BLOCKED` | no current canonical evidence input; legacy Dataset paths are reproduction-only |
| future AUTO-RD loop | `BLOCKED` | same canonical preflight plus a separately approved orchestration contract |

H0/H1/H2 snapshot, delta, snapshot-sequence, delta-sequence, and microstructure-training-ready days are now separate canonical fields; no generic H2-ready boolean exists.

## Leakage, harness, and adaptive training

The executable guards passed for source/receive as-of timestamps, no future joins/fills, gap rejection, event isolation, 600-second purge/embargo, train-only normalization, settlement-feature rejection, holdout exclusion, Dataset-path rejection, immutable input identity, deterministic run identity, atomic manifests/checkpoints, bounded output/memory, and fail-closed resume.

Adaptive recorder retention is implemented but is not adaptive model training. Current-training recency/regime/data-quality weighting, concept/calibration/performance drift triggers, and a Stable Champion / Adaptive Challenger loop over the Research Data Authority remain `MISSING`. No policy or runtime wiring was added.

## Resource capability

Host evidence: Intel i7-10870H (8 cores / 16 logical processors), 16,935,030,784 bytes RAM, RTX 3060 Laptop GPU (4,293,918,720 reported adapter bytes), and about 122.6 GiB / 143.0 GiB free on C: / D:. Bounded offline smoke work fits the envelope; formal deep-model long runs remain unapproved and data-blocked.

## Validation and next task

- 58 targeted H2/canonical/provider/authority/runner/sequence tests passed.
- 40 leakage, factor, historical-research, and long-run harness tests passed.
- Full suite: 1068 passed and 14 live-smoke tests skipped in 72.21 seconds. The default Windows pytest temp directory is permission-blocked; a writable repository-external `basetemp` preserves the credential-location test contract.
- `ruff check` and `git diff --check` passed.

Highest-priority blocker: obtain one bounded, recorded DepthFeed snapshot range with its exact market mapping and source timestamp semantics, then materialize an equivalent H0 overlap range. Run the new overlap checker; only a validated result may populate real H2 capability days. Keep delta/tick families blocked until provider capability changes.

Recommended next task: `H2-TRAIN-002 — bounded real-snapshot acquisition and H0 overlap validation`.

`NO FORMAL LONG TRAINING`
`NO HOLDOUT ACCESS`
`NOT PROMOTED`
`NO PRODUCTION DEPLOYMENT`
`NO PRODUCTION RESTART`
`PRODUCTION WRITES 0`
