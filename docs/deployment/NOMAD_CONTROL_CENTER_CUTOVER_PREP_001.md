# NOMAD-CONTROL-CENTER-CUTOVER-PREP-001

**Status:** `BLOCKED_HUMAN_GATE / ENVIRONMENT_OPERATOR_INSTALLATION`.

## Scope and result

This was a read-only cutover-preparation audit. It did not submit a Nomad job,
create a candidate artifact, alter ACLs, read credential contents, or start,
stop, restart, uninstall, or replace any service.

The current installed `LIVE15ControlCenter` is owned by:

```text
Windows SCM -> WinSW:LIVE15ControlCenter -> python -m live15_quant.control_center
```

The installed service runs as `LocalSystem`. The verified Nomad agent remains
an independent Windows SCM service running as `LocalService`. That POC fact
does not transfer the current ControlCenter workload to Nomad.

## Single blocking prerequisite

The installed ControlCenter cannot yet be used as a safe Nomad candidate.
Read-only inspection found that its active release is explicitly `UNPROVEN`,
while its executable/release/bootstrap root is writable by `Authenticated
Users`. Its installed WinSW sidecar starts the mutable working-tree module
directly rather than the tracked SHA-verifying release runner. The two external
credential-path references are bound to that `LocalSystem` WinSW sidecar; their
contents were neither read nor copied.

Therefore there is no reviewed SHA-pinned, non-user-writable artifact identity
or verified LocalService credential boundary for a real ControlCenter Nomad
allocation. A jobspec pointing at the current installation would violate the
release and secret-boundary requirements, so none was created.

This is an `environment/operator/installation` prerequisite, not a reason to
add an ACL manager, secret manager, supervisor, restart controller, rollback
state machine, or second deployment controller.

## Required human gate before a fresh preparation task

An independently approved deployment/install operation must first establish a
reviewed SHA-pinned ControlCenter release in a non-user-writable runtime root,
preserve the existing WinSW rollback package, and provide a reviewed external
credential reference that is safe for the Nomad `LocalService` identity. It
must not expose credential material in Git, release manifests, jobspecs, or
evidence receipts.

After that gate, rerun this preparation task from then-current `origin/main`.
Only then may it build and validate a candidate jobspec, application health
contract, and operator cutover/rollback receipt. Actual cutover remains a
separate human-approved `NOMAD-CONTROL-CENTER-CUTOVER-001` task.

## Upstream ownership retained

- Windows SCM owns the Nomad agent service.
- Nomad owns allocation lifecycle, restart policy, native checks, update, and
  native revert for a future submitted workload.
- LIVE15 retains Kalshi and Recorder truth, freshness/gap policy, persistence,
  Hard Risk, execution boundaries, and only thin configuration/evidence work.

No generic LIVE15 lifecycle machinery was added or extended.
