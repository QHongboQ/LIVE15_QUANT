# LIVE15 project progress

Compact ledger; `CURRENT_STATE.md` records whole-project orientation. Git commits and PRs are canonical history. Dated detail and handoff files are legacy evidence/task-detail only.

## Reading and update rule

Task status is one of `PLANNED`, `IN_PROGRESS`, `BLOCKED`, `PR_OPEN`, `MERGED`, `DEPLOYED`, `VERIFIED`, `CLOSED`, or `CANCELLED`; research result is separately `PASS`, `FAIL`, or `NO_GO` when applicable.

`MERGED != DEPLOYED`; `DEPLOYED != VERIFIED`; technical `PASS != TRAINING_GO`. Put volatile receipts, PIDs and measurements in bounded evidence, not here.

## Current reconciliation basis

- Project Brain V2 migration baseline was `c557d52` (merged PR #103). Always resolve current
  `origin/main` at task start; no fixed SHA is current authority. `MERGED != DEPLOYED` outside
  explicitly verified work.
- When an authority leaf selects dated evidence, use
  `docs/project-brain/PROJECT_PROGRESS_DETAIL_20260829.md`,
  `docs/project-brain/NOMAD_OVERNIGHT_HANDOFF_20260829.md`, or
  `docs/project-brain/NOMAD_MIGRATION_STATUS_20260830.md` as legacy evidence/task detail,
  not as canonical or detailed history.
- Current execution sequence: `docs/project-brain/plan/current-roadmap.md`.

## Durable closeouts relevant to current gates

| Task | Status / result | Evidence | Durable implication |
| --- | --- | --- | --- |
| WS-RESYNC-001 + GAP-002 | CLOSED / PASS | PRs #117/#120 | GAP002 is closed and has no further execution route. |
| RECORDER_LIFECYCLE_TO_NOMAD | VERIFIED / COMPLETE | runtime/control-center authorities | Recorder Production lifecycle is Nomad-owned. |
| PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 | MERGED / CONSOLIDATION_COMPLETE | PR #122 | Governance closeout only; it did not perform an upstream replacement. |
| RUNTIME-LIFECYCLE-CONSOLIDATION | COMPLETE / VERIFIED | PR #129 | Nomad + Windows SCM adopted; RuntimeSupervisor and managed paper wrapper retired. |
| WEB-APPLICATION-SHELL | COMPLETE / VERIFIED | PRs #131–#139 | React Admin + Material UI is the sole ControlCenter Web owner. |
| VECTOR-TELEMETRY-BOUNDED-POC-001 | MERGED / TECHNICAL_PASS / PRODUCTION_NO_GO | PR #147 | Feasible isolated POC; no adoption or Production integration authorized. |
| COMMERCIAL-STORAGE-BAKEOFF | MERGED / PARQUET+ZSTD_SELECTED | PR #157 | Parquet + ZSTD ranked first and became the archive-format candidate; Arrow IPC is historical benchmark/prototype evidence and S3/MinIO was not selected. |
| PARQUET-HOT-COLD-CLOSED-LOOP | MERGED / PRODUCTION_DISABLED | PR #158 | Production-capable Parquet + ZSTD path exists with semantic/replay verification, manifest state, bounded purge gates, and fail-closed recovery; merge did not activate Production archive or purge. |
| PARQUET-NAMED-MULTI-ROOT | MERGED | PR #160 | Archive chunks bind to named roots with centralized manifest and one active writer root; historical-root lookup fails closed. |
| RUNTIME-DEPLOY-SIMPLIFICATION | MERGED / HOST_ROLLOUT_PENDING | PR #167 | Movable-venv promotion/custom recovery is retired; immutable runtime revisions and native Nomad lifecycle ownership are merged, but host Runtime/Recorder rollout remains separately gated. |

## Superseded standalone work

| Task | Status / result | Retained authority | Durable implication |
| --- | --- | --- | --- |
| ST-005 | CANCELLED / SUPERSEDED | Recorder throughput authority | Standalone custom-throughput lane retired; its bounded measurement contract remains available on demand. |
| PARQUET-PRODUCTION-ACCEPTANCE-PHASE1-001 | CANCELLED / HISTORICAL_STOP_RECEIPT | PR #159 | Stopped before any Production archive/manifest/purge mutation; `PRODUCTION HOT ROWS DELETED = 0`. Do not merge or extend this old branch as current execution. |

## Active and gated work

### Current Production runtime authority

CENTRAL_RUNTIME_AUTHORITY = ESTABLISHED
CANONICAL_RUNTIME = `CANONICAL_LIVE15_PRODUCTION_RUNTIME`
RUNTIME_AUTHORITY = `docs/project-brain/dependencies/platform/runtime-ownership.md`

Future LIVE15 Python workloads must resolve that authority before provisioning or selecting a runtime. The runtime leaf owns the full contract; a separate runtime requires concrete incompatibility evidence.

| Task | Status / result | Next action / caution | Human gate |
| --- | --- | --- | --- |
| COMMERCIAL-ARCHIVE-PACKAGE-UPSTREAM-ASSEMBLY | PLANNED / NEXT | Current gate: prepare/verify immutable Production Runtime, then Recorder deploy Preview and separately authorized rollout verification. After that retire WTI 10->9, then fresh Phase1-002 with Recorder running. Preserve HOT SQLite truth, semantic/replay verification, manifest state, contiguous-range purge authorization, restart recovery, and fail-closed behavior. | Separate bounded tasks; no archive activation or purge during the Runtime gate |
| IMMUTABLE-PRODUCTION-RUNTIME-ROLLOUT | PLANNED / CURRENT_GATE | Prepare/verify from current main without stopping Recorder; rollout is separate and Nomad-owned. | Runtime preparation may create only the new immutable revision; Recorder rollout requires explicit authorization |
| WTI-RETIREMENT | PLANNED / AFTER_RUNTIME_ROLLOUT | Remove WTI completely: universe 10->9, Recorder expectation 9, UI fixed 3x3, and Pyth/health/model/archive future-write assumptions removed; no compatibility layer. | Separate PR and deployment |
| PARQUET-PRODUCTION-ACCEPTANCE-PHASE1-002 | PLANNED / AFTER_WTI | With Recorder RUNNING, archive one bounded historical unit (~100k–250k rows) to Parquet+ZSTD, semantic/replay verify, reach `VERIFIED/PURGE_ELIGIBLE`, and STOP BEFORE PURGE; `PRODUCTION HOT ROWS DELETED = 0`. | Production archive/manifest write requires explicit authorization; purge is not authorized |
| VECTOR-TELEMETRY | DEFERRED / LATER | Retain PR #147 evidence; no integration while deferred. | No Vector adoption or Production action |
| TRN-001 | BLOCKED / HOLDOUT_CONTAMINATION_REMEDIATION_REQUIRED | Separate remediation/replacement decision required before the formal training gate; do not reopen the exposed holdout to measure scope. | Training/holdout |

## Planning route

`docs/project-brain/plan/current-roadmap.md` owns execution sequence. Training/model capability authorities are under `docs/project-brain/capabilities/README.md`.

## Governance route

Engineering/design rules are owned by `docs/agents/change-protocol.md`; upstream classifications are owned by `docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md`.
