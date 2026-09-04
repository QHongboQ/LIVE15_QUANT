# Current roadmap

Revision: R11
Status: approved execution strategy.

## What it is

The sole current execution-sequence authority.

## Current truth

`GAP002` is **CLOSED / PASS**. Its Production FAIL/PASS receipts remain immutable evidence; the old
GAP002 phase structure is historical and does not direct future work.

Normal feature, archive, and WTI progression is frozen. The sole current mainline is:

```text
LIVE15 STABILIZATION / RECOVERY FREEZE
  -> RECORDER-PYTH-CRITICALITY-RECOVERY = FIRST BLOCKER
       decide the correct failure boundary for a complete Pyth outage without
       faking freshness, silently substituting sources, or corrupting Kalshi truth
  -> CONTROL-CENTER-OWNERSHIP-RECOVERY = BLOCKER / AFTER RECORDER RECOVERY
       reconcile one lifecycle owner, desktop entry, listener, release identity, and Web surface
  -> HOST-PRODUCTION-ACCEPTANCE-GATE
       prove actual Windows/Nomad/Web/runtime/deployed identity before normal progression
  -> bounded stability observation
  -> WTI-FULL-STACK-RETIREMENT (10 -> 9)
  -> PARQUET-PRODUCTION-ACCEPTANCE-PHASE1-002
  -> normal data/model progression under existing gates
```

The completed [full-system audit receipt](../../evidence/LIVE15_FULL_SYSTEM_ROOT_CAUSE_AUDIT_001.md) established that a global Pyth transport failure currently escalates
through bounded recovery to a whole-Recorder exit and Nomad restart, while Kalshi can re-synchronize.
The same loop occurs on the reverted legacy release/runtime; immutable Runtime preparation is not its
root cause. The audit also established that ControlCenter's declared Nomad ownership is not reconciled
with current host truth: the main job is not proven running and the desktop Web entry is broken.

Historical implementation and cutover receipts remain evidence. They do not establish current host
operational verification. Parquet + ZSTD selection, the merged archive path, named multi-root layout,
and the prepared immutable runtime remain preserved, but archive activation, purge, WTI retirement,
and further runtime rollout are on HOLD during this freeze. Vector remains deferred; training and
model-promotion gates remain unchanged and blocked.

**NEXT:** **RECORDER-PYTH-CRITICALITY-RECOVERY**. Its purpose is to decide and implement the correct
failure boundary for a complete Pyth outage; it is not a task to make Pyth requests succeed. It must
preserve honest degraded health, no fake freshness, no silent substitution, and Kalshi truth.

During this recovery freeze, if a remediation mechanism requires a second immediate corrective PR
before Production acceptance, stop at that task's stable entry boundary and re-evaluate the design.
Do not continue a fix-on-fix chain.

## Interfaces / dependencies

`dependencies/platform/runtime-ownership.md`;
`constraints/execution/runtime-upstream-boundary.md`;
`docs/roadmap/COMMERCIAL_ARCHIVE_UPSTREAM_ASSEMBLY_001.md`;
`docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md`;
`docs/roadmap/UPSTREAM_REPLACEMENT_EXECUTION_001.md`.

## Read next

Use the selected capability/constraint authority before executing a gate. For the immediate
Recorder/Pyth recovery task, read the Recorder truth authority before any recovery action; consult
`dependencies/platform/runtime-ownership.md` only when a later host Runtime/Recorder gate is approved.

## Update rule

Update only when an approved phase, freeze, or gate changes.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | Recorded approved dual-lane GAP002 strategy. |
| R2 | GAP002-DEPENDENCY-CLOSURE-AUDIT-001B | Marked Phase 1 complete; Phase 2 migration is complete/no-op; Phase 3 freeze remains pending. |
| R3 | GAP002-FROZEN-BASELINE-001 | Completed Phase 3 and routed next action to Phase 4A; GAP002 not executed. |
| R4 | GAP002-CRITICAL-PATH-UPSTREAM-REPLACEMENT-001 | Held Phase 4A for the selected Recorder lifecycle replacement baseline. |
| R5 | PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 | Closed obsolete GAP002 phase routing and established the sole upstream-consolidation mainline. |
| R6 | PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 closeout | Completed authority consolidation and selected runtime/lifecycle consolidation as the one concrete next responsibility class. |
| R7 | RUNTIME-LIFECYCLE-CONSOLIDATION-CLOSEOUT-001 | Recorded verified Nomad/SCM lifecycle adoption, RuntimeSupervisor retirement, and the Web Application Shell as the next generic replacement class. |
| R8 | PROJECT-BRAIN-POST-WEB-RECONCILIATION-001 | Recorded the complete/verified Web Application Shell replacement and selected Vector telemetry as the next generic replacement responsibility. |
| R9 | STORAGE-ARCHIVE-REPRIORITIZATION-001 | Reprioritized current execution to storage/archive packaging after the capacity problem emerged. |
| R10 | PROJECT-BRAIN-STORAGE-RUNTIME-RECONCILE-001 | Reconciled PRs #157/#158/#160/#167: Parquet+ZSTD is selected/implemented, #159 is historical stop evidence, and the archive mainline now closes Runtime rollout, WTI retirement, then fresh Phase1-002. |
| R11 | PROJECT-RECOVERY-FREEZE-001 | Froze normal progression after the full-system audit; selected Recorder/Pyth criticality recovery as the sole NEXT and held ControlCenter, host acceptance, WTI, archive, and runtime progression in recovery order. |
