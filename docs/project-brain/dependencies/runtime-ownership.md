# Runtime ownership

Revision: R1
Status: machine-readable authority retained.

## What it is

Maps process/service ownership, health truth, and restart authority.

## Current truth

`deploy/windows/runtime-ownership.json` remains the machine-readable authority. ControlCenter is Nomad-managed; Recorder and RuntimeSupervisor retain their separately owned boundaries.

## Interfaces / dependencies

`docs/runtime_ownership_and_self_healing.md`; `deploy/windows/runtime-ownership.json`.

## Read next

Use `capabilities/control-center.md` or `capabilities/recorder.md` for component context.

## Update rule

Update only when ownership topology changes; change the JSON authority first when applicable.

## Change log

| Revision | Task / PR | Change |
| --- | --- | --- |
| R1 | PROJECT-BRAIN-ARCHITECTURE-V2-001 | V2 authority baseline. |
