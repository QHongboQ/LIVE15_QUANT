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
| WS-RESYNC-001 + GAP-002 | CLOSED / PASS | PR #117 preserves the first Production FAIL receipt; PR #120 preserves the merged second Production PASS receipt. | GAP002 is closed and has no further execution route. |
| RECORDER_LIFECYCLE_TO_NOMAD | VERIFIED / COMPLETE | `docs/project-brain/capabilities/control-center.md`; `docs/project-brain/dependencies/platform/runtime-ownership.md` | Recorder Production lifecycle is Nomad-owned. |
| PROJECT-BRAIN-SINGLE-AUTHORITY-CONSOLIDATION-001 | MERGED / CONSOLIDATION_COMPLETE | PR #122 | This is a Project Brain governance closeout; it did not perform an upstream replacement. |
| RUNTIME-LIFECYCLE-CONSOLIDATION | COMPLETE / VERIFIED | `docs/project-brain/dependencies/platform/runtime-ownership.md`; PR #129 | Nomad + Windows SCM lifecycle replacement is adopted and Production verified; cold boot passed, RuntimeSupervisor and the managed `paper_forward` wrapper are retired, and dual owner is absent. |
| WEB-APPLICATION-SHELL | COMPLETE / VERIFIED | `docs/project-brain/capabilities/control-center.md`; PRs #131–#139 | React Admin + Material UI terminal is the sole ControlCenter Web owner; the legacy handwritten shell is retired and Production verification is complete. |
| VECTOR-TELEMETRY-BOUNDED-POC-001 | MERGED / TECHNICAL_PASS / PRODUCTION_NO_GO | PR #147 | Vector proved technically feasible in isolation, but no redundant generic telemetry owner was identified to retire; no adoption or Production integration is authorized. |
| COMMERCIAL-STORAGE-BAKEOFF | MERGED / PARQUET+ZSTD_SELECTED | PR #157 | Parquet + ZSTD won the bounded archive bakeoff; Arrow IPC is retained as historical benchmark evidence, not the selected Production cold format. |
| PARQUET-HOT-COLD-CLOSED-LOOP | MERGED / PRODUCTION_DISABLED | PR #158 | Production-capable Parquet + ZSTD archive path exists with semantic/replay verification, manifest state, bounded idempotent purge, and fail-closed recovery; merge did not activate Production archive or purge. |
| PARQUET-NAMED-MULTI-ROOT | MERGED | PR #160 | Archive chunks bind to named roots with centralized manifest and one active writer root; historical-root lookup remains fail closed. |
| RUNTIME-DEPLOY-SIMPLIFICATION | MERGED / HOST_ROLLOUT_PENDING | PR #167 | Movable-venv promotion/custom recovery is retired; immutable runtime revisions and native Nomad lifecycle ownership are merged, but the host still requires explicit runtime preparation and Recorder rollout verification. |

## Superseded standalone work

| Task | Status / result | Retained authority | Durable implication |
| --- | --- | --- | --- |
| ST-005 | CANCELLED / SUPERSEDED | `docs/project-brain/capabilities/records/recorder/throughput-proof.md` | The standalone custom-throughput optimization lane is retired. Its bounded 60-minute measurement contract remains available on demand; it is not evidence that the proof passed and does not authorize upstream adoption. |
| PARQUET-PRODUCTION-ACCEPTANCE-PHASE1-001 | CANCELLED / HISTORICAL_STOP_RECEIPT | PR #159 | The first attempt stopped before any Production archive/manifest/purge mutation because Recorder health was degraded by WTI/Pyth; HOT rows deleted = 0. Do not merge or extend this old branch as current execution. |

## Active and gated work

### Current Production runtime authority

CENTRAL_RUNTIME_AUTHORITY = ESTABLISHED
CANONICAL_RUNTIME = `CANONICAL_LIVE15_PRODUCTION_RUNTIME`
RUNTIME_AUTHORITY = `docs/project-brain/dependencies/platform/runtime-ownership.md`

Future LIVE15 Python workloads must resolve that authority before provisioning
or selecting a runtime. The runtime leaf owns the full contract; a separate
runtime requires concrete incompatibility evidence.

| Task | Status / result | Next action / caution | Human gate |
| --- | --- | --- | --- |
| IMMUTABLE-PRODUCTION-RUNTIME-ROLLOUT | PLANNED / NEXT | Prepare and verify the immutable Production runtime from current `main` without stopping Recorder; then run Recorder deploy Preview. Apply is a separate Production deployment decision. Archive remains disabled during this step. | Runtime preparation may write only the new immutable revision; Recorder deployment requires separate explicit authorization |
| WTI-RETIREMENT | PLANNED / AFTER_RUNTIME_ROLLOUT | Retire WTI completely as its own narrow task/PR: project universe 10->9, Recorder expectations 9, UI fixed 3x3, Pyth/health/model/archive future-write assumptions removed, no disabled compatibility layer. | Separate PR and deployment; do not mix with archive acceptance |
| PARQUET-PRODUCTION-ACCEPTANCE-PHASE1-002 | PLANNED / AFTER_WTI | With Recorder RUNNING and archive/purge safety gates intact, archive one bounded historical unit (~100k–250k rows) to Parquet+ZSTD, semantic/replay verify, reach `VERIFIED/PURGE_ELIGIBLE`, and STOP BEFORE PURGE. `PRODUCTION HOT ROWS DELETED = 0`. | Production archive/manifest write requires explicit authorization; purge is not authorized |
| COMMERCIAL-ARCHIVE-PACKAGE-UPSTREAM-ASSEMBLY | MERGED / IMPLEMENTATION_SELECTED | Candidate selection is complete for the current cold path: Parquet+ZSTD is selected and merged. Preserve HOT SQLite truth, deterministic replay, manifest state, contiguous-range purge authorization, restart recovery, and fail-closed storage behavior during Production acceptance. | No purge weakening or alternate format adoption without a new bounded task |
| VECTOR-TELEMETRY | DEFERRED / LATER | The post-Web Vector selection was valid before the storage-capacity problem reprioritized execution. Retain PR #147 evidence; do not deploy or integrate Vector until this responsibility is explicitly promoted again. | No Vector adoption or Production action while deferred |
| TRN-001 | BLOCKED / HOLDOUT_CONTAMINATION_REMEDIATION_REQUIRED | A broad local artifact search displayed frozen-holdout rows and was stopped immediately. The previous `UNREVEALED` state is invalid; exposed content was not used for WS/GAP/H2 implementation, test thresholds, parameters, or code changes. Do not reopen it to measure scope. A separate remediation/replacement decision is required before the formal gate or any training. | Training/holdout |

## Planning route

`docs/project-brain/plan/current-roadmap.md` owns execution sequence. The
training and H2 capability authorities are under `capabilities/README.md`.

## Governance route

Engineering/design rules are owned by `docs/agents/change-protocol.md`; upstream classifications
are owned by `docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md`. This ledger does not duplicate them.
