# Legacy runtime receipt boundary

Revision: R1
Status: historical bounded evidence; not current deployment proof.

## What it is

Separates the last bounded read-only WinSW receipt from a claim about current protected-main deployment.

## Current truth

The receipt showed all three WinSW services running; Recorder was 10/10 synchronized with zero
sequence gaps and no fatal task, with an honest exact-WTI/Pyth feed-local degradation. It does not
prove newly merged protected-main code is deployed. `MERGED != DEPLOYED`; `DEPLOYED != VERIFIED`.
PR #46 and PR #49 are merged code only; no current-main deployment claim follows.

## Interfaces / dependencies

`PROJECT_PROGRESS.md`; the selected bounded service/health evidence; `CURRENT_STATE.md`.

## Read next

For active task state read `PROJECT_PROGRESS.md`; for current workstream orientation read `CURRENT_STATE.md`.

## Update rule

Update only when a new bounded receipt changes this durable deployment-evidence boundary.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-V2-RECURSIVE-HIERARCHY-001 | Moved retained receipt distinction from Current State. |
