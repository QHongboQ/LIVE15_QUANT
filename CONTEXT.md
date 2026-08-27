# LIVE15 context

This is a vocabulary and pointer map, not a project history. Read only the
linked detail that matches the task.

## Architecture map

`Kalshi SDK → immutable LIVE15 gateway → Reliability → Recorder/RecorderStore →
Materializer/Dataset/Paper → Model/Decision/Hard Risk/Execution → Control Center`.

The SDK owns transport, authentication, typed subscriptions, SID routing and
generic REST primitives. LIVE15 owns the 15-minute domain, source/reliability
policy, storage, lifecycle, datasets, models, paper, risk, and UI. Detail:
`docs/kalshi_native_architecture.md`.

## Domain vocabulary

| Term | Meaning | Detail |
| --- | --- | --- |
| H0 | LIVE15 Recorder data and verified cold archive; quarantined ranges excluded | `docs/research_data_authority.md` |
| H1 | Official Kalshi historical completed-market/trade/candle evidence; not full historical L2 | `docs/historical_research.md` |
| H2 | Credentialed DepthFeed historical L2, valid only after identity and overlap validation | `docs/research_data_authority.md` |
| HOT / COLD | Recorder SQLite is hot authoritative capture; verified archive is cold immutable evidence | `docs/continuous_recorder.md` |
| ResearchUniverse | Source-registry, deduplicated, conflict-quarantined authorized research coverage | `docs/research_data_authority.md` |
| Training Snapshot | Immutable reproduction selection built from a universe; never a replacement history store | `docs/training_dataset.md` |
| Feature freshness | Decision-time seconds/minutes as-of validity | `docs/research_data_authority.md` |
| Training recency | Development history over sessions/weeks | `docs/research_data_authority.md` |
| Forward OOS freshness | Evidence strictly after a frozen specification | `docs/research_data_authority.md` |
| Runtime owner | The single service/component authority permitted to restart a component | `docs/runtime_ownership_and_self_healing.md` |

## Fast routing

- Recorder/archive/replay issue: `docs/continuous_recorder.md`, then
  `docs/agents/debug-routing.md`.
- SDK or venue boundary: `docs/kalshi-sdk-v12-migration.md` and
  `docs/kalshi_native_architecture.md`.
- Dataset/model question: `docs/training_dataset.md`,
  `docs/model_artifact_lineage.md`, then the relevant evidence report.
- Runtime ownership/recovery: `docs/runtime_ownership_and_self_healing.md`
  and `deploy/windows/runtime-ownership.json`.
- Policy decision: `PROJECT_CHARTER.md`, then `docs/adr/README.md`.

Do not scan the repository for context. Start here, then follow one pointer at
a time.
