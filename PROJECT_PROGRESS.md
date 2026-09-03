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

## Superseded standalone work

| Task | Status / result | Retained authority | Durable implication |
| --- | --- | --- | --- |
| ST-005 | CANCELLED / SUPERSEDED | `docs/project-brain/capabilities/records/recorder/throughput-proof.md` | The standalone custom-throughput optimization lane is retired. Its bounded 60-minute measurement contract remains available on demand; it is not evidence that the proof passed and does not authorize upstream adoption. |

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
| VECTOR-TELEMETRY | PLANNED / NEXT | Select the bounded Vector telemetry/log replacement decision or POC. Preserve Recorder truth and keep the collector out of the Recorder hot path; compare OTel only if a measured OTLP/tracing requirement exists. | Separate bounded task; no Vector adoption or Production action in this closeout |
| TRN-001 | BLOCKED / HOLDOUT_CONTAMINATION_REMEDIATION_REQUIRED | A broad local artifact search displayed frozen-holdout rows and was stopped immediately. The previous `UNREVEALED` state is invalid; exposed content was not used for WS/GAP/H2 implementation, test thresholds, parameters, or code changes. Do not reopen it to measure scope. A separate remediation/replacement decision is required before the formal gate or any training. | Training/holdout |

## Planning route

`docs/project-brain/plan/current-roadmap.md` owns execution sequence. The
training and H2 capability authorities are under `capabilities/README.md`.

## Governance route

Engineering/design rules are owned by `docs/agents/change-protocol.md`; upstream classifications
are owned by `docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md`. This ledger does not duplicate them.
