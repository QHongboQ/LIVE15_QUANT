# FLOW-005A — Model Readiness Matrix

| Model family | Role | Status | Allowed inputs | Blocker |
|---|---|---|---|---|
| `path_expert_foundation` | Path Expert contract | `READY` | H1 official history, H0 featureized inputs | complete sequence/detail coverage for full training |
| `terminal_baseline` | Terminal / decision expert | `READY` | H1 official history, H0 featureized inputs | offline development only |
| `microstructure_expert` | TLOB / DeepLOB / MLPLOB | `PARTIAL` | H0 Recorder L2, H2 snapshots/ticks where available | H2 ticks unavailable; commodity L2 unverified |
| `path_sequence_challengers` | TimeXer/PatchTST/iTransformer/TimeMixer/TimesNet/DLinear | `RESEARCH_ONLY` | approved H1/H0 sequences | no broad training in FLOW-005A |
| `rolling_research_orchestration` | Qlib reference | `RESEARCH_ONLY` | offline manifests | not a runtime dependency |
| `hierarchical_architecture_reference` | EarnHFT reference | `ARCHITECTURE_ONLY` | expert outputs/regime descriptors | router and execution layers deferred |
| `router_policy_foundation` | future router/policy interface | `ARCHITECTURE_ONLY` | expert outputs/regime descriptors | no runtime wiring or policy learning |
| `microstructure_commodity` | commodity microstructure | `BLOCKED` | H0 only if independently verified | no verified historical commodity L2 |

All statuses are readiness decisions, not profitability or promotion decisions. Dataset v2
holdout remains `UNREVEALED_FROZEN` and is not an allowed source.
