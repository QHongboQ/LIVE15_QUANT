# BASELINE-001B — Data Foundation Closeout

Status: **AUDIT COMPLETE — DEVELOPMENT/RESEARCH BASELINE**

Protected main at audit: `da83c473cf66f84b4d3621d9ef55f7164c4ba9dc` (PR #14 squash merge).
No model training, Dataset v2 mutation, holdout access, Recorder mutation, Paper activation,
or Production writes were performed.

## Evidence layers

| Layer | Evidence | Gate |
|---|---|---|
| H0 Recorder | SDK-authoritative `data/live15.sqlite3`; 10 markets, 5 archive/checkpoint UTC days; gaps retained and typed | HEALTHY / read-only audit |
| Current trainable | 4,350 events, 26,402 rows, 3,582 eligible events, 6 UTC days, 10 assets | Mutable materializer output; not a frozen dataset |
| H1 historical | `historical-research-f2d529adfb95080971becdaf`, 90 days, 59,056 markets, 886,454 trades, 5,242 candles | Official Kalshi research layer; 8-fold plan only |
| H2 microstructure | DepthFeed snapshot/delta evidence not materialized | `INSUFFICIENT_MICROSTRUCTURE_EVIDENCE` |
| Frozen dataset | `live15-dataset-v2-4bb4934bf328b6b024ff`; holdout `UNREVEALED_FROZEN` | Untouched and unaccessed |

## Integrity and readiness

- Canonical evidence gate: **PASS**.
- As-of/anti-leakage policy remains authoritative; no future fill, interpolation, or fabricated
  target was introduced. Five/15-second and window-end targets remain unavailable where the
  observation contract cannot be satisfied.
- Sequence gate remains **INSUFFICIENT_SEQUENCE_EVIDENCE**; microstructure gate remains
  **INSUFFICIENT_MICROSTRUCTURE_EVIDENCE**.
- Structured path research is eligible on official H1 evidence, but a Path Model Tournament is
  **not approved** until this baseline is tagged and the remaining evidence gates are reviewed.

## Governance and residue

PR #14 (`32935451418`) passed CI and was squash-merged. PRs #12 and #1 remain open historical
work and require human review; they were not closed or deleted. Existing dirty files on local
`main`, active worktrees, runtime PID files, and the `.agents` dry-run example were preserved.
Only deterministic pytest cache/temp residue was targeted for cleanup; filesystem ACLs denied
removal, so no unverified deletion was attempted.

The annotated tag `data-foundation-v1` is intentionally deferred until this manifest PR is
reviewed and merged under protected-main governance.

## Reproducibility

Machine-readable details, observations, source identities, and validation status are in
[`data-foundation-001b.json`](data-foundation-001b.json). Raw SQLite databases and credentials
remain outside Git under ignored paths.
