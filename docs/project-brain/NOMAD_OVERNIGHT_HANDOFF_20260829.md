# NOMAD-OVERNIGHT-HANDOFF-20260829: continuation snapshot

**Purpose.** This is the compact, durable continuation note for a new Codex
chat. It describes only the isolated Windows Nomad POC and its evidence as of
the last recorded observation on 2026-08-29 PT. It is not a deployment receipt,
Production authorization, merge instruction, or permission to access holdout.

## Read first

1. `AGENTS.md` → `PROJECT_CHARTER.md` → `CONTEXT.md` → `CURRENT_STATE.md` →
   `PROJECT_PROGRESS.md`.
2. This handoff note, then the targeted detail in
   `PROJECT_PROGRESS_DETAIL_20260829.md`.
3. Rediscover live POC state and durable evidence under
   `D:\LIVE15_NOMAD_POC`; do not infer that an old PID, allocation, checkpoint
   or background process is still current.

The only authoritative conclusion about a long-running phase is its final
durable evidence/checkpoint. A detached process or a prior status message is
not completion.

## Current project position

- `NOMAD-POC-SECURE-001` is **VERIFIED for the isolated POC phase**. The service-model burn-in and
  Nomad-native bad-update auto-revert are verified; the observation-only
  two-hour POC soak completed with 24 healthy iterations. Its final durable
  receipt is reconciled below. This is never a Production runtime replacement
  or cutover approval.
- `ST-005` remains separately `CODE_READY_PENDING_60MIN_PROOF`. The Nomad POC
  does not authorize a Recorder restart, storage mutation, retention action or
  its formal 60-minute proof.
- Web / Control Center work remains a separate, truth-first workstream. It may
  display evidence, but cannot manufacture health, gain Production control or
  replace the runtime owner.
- Shadow Recorder, fault injection, throughput and future Nomad/Consul shadow
  work are planned bounded tasks. Start each on its own branch/PR only after
  reading its own authority and acceptance contract.

## Resolved sleep-time privilege sequence

The POC uses official Nomad v2.0.5 Windows-service behavior, not a custom
LIVE15 supervisor. The privileged surface is intentionally fixed and small:

- the protected, hash-verified Nomad binary is non-user-writable;
- the Windows service account is `LocalService`, never LocalSystem;
- POC config/data/log scope is `D:\LIVE15_NOMAD_POC`;
- fixed bridge actions are allowlisted; there is no arbitrary PowerShell,
  command, executable, service, path, account or `RUN_ANY_COMMAND` operation;
- the sole HTTP.sys reservation is
  `http://127.0.0.1:18080/` for `NT AUTHORITY\LOCAL SERVICE`.

Two earlier findings must remain in the history because they prevent repeat
mistakes:

1. An initially installed candidate targeted a similarly named protected file,
   but the scheduled bridge actually ran
   `C:\ProgramData\LIVE15-Admin\runner.ps1`. A separate explicit corrective
   UAC atomically replaced that actual runner with the Checker-reviewed,
   hash-verified candidate while retaining protected ACL and rollback backup.
2. The first fixed native URLACL invocation failed closed because Windows
   `netsh` does not permit `sddl=` together with `listen`/`delegate`. It created
   no URLACL. The user then explicitly authorized one corrected, exact native
   operation:

   `netsh http add urlacl url=http://127.0.0.1:18080/ sddl=D:(A;;GX;;;LS)`

   The correction was then read back and validated by the real LocalService
   workload; it did not broaden the bridge.

The known planned POC privilege gates are therefore cleared. Do **not** request
blanket future elevation. If a genuinely unforeseen elevated action appears,
mark only that action `HUMAN_GATE/BLOCKED`, preserve evidence, and continue an
independent safe Project Brain task. Never use that contingency to touch
Production.

## Verified POC evidence boundary

The following is verified in the isolated POC only:

1. Protected binary integrity/ACL and LocalService service configuration.
2. Fixed URLACL read-back, then a real LocalService workload binding loopback
   `18080`.
3. Nomad-native HTTP health success, workload restart/recovery, service restart
   and allocation rediscovery.
4. A good jobspec stabilised; a deliberately bad update returned HTTP 503;
   Nomad marked it failed at its progress deadline and automatically reverted
   to the prior stable version. The recovered allocation again passed native
   health and HTTP 200.
5. Local validation and independent review completed for the relevant POC
   documentation/preflight boundary; hosted CI remains deliberately deferred.

Evidence lives under `D:\LIVE15_NOMAD_POC\generic-poc\logs`, including
`pre-sleep-end-to-end-burn-in-20260829T0800Z.log`,
`nomad-native-auto-revert-20260829T0811Z.log`, secure-bridge action evidence,
and the soak's durable checkpoint/final summary. Re-discover filenames and
checksums rather than reconstructing them from chat text.

### Final two-hour soak receipt (authoritative for this phase)

- File: `D:\LIVE15_NOMAD_POC\generic-poc\logs\nomad-soak-observer.log`.
- Terminal line: `2026-08-29T10:20:31.2100764+00:00 soak_complete iterations=24`.
- SHA-256 at reconciliation:
  `3830C7A698D22CA0748F045D8F2EB4A559B66D8D627E9A5BC2DE39FBF49FCB66`.
- The 24 five-minute observations recorded `service=Running`, stable job version
  `15`, the same allocation, native health success, and HTTP `200`. This proves
  only the bounded isolated POC observation phase; it is not Production proof.

Two older files are historical progress drafts, **not** final soak receipts:

- `D:\LIVE15_NOMAD_POC\state\validation-status.txt` was last written
  `2026-08-29T08:21:00Z` and still says `RUNNING`; SHA-256
  `30F756AA518F687D077646BCF8AC67E7222449473386822322D3715729BE46A8`.
- `D:\LIVE15_NOMAD_POC\logs\validation-final-summary.txt` was last written
  `2026-08-29T05:15:38Z`, before the successful observation pass, and contains
  preliminary failures; SHA-256
  `46B80EF389CAB9029F0E6D43F3F35902FE3466E43BD95C42214AFCAEA97DFCC7`.

Do not rewrite those runtime-side files merely to make their text agree. For
recovery, use the terminal observer receipt above and record any future phase
in a new bounded receipt.

## Engineering rules that the user explicitly adopted

- **Maker / Checker / Project Brain:** one bounded function → one task/branch/
  PR. Maker creates scoped evidence; Checker independently reviews contract and
  diff. A PASS does not authorize merge or deployment.
- **Upstream Reuse First:** official docs/release notes → pinned upstream
  source/tests/examples → upstream issues/PRs/discussions → mature maintained,
  license-compatible implementation → broader authoritative research → narrow
  local adaptation only last.
- **Reuse over reimplementation / Thin Adapter:** Nomad owns generic service,
  health, restart, update and rollback behavior. LIVE15 adds only the smallest
  POC config, domain and evidence adapter.
- **Anti-spaghetti:** competing supervisors, duplicated lifecycle paths,
  repeated special cases or contradictory invariants are consolidation signals,
  not justification for another patch. Retire/supersede local workarounds when
  the supported upstream path makes them redundant.
- **Runtime truth:** `MERGED != DEPLOYED != VERIFIED`; a green local suite,
  PR, background PID or UI view never substitutes for durable live evidence.

## CI, safety and recovery protocol

- `CI_DEFERRED_QUOTA` is the default. The prior one-time hosted CI authorization
  was consumed on the earlier Project Brain tracking PR. Do not push merely to
  run CI, do not rerun it, and do not call deferred CI a PASS. Re-enable hosted
  CI only on a later explicit user authorization for one ready SHA.
- No merge, Production cutover, Production writes, holdout access, model
  training/promotion, Hard Risk/sizing/execution policy change or irreversible
  policy action is authorized here.
- Preserve the dirty Production root `D:\LIVE15_QUANT`; use a clean, isolated
  worktree for a scoped task. Do not run generic cleanup or kill processes by
  name. Validate exact POC ownership before any bounded cleanup.
- If the soak is healthy, preserve it and wait for a useful checkpoint. If it
  completes, verify its final evidence, update this Project Brain on its own
  docs PR, and stop at the appropriate human merge/review gate. If it fails,
  record one smallest reproducible blocker and follow Upstream Reuse First
  before any local change.

## Next bounded decision

The next safe action is **not** a Production migration. The two-hour soak's
final evidence is reconciled and is a completed POC phase, not a deployment
decision. Next, decide in a separately scoped task,
whether a non-production Shadow Recorder/service-discovery validation has an
explicit contract. A later Production provider or cutover decision requires
new human authority and cannot be inferred from this POC.
