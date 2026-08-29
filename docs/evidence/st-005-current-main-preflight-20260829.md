# ST-005 then-current-main proof preflight — 2026-08-29

Classification: `ST_005_PROOF_BLOCKED_PENDING_DEPLOYMENT`.

This is a bounded, read-only preflight. It did not restart a service, read or
mutate a Production database, change retention eligibility, or start the
60-minute proof window. Production writes remained zero.

## Observation identity

- preflight completed: `2026-08-29T12:07:37.5899597Z`;
- then-authoritative `origin/main`: `4d088930cc83634faf807188fba386f7a7a34bea`;
- protected checkout: `D:\LIVE15_QUANT`, branch `main`, HEAD
  `c2ded1d4fc172b184db4e0fb6faf6b5d6d0100e0` (37 commits behind
  `origin/main` at observation time);
- protected checkout is dirty: tracked `deploy/windows/live15-recorder.xml`,
  untracked release/bootstrap/pointer artifacts, and an inaccessible
  `checker-temp-dep-venv-process-chain-004` directory;
- `LIVE15Recorder`, `LIVE15ControlCenter` and `LIVE15RuntimeSupervisor` report
  `Running` as built-in `LocalSystem`, with executables under
  `D:\LIVE15_QUANT\.local-tools\winsw`;
- active pointer is `legacy-unproven-08989b3efd7d19f6` with manifest hash
  `610b8b467bed47851672242d714c468c649951ae2c7f0f57b3796278e30715cb`;
- protected working-tree `native_recorder.py` blob is
  `17bc94ab02a8897520d19bec2eb6688c02b8a119`, while then-current-main and the
  modern release `live15-13fcc4e7fd73-baa2e33725fd\app` contain blob
  `137823c8c08b45c30d0b72241a8a11398994caef`;
- the known health receipt was last written at
  `2026-08-29T12:06:47.5156796Z` and was inspected only by metadata/hash
  (`985DCC73A4EDFF8A8B602703A555523E43BF58B7142039D9288130060BCF09E8`).

## Result and exact blocker

At observation time, the then-current-main ST-005 instrumentation was present
in the unactivated modern release, but the running service identity was not
bound to that SHA: the active pointer remained legacy-unproven and the protected
checkout differed from `origin/main`. Therefore no valid 60-minute catch-up
proof could begin.

The next action requires a separately human-authorized, SHA-verifiable
current-main deployment. After that gate, perform one fresh read-only preflight
and, only if it passes, one continuous approximately 60-minute proof window.
Do not restart services, mutate storage, or stitch this preflight to a later
observation.
