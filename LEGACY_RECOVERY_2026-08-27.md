# LIVE15_QUANT legacy recovery source — 2026-08-27 PT

> Temporary reconciliation source for CTX-002. This is an AI-readable projection of the user-provided Excel workbook `LIVE15_QUANT_项目路线图与持续项目笔记_2026-08-27_日终记忆恢复版(2).xlsx` (SHA-256 `c860392473e6ace3c14c830697a079f246a6195142d8e2bb2eb204f845afaaf5`).
>
> This file is **not** a competing Project Brain. Reconcile durable facts into the existing Project Brain, then keep this file only as legacy/disaster-recovery evidence or retire it through a later reviewed change.

## Authority order

1. Current Production runtime / DB / manifests / service evidence.
2. `origin/main` code and merged evidence.
3. Existing Git Project Brain.
4. This legacy recovery source.
5. Chat history.

Lower authority must never overwrite higher-authority current truth.

**Merged != deployed.**

## Recovery read order represented by the workbook

The original workbook instructed a fresh session to read, in order:

1. `09_上下文恢复`
2. `14_协作偏好与执行规则`
3. `02_当前状态`
4. `13_2026-08-27详细工作日志`
5. `07_技术架构`
6. `08_关键决策`
7. `03_任务总表`
8. `12_研究数据策略与Authority`
9. `11_模型现状与vNext`

The Git Project Brain should ultimately make the Excel unnecessary for normal session bootstrap.

---

# 1. Current state at day-end recovery point

## Git / runtime

| Item | Recovered state | Notes |
| --- | --- | --- |
| Repository | `QHongboQ/LIVE15_QUANT` | Windows canonical checkout: `D:\LIVE15_QUANT` |
| Latest protected main at recovery point | `cce1ebc1ad7e29fb85fea9f86d9f1b9cb924fb17` | PR #40 merge SHA; every new session must re-check `origin/main` |
| Python | 3.13.15 | canonical `.venv`; Production install must be regular non-editable |
| kalshi-sdk | 12.0.0 | pinned; do not patch vendor source; thin typed boundary |
| Production deployment truth | Last explicitly proven Production closeout was after PR #34 | PR #35–#40 merges did **not** automatically prove deployment |
| Recorder last proven | 15-minute closeout previously showed running, 10/10 synchronized, gaps 0, fatal null | Later rollover incident self-recovered; #40 fix was merged but had no post-merge Production rollover proof at this recovery point |
| Control Center / Terminal | Terminal V3 exists in Production; #39 health projection merged | Production effect requires deployment proof |
| Production writes | 0 / automatic writes prohibited | Read capability != trading permission |

## Research / model

| Item | Recovered state | Notes |
| --- | --- | --- |
| Research Data Authority | COMPLETE; PR #29 merged (`080e7804...`) | source registry + typed freshness + `ResearchUniverseSnapshot` + `/api/research-data` |
| Archive research adapter | COMPLETE; PR #36 merged (`53380072...`) | replay-verified COLD -> provenance-bearing observations; bounded read |
| MVN-003 isolated runner | COMPLETE; PR #35 merged (`c8afe5e2...`) | only `ResearchUniverseSnapshot` + `CanonicalEvidenceSnapshot`; atomic checkpoint/resume; isolation |
| Archive -> MVN integration | COMPLETE; PR #38 merged (`79e4f708...`) | real verified COLD one-chunk / 10,000-event smoke; resumed vs uninterrupted digest identical |
| Frozen holdout | `UNREVEALED_FROZEN` | integration proof holdout access=false; do not inspect/tune/infer |
| Formal long-run training gate | NOT RUN / **NO TRAINING_GO** | Archive->runner technical success is not authorization for long training |

## Runtime/source incidents closed in code

| Area | Recovered state | Notes |
| --- | --- | --- |
| Runtime ownership/self-healing | PR #30 merged (`cd5d64d9...`) | Recorder -> WinSW; Control Center -> WinSW; RuntimeSupervisor -> WinSW; Supervisor owns only registered auxiliaries |
| Runtime diagnostics / ACL | PR #31 merged; Codex service ACL resolved | non-admin query/start/stop/interrogate within least privilege; do not reopen without new evidence |
| Pyth Hermes compatibility | PR #33 merged | upgraded endpoint; 4 valid feeds; exact WTI feed has no authoritative replacement |
| Pyth feed-local circuit breaker | PR #37 merged (`eeed2e5b...`) | exact WTI unavailable -> isolate only that feed; 300s reprobe; siblings continue; degraded health honest |
| DataGap restart idempotency | PR #34 merged (`c2ded1d4...`) | logical gap fact identity separated from provenance; semantic conflicts still fail closed |
| Kalshi rollover/resync | PR #40 merged (`cce1ebc1...`) | one-side-missing snapshot narrow normalization; both sides missing rejected; rollover uses session replacement, no concurrent recv |
| Auxiliary health projection | PR #39 merged (`7f790fb8...`) | `ON_DEMAND` / `PAUSED_BY_DESIGN` no longer become false STALE from old child heartbeat |

## Storage/archive

| Item | Recovered state | Notes |
| --- | --- | --- |
| Retention effectiveness audit | PASS -> `TEMPORARY_BACKLOG` | HOT physical ~101.731GB; configured retention 6h; ordinary HOT ~52h; archive lag ~29.6h |
| Archive/purge | active / progressing | PURGED 38,576 chunks / 292,153,405 rows; PURGE_ELIGIBLE 2,068 chunks / 20,460,492 rows; QUARANTINED 26 / 21,594 rows; FAILED=0; WAITING=0 |
| Compaction | `NOT_ELIGIBLE` | freelist ~42.9MB / 0.042%; gate requires >=8GiB and >=25%; do not VACUUM/compact |
| 60m catch-up trend | BLOCKED / rerun later | first attempt correctly stopped before sampling because Kalshi WS was reconnecting, sync 0/10, seq_gaps 10 |

## Context/governance

| Item | Recovered state | Notes |
| --- | --- | --- |
| Git Project Brain | established by PR #32 (`e788a235...`) | durable engineering authority; Excel is human-readable recovery mirror |
| Project Brain reconciliation | DEFERRED at recovery point | original blocker was `LEGACY_DURABLE_SOURCE_MISSING`; this file now removes that source-access blocker |
| User stop instruction at day end | PAUSE AFTER CURRENT BATCH | do not auto-start deployment, training, or storage trend before fresh recovery and human decision |

---

# 2. Strategic / durable decisions

These were marked in the workbook as decisions not to repeatedly relitigate unless new evidence exists.

- Do not promise stable profit. Optimize for **Capital Preservation > Stability > Positive Expectancy > Return Maximization**.
- No martingale, all-in, or loss chasing.
- Kalshi finalized settlement is the sole terminal label truth.
- Strict as-of. Unknown/missing values are not filled with zero. Any value not proven available at decision time is unavailable.
- Archive raw truth comes first: only verified checksum/replay/manifest evidence can become purgeable; quarantined raw is never purged.
- Frozen final holdout is revealed once only.
- Validated factor evidence comes before unnecessary model complexity.
- Kalshi implied probability is benchmark / executable-price evidence, not the model target.
- Positions are continuously re-evaluated; do not force hold-to-settlement when close/continue/settle EV changes.
- Historical backtests cannot replace fresh forward evidence.
- `kalshi-sdk` is generic venue infrastructure; LIVE15 must not rebuild auth/WS/SID/reconnect in parallel.
- LIVE15 attaches after SDK typed decode/output; do not create a competing authoritative pre-dispatch stream.
- Engineering discipline: **Working First -> Upstream First -> Narrow Fix**.
- Unknown local residue is never blindly cleaned; diff/hash/classify/salvage first.
- Protected main, CI/Checker, and human merge gates remain mandatory.
- **Merged != deployed**. Production truth requires installed-code/service/runtime proof.
- Runtime invariant: **ONE COMPONENT · ONE OWNER · ONE HEALTH TRUTH · ONE RECOVERY AUTHORITY**.
- `ON_DEMAND` / `PAUSED_BY_DESIGN` are desired states and should not become stale failures merely because an old child heartbeat exists.
- Kalshi 15m rollover must replace the SDK session; do not call `update_subscription` from an active receive loop because pinned SDK waits for an ack using `recv`, which conflicts with the existing reader.
- A sparse orderbook snapshot with exactly one omitted side may normalize that side to `[]` only when the other side is a valid list. Both sides missing or malformed remains fail-closed.
- Exact WTI upstream unavailability must not silently swap to a different oil-price feed. Isolate the exact source and low-frequency reprobe it.
- A 15-minute prediction horizon does **not** mean training history should be limited to 15 minutes or two days.
- Feature freshness, training recency, and forward-OOS freshness are different concepts.
- Dataset v1/v2 are immutable experiment/reproduction artifacts, not current research-history authority.
- Only approved research chain: verified source -> Research Data Authority -> `ResearchUniverseSnapshot` -> `CanonicalEvidenceSnapshot` -> `ResearchRunInput` -> MVN-003 runner.
- Archive->runner integration PASS != TRAINING_GO.
- Git Project Brain is the durable shared engineering memory; Excel is a human-readable legacy/recovery mirror.
- Durable rules must be both **STORED and RECOVERABLE** by a fresh AI session.

---

# 3. User collaboration and execution rules

## Role split

- ChatGPT: strategy, research, architecture, task definition, acceptance, result judgment.
- Codex: code execution, tests, Git worktree/branch, Checker/CI, PR preparation.
- Codex must not silently expand project scope or change strategic architecture.

## Model / reasoning defaults

- Complex LIVE15 tasks: **Codex Terra + High**.
- Runtime, data authority, archive, Production, Project Brain, bug diagnosis, training gates: Terra High.
- Simple mechanical read-only audits may use Medium.
- Every Codex task prompt should explicitly state: recommended model, reasoning level, Goal, authority/boundaries, prohibited actions, acceptance/validation, RETURN format.

## Upstream First

For bugs or hard engineering problems, use this order before inventing a local patch:

1. official documentation;
2. pinned dependency source/tests;
3. GitHub Issues/PR;
4. mature/reference implementation;
5. broader web;
6. local reproduction;
7. narrow fix;
8. regression -> Checker -> CI.

External similar issues are only hypotheses/prior art until LIVE15 evidence proves applicability.

## Error handling

Do not stop just because an exception or test failure appears. Within safety bounds continue through:

`research -> reproduce -> classify owner -> instrument -> narrow fix -> regression -> Checker/CI`

Only return BLOCKED for a real **single smallest blocker** such as permissions, unavailable external evidence/resource, or an explicit safety/authority boundary.

## Git / workspace safety

- Use a small number of non-overlapping parallel lanes, normally 2–3 maximum.
- Each mutating task uses an isolated worktree and feature branch.
- Avoid simultaneous mutation of canonical checkout, `.venv`, Recorder, or Production DB.
- `D:\LIVE15_QUANT` should remain a clean deployment/canonical checkout when practical.
- Never blind `reset --hard` or `git clean` unknown residue.
- Protected main: branch -> tests -> independent Checker -> CI -> PR -> human approval -> merge.
- User normally performs merge manually. Codex should stop at a ready PR and say **DO NOT MERGE automatically**.

## Production / data / research safety

- Automatic Production writes remain 0 unless explicitly authorized.
- Hard Risk, risk caps, position sizing, execution permission, settlement labels, and destructive retention actions require elevated human authority.
- Frozen holdout payload remains opaque during development.
- Research must go through the unique authority chain; do not use Dataset IDs as current history coverage.
- Do not hand-edit Production SQLite just to make a task continue.
- Do not VACUUM/compact without its independent gate.

## Knowledge persistence

A major bug must not live only in chat. Durable root cause, upstream evidence, fix and regression belong in `BUG_REGISTRY.md` or the relevant Project Brain authority.

When the user says pause/switch chats, finish bounded current work, record a recovery point, and do not automatically launch the next risky stage.

---

# 4. Recovered task ledger

The original workbook's task table used symbols/labels that mix completion and workstream state. CTX-002 should normalize these into a canonical project log rather than copying labels blindly.

## Completed / established foundations

| ID | Area | Task | Recovered state / evidence |
| --- | --- | --- | --- |
| D-001 | Data | Data Layer Certification / Pre-Model Final Audit | complete; fault/leakage/WS/gap/storage/security PASS; `DATA LAYER CERTIFIED` |
| D-002 | Dataset | Frozen Dataset v1 | complete; immutable; strict as-of; event/time split; 1091 events / 7984 rows / 42 features |
| D-003 | Dataset | Dataset v2 + frozen holdout | complete; 3489 events / 25975 rows; holdout `UNREVEALED_FROZEN` |
| M-001 | Models | Model Zoo v1/v2 + walk-forward | complete; v2 Paper baseline |
| M-002 | Models | Model Architecture v3 | complete architectural asset |
| P-001 | Paper | Paper/Shadow forward infrastructure | ongoing foundation; ledger/fill/accounting/settlement/idempotency |
| SDK-001 | SDK | kalshi-sdk v12 authoritative foundation | complete; SDK owns generic auth/REST/typed WS/SID/reconnect |
| UI-010 | UI | LIVE15 Terminal V3 | complete; PR #25 merged + deployed |
| OPS-010 | Ops | least-privilege service ACL | complete |
| OPS-011 | Ops | workspace salvage/recovery isolation | complete; stash preserved |
| AR-003 | Archive | historical baseline-gap classification/quarantine | complete; raw preserved |
| AR-004 | Archive | epoch-aware baseline reconciliation code | complete; PR #26 merged |
| AR-005 | Archive | Production 15m proof | complete at its historical proof point; 10/10 sync, gaps0, archive growth, FAILED0/WAITING0 |
| FAC-001 | Factors | Factor-001R batch | complete; 96 candidates + BH-FDR; holdout untouched; 0 validated |
| RD-001 | Data/Research | Research Data Authority + three-layer freshness | complete; PR #29 |
| UI-011 | UI/Data | Research Data / Data Intelligence page | complete; PR #29 |
| RT-OWN-001 | Runtime | one owner/one health truth/self-healing | complete; PR #30 |
| CTX-001 | Context | full agent skills + Git Project Brain | complete; PR #32 |
| PYTH-001 | Provider | Hermes upgraded endpoint compatibility | complete; PR #33 |
| GAP-001 | Runtime/Data | DataGap sequential restart idempotency | complete; PR #34 |
| MVN-003 | Research Runner | isolated research preflight runner | complete; PR #35 |
| AR-RD-001 | Archive/Research | verified COLD research adapter | complete; PR #36 |
| PYTH-002 | Provider | feed-local circuit breaker / low-frequency reprobe | complete code; PR #37 |
| RUN-004 | Runner | Archive -> RDA -> Universe -> Canonical Evidence -> MVN-003 integration | complete; PR #38; **not TRAINING_GO** |
| UI-012 | UI/Ops | intentional auxiliary health projection | complete code; PR #39 |
| KWS-001 | Kalshi WS | sparse snapshot + single-reader rollover/resync fix | complete code; PR #40 |
| ST-006 | Storage | retention effectiveness read-only audit | complete; `TEMPORARY_BACKLOG` classification |

## Active / deferred / next-gate work

| ID | Area | Task | Recovered state / caution |
| --- | --- | --- | --- |
| ST-005 | Storage | 60min archive/purge catch-up trend | BLOCKED/WAITING at recovery point; prior attempt correctly stopped on unhealthy Kalshi WS; rerun only after runtime health proof |
| DATA-004 | Data | expand independent UTC days/regimes | ongoing; raw rows != independent evidence |
| FAC-002 | Factors | next evidence batch | not started; decision-time-safe, interpretable, chronological/BH-FDR; no feature stuffing |
| FAC-003 | Factors | fixed 96-factor rerun on unified universe | pending authority/runtime gates; do not expand search first |
| VAL-001 | Validation | chronological anti-overfit gate | not started; event grouping/purge/embargo/frozen holdout/cost stress |
| MVN-001 | Models | underlying path / terminal probability | not started |
| MVN-002 | Models | after-cost net-edge contract | not started; contract before training |
| MOD-UNC-001 | Models | true uncertainty/confidence | roadmap; renamed from older conflicting MVN-003 ID |
| MVN-004 | Models | dynamic exit / continuous re-evaluation | not started |
| DEC-001 | Decision | rolling 15m decision engine | not started |
| SIG-001..004 | Signals | OFI/OBI/microprice; CVD/add-cancel; path risk; futures/spot lead-lag | future; each group requires ablation |
| MOD-004 | Models | Champion/Challenger promotion + rollback | future; immutable versions |
| MOD-005 | Models | periodic/batch retrain | future; event/time/drift triggered, not per event |
| RISK-001 | Risk | Hard Risk closed loop | mandatory before real money |
| EXE-001 | Execution | execution closed loop | mandatory before real money |
| EXE-002 | Execution | restart reconciliation | mandatory before real money; unknown fail-closed |
| SEC-001 | Security | pre-production security audit | mandatory before tiny Production |
| PROD-001 | Production | tiny 1-contract low-frequency pilot | future only; Hard Risk/security/forward evidence/human approval required |
| SESS-001 | Sessions | market session awareness | optimization |
| OPS-002 | Ops | incident taxonomy | optimization |
| ST-004 | Storage | deep archive / second-copy lifecycle | deferred; does not block model mainline |
| AI-001 | AI | news/event intelligence overlay | future; cannot authorize orders |
| AI-002 | AI/Ops | auto-repair independent lane | future; never self-deploy Production changes |
| CLOUD-001 | Cloud | 24/7 private cloud | future; after local stability |
| DF-001 | Data/Provider | DepthFeed formal H2 adapter | optional/key-required; only if experiments actually need H2 |
| CTX-002 | Context | Project Brain reconciliation vs legacy Excel | **READY TO RESUME**; this file resolves the missing-source blocker |
| DEP-001 | Deployment | post-merge current-main Production deploy + bounded runtime proof | requires explicit human approval; merged #37/#39/#40 != deployed |
| TRN-001 | Training Gate | `LONG_RUN_TRAINING_FINAL_GO_NO_GO` | NOT STARTED; current state = **NO TRAINING_GO** |

---

# 5. Detailed 2026-08-27 recovery timeline

- PR #30: runtime ownership / self-healing governance merged; becomes runtime governance baseline.
- PR #31: Runtime blocker diagnostics/service delegation; least-privilege service ACL and Pyth transport classification; merged, Checker/CI PASS; ACL later validated.
- PR #32: Git Project Brain / skills. Created `PROJECT_CHARTER.md`, `CONTEXT.md`, `CURRENT_STATE.md`, `BUG_REGISTRY.md`, ADRs, AGENTS and recovery simulation. Fresh-session recovery simulation PASS, but later reconciliation showed some legacy Excel rules were still not recoverable.
- PR #33: Pyth Hermes endpoint compatibility. Upstream check proved four valid feeds work on upgraded host while exact WTI ID still returns unavailable and has no authoritative replacement. Batch failure split to per-feed semantics.
- PR #34: DataGap incident. Forensic reproduction proved sequential restart alone could reproduce the conflict. Root cause: same logical gap fact but changing recorder/session provenance was included in the old content hash. Fix separated logical fact identity from provenance while preserving fail-closed semantic conflict detection. No Production DB rewrite.
- Post-#34 runtime closeout: regular non-editable install + Recorder restart + 15m proof; 10/10 synchronized, gaps0, written records grew, archive verified grew, FAILED0/WAITING0, fatal null, `/api/research-data` 200. This is the last clearly proven Production deployment in the workbook.
- PR #35: MVN-003 isolated runner. Windows CI originally hit `KeyboardInterrupt`. Upstream First found `os.kill(pid,0)` is not a safe pure liveness probe on Windows. Replaced with WinAPI process handle/exit-code + creation identity; CI/Checker green.
- PR #36: verified COLD archive research adapter; strict checksum, replay, identity, source/received as-of, quarantine exclusion; RDA exposes archive research seam.
- Storage retention audit: read-only. Result `TEMPORARY_BACKLOG`, not proof that purge/compression was broken. Archive and purge are advancing; compaction gate not met.
- First 60m storage trend attempt: correctly stopped before sampling because Kalshi WS was reconnecting, 0/10 sync, seq_gaps10. No mutation.
- Kalshi WS forensic: Upstream First confirmed two causes. (A) real Production snapshots can omit one orderbook side, which strict SDK decoding rejected. (B) LIVE15 rollover called SDK `update_subscription`, whose ack wait does its own `recv`, causing a second reader against the active receive loop and a websockets `ConcurrencyError`. Host-level session replacement eventually self-recovered.
- PR #37: Pyth feed-local circuit breaker. Confirmed exact WTI ID is unavailable on legacy/upgraded Hermes; do not choose a substitute. Remove only that exact feed from shared SSE and reprobe every 300s; global auth/TLS/429 remain fail-closed.
- PR #38: real verified-COLD -> archive adapter -> RDA -> `ResearchUniverseSnapshot` -> `CanonicalEvidenceSnapshot` -> `ResearchRunInput` -> MVN-003 runner integration. One chunk/10,000 events, pause/resume deterministic, holdout false. Technical PASS, explicitly **not TRAINING_GO**.
- PR #39: Control Center intentional auxiliary projection. Current RUNNING supervisor receipt is authoritative; `ON_DEMAND`/`PAUSED_BY_DESIGN` remain neutral; historical PIDs are not trusted; stale RUNNING remains strict.
- PR #40: Kalshi rollover/resync fix. Reused a narrow Production SDK decode normalizer without creating an authoritative pre-dispatch stream. Exactly one missing side may become `[]`; both missing/malformed reject. Rollover now exits old SDK session and creates a fresh session rather than calling `update_subscription` inside the active reader. Full 1062 passed +14 skipped; CI success.
- Project Brain reconciliation attempt: Codex searched repo/Git history/local documents but could not find the legacy Excel, so it correctly returned `LEGACY_DURABLE_SOURCE_MISSING` and made no repository/runtime mutation. This file is intended to remove that blocker.
- Day-end instruction: stop launching new work, deployment, training, or storage trend; preserve recovery point and switch chat.

---

# 6. Intended post-recovery sequence

The workbook's intended sequence, subject to fresh reality checks and human approval, was:

1. Recover context and verify current `origin/main`, open PRs, installed Production code, PIDs/ownership and health.
2. Resume **CTX-002 Project Brain reconciliation** using this legacy source.
3. With explicit human approval, run **DEP-001** current-main Production deploy/proof: regular non-editable install, only necessary service restart, 10/10 sync, gaps0, honest Pyth degraded state, neutral intentional UI states, and a real 15-minute rollover proof.
4. Run read-only **ST-005** 60-minute archive/purge catch-up trend only when runtime stop conditions are healthy.
5. Run **TRN-001 `LONG_RUN_TRAINING_FINAL_GO_NO_GO`**. Training is allowed only if every data/runtime/resource/anti-overfit/holdout gate passes. Until then: **NO TRAINING_GO**.
6. Re-run the fixed 96-factor set using whole-event chronological validation + 600s purge/embargo + BH-FDR, without widening the search first.
7. Only after robust factor/feature evidence, continue Model Zoo / rolling decisions / fresh forward validation.

---

# 7. CTX-002 reconciliation requirements derived from the workbook

CTX-002 should not create `MEMORY_V2`, `RULES_V2`, or a second project-brain architecture.

Classify each legacy durable fact as:

- `ALREADY_PRESENT`
- `PRESENT_BUT_WEAKER`
- `MISSING`
- `SUPERSEDED`
- `TRANSIENT_DO_NOT_STORE`
- `CONFLICT_REQUIRES_HUMAN`

At minimum reconcile:

- stale `CURRENT_STATE.md` blocker/status claims;
- durable Upstream First and error-handling rules;
- Terra/High task-spec convention;
- Merged != deployed;
- runtime ownership;
- Dataset artifact vs Research Data Authority distinction;
- frozen-holdout rules;
- Archive->runner PASS != TRAINING_GO;
- durable bug knowledge for DataGap restart, Pyth exact-source unavailability/circuit breaker, Kalshi sparse snapshot + concurrent-recv/session-replacement incident, intentional auxiliary state projection, archive quarantine and replay-baseline rules;
- a durable AI-readable task/progress ledger, preferably `PROJECT_PROGRESS.md`, without turning `CURRENT_STATE.md` into a historical diary.

Recommended canonical task states for `PROJECT_PROGRESS.md`:

`PLANNED -> IN_PROGRESS -> BLOCKED -> PR_OPEN -> MERGED -> DEPLOYED -> VERIFIED -> CLOSED`, plus `CANCELLED` as needed. Research results may additionally use `PASS`, `FAIL`, `NO_GO`.

Task records should capture, where applicable:

- Task ID / title / area
- Status / result
- Started / last updated
- Branch / PR / merge commit
- Deployed commit if applicable
- Evidence
- Blocker
- Important cautions
- Next action
- Human gate

Do **not** store transient second-by-second PIDs, heartbeat timestamps, or measurements in the durable task ledger; keep those in bounded runtime/evidence artifacts.

A fresh-session recovery test should be able to answer, without chat history:

1. project objective;
2. current phase;
3. current task and next task;
4. what is merely merged vs deployed vs runtime-verified;
5. whether TRAINING_GO exists;
6. Dataset v1/v2 role;
7. Research Data Authority role;
8. Archive->runner PASS meaning;
9. frozen holdout state;
10. runtime ownership;
11. Kalshi rollover root cause/fix;
12. Pyth exact-WTI policy;
13. Upstream First order;
14. human-only gates;
15. where to read task history and current project status.
