# Project Brain V2 migration inventory

**Role:** EVIDENCE. Migration map for `PROJECT-BRAIN-V2-RECURSIVE-HIERARCHY-001`; Git commits and PRs remain canonical history.

## Mapping created before the recursive move

| Source fact or section | Recursive authoritative home | Retained evidence / note |
| --- | --- | --- |
| `AGENTS.md` architecture and SDK boundary | `dependencies/platform/software-modules.md` | Native architecture documents remain supporting detail. |
| `AGENTS.md` data/model truth | `capabilities/model-governance/data-and-validation.md` | RDA freshness detail is in `capabilities/records/research-data.md`. |
| `AGENTS.md` detailed reuse, platform-stop, and PR procedure | `docs/agents/change-protocol.md`, `runtime-official-source-policy.md`, `debug-routing.md` | `AGENTS.md` retains the permanent top-level gate and pointers. |
| `AGENTS.md` task-closeout procedure | `status/task-closeout.md` | No closeout rule is discarded. |
| completed foundation: protected-main governance | `docs/adr/0001-protected-main-governance.md`, `docs/agents/change-protocol.md` | Permanent policy plus Git/PR history remain authoritative. |
| completed foundation: RDA and `/api/research-data` | `docs/research_data_authority.md`, `capabilities/records/research-data.md` | RDA and aggregate endpoint remain the authority. |
| completed foundation: runtime ownership design | `dependencies/platform/runtime-ownership.md`, `deploy/windows/runtime-ownership.json` | JSON remains machine-readable authority. |
| completed foundation: Terminal V3 / HOT-COLD archive | dated evidence selected from `PROJECT_PROGRESS.md` or `PROJECT_PROGRESS_DETAIL_20260828.md` | Evidence only; no new history authority. |
| completed foundation: Skills/context system | `docs/agents/skills-installation.md`, `.agents/skills-manifest.json`, `docs/agents/context-recovery-simulation.md` | Installed-skill provenance and recovery contract remain authoritative. |
| completed foundation: DEP-PKG / NIGHT-001 / ControlCenter hardening | `PROJECT_PROGRESS.md` recent-completed-foundations ledger | Git/PR rows remain canonical history. |
| `CURRENT_STATE.md` bounded WinSW receipt and `MERGED/DEPLOYED/VERIFIED` distinction | `status/legacy-runtime-receipt.md` | Receipt remains historical evidence, not a current deployment claim. |
| `CURRENT_STATE.md` H2-TRAIN-003 and H2 capability limits | `capabilities/model-governance/training-and-promotion.md` | H2 remains inside the WS/GAP acceptance path. |
| `CURRENT_STATE.md` ST-005 proof limit | `capabilities/records/recorder/throughput-proof.md` | UI state does not replace the 60-minute proof. |
| `CURRENT_STATE.md` training, holdout, and Production-write limit | `capabilities/model-governance/training-and-promotion.md` | No training action is authorized. |
| `capabilities/recorder.md` truth/non-migration boundary | `capabilities/records/recorder/truth.md` | `ST-005` proof moves separately to `recorder/throughput-proof.md`. |
| `capabilities/reliability.md` | `capabilities/records/reliability.md` | Current roadmap remains sole sequence authority. |
| `capabilities/research-data.md` | `capabilities/records/research-data.md` | RDA source documents retained. |
| `capabilities/training-and-models.md` invariants | `capabilities/model-governance/data-and-validation.md` | Narrow data/validation authority. |
| `capabilities/training-and-models.md` training/holdout gates | `capabilities/model-governance/training-and-promotion.md` | No training action is authorized. |
| `dependencies/software-modules.md` | `dependencies/platform/software-modules.md` | Package and native-architecture sources retained. |
| `dependencies/runtime-ownership.md` | `dependencies/platform/runtime-ownership.md` | JSON remains machine-readable authority. |
| `constraints/parallel-development.md` | `constraints/execution/parallel-development.md` | Freeze rule retained. |
| `constraints/runtime-upstream-boundary.md` | `constraints/execution/runtime-upstream-boundary.md` | Permanent authority remains AGENTS/Charter. |
| ControlCenter / GAP002 closure / roadmap / status closeout | existing narrow leaf stays in place | No artificial one-child folder is created. |

No durable fact was deleted to meet a token target. This map was created before moves; dated evidence remains in place, and V2 adds recursive current-authority pointers rather than a generic history tree.
