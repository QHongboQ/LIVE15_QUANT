# DATA-READINESS-001 — Canonical Evidence Reconciliation

Status: `DEVELOPMENT RESEARCH ONLY`. This gate does not train, promote, or wire a model and does
not mutate Dataset v2, its holdout, Recorder, Paper, Production, or Hard Risk.

## Canonical object

All future model, sequence, microstructure, factor, and AUTO-ML readiness decisions must begin
with `live15_quant.canonical_evidence.CanonicalEvidenceSnapshot` built by
`build_canonical_evidence_snapshot(...)`. A readiness evaluator must not infer global coverage from
one local detail artifact. The deterministic identity is `ces-<sha256 prefix>` over the schema,
experiment ID/cutoff, ordered source records, frozen-dataset references, and inconsistency states;
the wall-clock `generated_at` is provenance only and is not part of the identity.

Each `EvidenceRecord` contains:

| Field group | Contract |
|---|---|
| identity | `source_id`, `provenance_tier`, `artifact_id` |
| scope | `coverage_scope`, `full_source`, `capped`, `cap_size`, `sampling_policy` |
| time/counts | earliest/latest, cutoff, independent UTC days/events, per-day/per-asset counts, row count |
| quality | data-quality status, gap/quarantine state, target availability |
| capabilities | sequence/path days, snapshot days, delta days, source-specific availability |
| boundary | experiment cutoff, frozen dataset references, holdout access state |

Coverage scopes are explicit: `FULL_SOURCE`, `BOUNDED_WINDOW`, `STRATIFIED_SAMPLE`,
`SAMPLED_SUBSET`, `FROZEN_DATASET`, and `EXPERIMENT_CUTOFF`. A sampled record can describe only
its artifact; it cannot overwrite the corresponding full-source record. Readiness exposes separate
fields `h0_path_days`, `h1_path_days`, `h2_path_days`, `combined_path_days`, and equivalent
snapshot/delta fields.

## Sampling and inconsistency policy

`first N`, `first-N`, API-order, and storage-order selection is rejected by
`validate_sampling_policy`. Bounded materialization must use chronology-preserving deterministic
stratification such as UTC day × asset × bounded events, with requested and actual coverage
reported. When a sampled artifact collapses many source days without an explicit stratification
policy, the gate returns these structured states:

- `TEMPORAL_COVERAGE_COLLAPSE`
- `ASSET_COVERAGE_COLLAPSE`
- `ARTIFACT_SCOPE_MISMATCH`
- `SOURCE_ARTIFACT_COUNT_MISMATCH`
- `EVIDENCE_RECONCILIATION_REQUIRED`
- `READINESS_EVIDENCE_INCONSISTENT`
- `EXPERIMENT_CUTOFF_VIOLATION`

The confirmed HIST-003 bug (`HIST003_DETAIL_CAP_FIRST_N_PER_ASSET_TEMPORAL_CONCENTRATION`) is
covered by regression tests. An unexplained one-day detail artifact is blocked, not relabeled as
global one-day evidence.

## Source hierarchy and training preflight

H0 (`H0_LIVE_NATIVE`) is the preferred current-regime validation, drift, Challenger, and promotion
reality check. H1 (`H1_KALSHI_OFFICIAL_HISTORY`) is authoritative for historical markets, trades,
candles, and settlement truth; it is not historical full L2. H2
(`H2_DEPTHFEED_RECORDED_L2`) is third-party historical L2 and remains separate snapshot/delta
quality classes. H0/H1/H2 rows may contribute to research only when feature semantics permit, and
each row must retain provenance, provider identity, timestamp semantics, and quality class. They
are never blindly concatenated.

`training_preflight(snapshot, model_family=...)` is the mandatory future gate. It verifies the
canonical snapshot type, experiment cutoff, holdout boundary, inconsistency states, and
model-family capability before returning `READY`, `PARTIAL`, or `BLOCKED`. AUTO-ML must run this
gate before freeze, retraining, factor search, tournaments, or Challenger comparison and must stop
on `EVIDENCE_RECONCILIATION_REQUIRED`.

Current known evidence remains source-aware: H0 has 6 live-native days, H1's reconciled path
sample has 7 path-ready source days while the H1 global source is broader, and H2 has no usable
snapshot/tick rows. These numbers are references, not a new Dataset v3 or a promotion decision.
