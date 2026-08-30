# NOMAD-CONTROL-CENTER-CUTOVER-FINAL-001 — runtime-accepted ownership cutover

## Result

`CONTROL_CENTER_NOMAD_CUTOVER = RUNTIME_ACCEPTED / GOVERNANCE_PENDING`.

On 2026-08-29, `LIVE15ControlCenter` changed owner from the stopped
`WinSW:LIVE15ControlCenter` service to the running Nomad allocation
`live15-control-center.control-center[0]`. This is an actual ControlCenter
cutover only: Recorder, RuntimeSupervisor, ACLs, UAC, registry, model/risk,
execution, training, holdout, and Production write paths were not changed.

## Immutable identity and upstream basis

```text
protected origin/main = b1e1894c7666e9763b3994cc8135ad0d7727698e
release id            = live15-b1e1894c7666-c0b6557e6fc9
source tree            = c0b6557e6fc9b8c6e6875abbd2dc7b7b6c8a478d
release manifest       = 420E1167FCC3F83EF0076ED197228A3C98ED46A76DCCBF539D48B5A020FB3596
artifact manifest      = 175C468974EA8C52F57CB5F8261D00382C2ABC6FBAC5D24C0CAFAE9304F49D5F
requirements lock      = 4521A9151C00797B004CD6AEB12A054DD5759BD211333D012736CED3E635A67E
runtime python         = 72B29481593C5DA37C99248C82777FBFB56217EA7809B771BC760D0A9ECB179B
```

The operator-installed release is under
`C:\Program Files\LIVE15\ControlCenterReleases`; its root, release, and app
grant `BUILTIN\Users` read/execute and retain full control only for `SYSTEM` /
Administrators. No reparse point was found. `DEP-PKG-001 verify-package` passed
both before and after the owner change. The runtime is CPython 3.13.15 at its
absolute protected venv path.

Current official Nomad v2.0.5, Microsoft, CPython 3.13.15, and official GitHub
source/test evidence is recorded in
`docs/research/NOMAD_CONTROL_CENTER_CUTOVER_FINAL_OFFICIAL_SOURCES_001.md`.

```text
OFFICIAL_DOCS_RETRIEVED_AT_TASK_TIME = YES
OFFICIAL_VERSION_OR_RELEASE = Nomad v2.0.5 / CPython 3.13.15
PROMPT_COPIED_VENDOR_PROCEDURE_USED_AS_AUTHORITY = NO
```

## Cutover sequence and configuration boundary

The release was verified, then a no-port Nomad batch preflight imported the
application from the immutable app root under the LocalService Nomad agent. Its
successful allocation was `866938a1-fbcb-8c84-14c2-7b46129ce545`, emitted
`LIVE15_CONTROL_CENTER_IMMUTABLE_ARTIFACT_PASS`, and was purged.

An explicit `task.user` attempt failed before launch with Windows token access
denied. The final jobspec uses Nomad raw_exec's documented default process
identity instead: the agent is the `NT AUTHORITY\LocalService` Windows service.
This is the smaller upstream path and adds no privilege, repair, or supervisor
logic.

The actual jobspec has SHA-256
`8D8B6BCECBFB832B7DAEEFC76B2373870528D28FA9C7676F7FAE6A73714F7B34` and uses:

- the absolute protected runtime and app source, with Python isolated mode;
- the legacy working root only for mutable read-only ControlCenter data paths,
  never as an import source;
- the two pre-existing absolute external Kalshi credential-path references as
  Nomad input variables. Secret content was never read, logged, or committed;
- loopback `127.0.0.1:8765` and Nomad's native HTTP check on `/api/health`;
- Nomad-native restart, health-gated update, and `auto_revert` controls.

Immediately before mutation, WinSW PID `5984`, child PIDs `7176` / `8384`, and
the `127.0.0.1:8765` listener were captured; `/api/health` returned HTTP 200.
`Stop-Service LIVE15ControlCenter` then reached `Stopped`, all three old PIDs
were gone, and the port had zero listeners before Nomad submission. Nomad job
plan was create-only and the submitted deployment was `75135e6a`.

## Bounded acceptance

```text
allocation              = eda23067-517c-29ea-6835-a59be27a6985
deployment              = 75135e6a (successful)
Nomad task status       = running
Nomad check             = success / HTTP 200
three direct health GET = HTTP 200; recorder_state=running; heartbeat=available
task restarts           = 0
listener                = 127.0.0.1:8765, PID 21564
owner chain             = python -> Nomad executor -> Nomad LocalService service
WinSW ControlCenter     = Stopped / Automatic / XML retained
UI GET /                = HTTP 200
```

The bounded observation does not repeat the generic two-hour soak, crash
recovery, service lifecycle burn-in, or bad-update test; their generic Nomad
evidence remains `NOMAD-POC-SECURE-001`. The running job's two credential-path
bindings match the prior WinSW references by redacted SHA-256 comparison.

No duplicate owner or port listener remained. WinSW rollback is preserved as a
stopped Automatic service with its unchanged XML retained; no deletion or
retirement occurred. No ControlCenter API endpoint that can request Recorder
control was called, and this task made no Production write call.

```text
ACTIVE_CONTROL_CENTER_OWNER = Nomad:live15-control-center
WINSW_CONTROL_CENTER_ACTIVE = NO
ROLLBACK_PRESERVED = YES
RECORDER_TOUCHED = NO
PRODUCTION_WRITES = 0
SUBTRACTIVE_REPLACEMENT = PASS
```

## Repository validation and review gate

The cutover runtime evidence above is complete, but repository acceptance remains
pending feature-branch CI and human merge. Maker review and Independent Checker
review passed without runtime mutation. The following local checks passed after
the final jobspec and receipt were written:

```text
nomad job validate (redacted placeholder input paths) = PASS
ruff check --no-cache . = PASS
ruff format --check --no-cache . = PASS (359 files)
pytest runtime_ownership + agent_context + control_center = 63 passed
git diff --check = PASS
```

Pytest emitted one pre-existing `PytestCacheWarning` because the isolated
worktree's configured runtime cache directory is not writable. No permission
repair was attempted; the test results were unaffected.

Only after the remaining CI gate passes may the task record
`CONTROL_CENTER_NOMAD_CUTOVER = VERIFIED` and publish a pull request as ready
for human merge.
