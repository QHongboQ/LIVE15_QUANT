# LIVE15 upstream replacement matrix

**Status:** decision record / no adoption authorized by this document.

This matrix records where a mature GitHub project may replace unstable generic
LIVE15 infrastructure. It does not install dependencies, change Production,
alter the Recorder truth contract, authorize training, or permit trading.

## Consolidation classification authority

Maturity alone never implies adoption. These five classifications are the stable project taxonomy:

| Classification | Current candidates / retained responsibility |
| --- | --- |
| **MUST_REPLACE** | Nomad + Windows SCM for generic workload lifecycle/service-wrapper/deployment state; React Admin + Material UI for the generic web shell; Vector as the default generic telemetry pipeline (OTel only if a measured OTLP/tracing requirement supersedes it) |
| **CONDITIONAL** | Grafana, NATS JetStream, DuckDB/Polars/Arrow, Consul, Temporal, and MLflow; each requires a measured unmet need and its own bounded decision |
| **RESEARCH_ONLY** | Time-Series-Library, TLOB/DeepLOB/MLPLOB references, Qlib, EarnHFT, AlphaGPT, and RD-Agent(Q) |
| **KEEP_LOCAL** | official pinned Kalshi SDK adapter boundary; Recorder/settlement/gap/as-of/provenance truth; authoritative SQLite/archive writes; feature/snapshot/leakage contracts; XGBoost baseline; Router/Decision/Hard Risk/Production authorization |
| **DO_NOT_INTRODUCE** | Kafka/Redpanda or another large/overlapping control plane without measured failure of the smaller selected owner and a new architecture decision |

The detailed rows below preserve prior evidence state and responsibility boundaries. They do not
form a second taxonomy or authorize adoption.

## Detail evidence-state legend

- **ADOPTED-POC:** isolated proof exists; production adoption still needs a
  separately reviewed migration task.
- **REPLACE-CANDIDATE:** generic responsibility is a good fit, subject to a
  bounded POC and Maker/Checker evidence.
- **CONDITIONAL:** introduce only when the stated requirement is measured.
- **ADAPT/KEEP:** upstream can provide generic machinery, but LIVE15 retains
  the domain contract and authoritative state.
- **RESEARCH-ONLY:** reference or offline adapter only; never a runtime
  dependency without a new decision.
- **DO-NOT-REPLACE:** no generic project may replace this safety or truth
  authority.

## Runtime, web, observability, and throughput

| LIVE15 function | Current unstable/custom surface | Upstream project | Decision | What it replaces / does not replace | Main advantage and gate |
| --- | --- | --- | --- | --- | --- |
| Agent and workload lifecycle | WinSW ownership plus `LIVE15RuntimeSupervisor` child-PID restart | [HashiCorp Nomad](https://github.com/hashicorp/nomad) + Windows SCM | **ADOPTED-POC** | Replaces generic scheduling, allocation lifecycle, task restart, deployment health, update, and native revert. Does not replace Recorder semantics, risk, or execution. | One upstream owner for workload recovery; the isolated v2.0.5 LocalService POC already passed. Migrate one non-Production workload at a time. |
| Windows service wrapper | Per-component WinSW XML and custom install scripts | Nomad agent installed as native Windows Service; SCM remains the host authority | **REPLACE-CANDIDATE** | Can replace WinSW for workloads moved into Nomad allocations. Does not remove SCM or justify deleting all WinSW before migration. | Removes duplicate wrapper/supervisor paths. Requires workload-specific service, identity, ACL, log, and rollback proof. |
| Deployment/update/rollback | Local deployment state and ad-hoc restart paths | Nomad `update`/deployment/revert plus existing SHA-pinned release provenance | **ADAPT/KEEP** | Replaces generic deployment state machinery. Does not roll back database truth, labels, risk policy, or arbitrary filesystem state. | Native health-gated update and revert; preserve LIVE15 release hashes and human deployment gates. |
| Web application shell | Hand-built static `web/app.js`, routes, tables, refresh/error state | [React Admin](https://github.com/marmelab/react-admin) + Material UI | **REPLACE-CANDIDATE** | Replaces custom browser plumbing, routing, tables, loading/error handling, and theme shell. Keeps FastAPI typed projections and read-only domain rules. | Mature REST/GraphQL admin primitives and controllable dark theme. First POC should migrate only health and markets, display-only. |
| Operations dashboard | Custom operational pages mixed into the Control Center | [Grafana](https://github.com/grafana/grafana) | **CONDITIONAL** | Can replace a metrics/logs/traces dashboard. Does not replace the typed markets, settlement, training-truth, or Recorder UI contracts. | Strong dark operational dashboards and alerting. Keep separate from the primary domain UI; review AGPL obligations and datasource adapters. |
| Logs and telemetry pipeline | Custom log/health aggregation | [Vector](https://github.com/vectordotdev/vector) (default) or [OpenTelemetry Collector](https://github.com/open-telemetry/opentelemetry-collector) | **REPLACE-CANDIDATE / ONE CHOICE** | Replaces generic logs/metrics/traces collection and routing. Does not enter the Kalshi hot path or decide data truth. | Vector is the default for this small high-frequency system's low-overhead log/metric path; OTel wins if standardized traces/OTLP are required. Do not run overlapping collectors without a separate decision. |
| Event buffering and replay | In-process queues and bespoke consumer coordination | [NATS JetStream](https://github.com/nats-io/nats-server) | **CONDITIONAL** | Can replace generic durable buffering, acknowledgement, replay, and consumer decoupling. Does not replace Recorder truth, idempotency, or gap policy. | At-least-once durable streams and replay. Require a non-Production POC with event IDs, duplicate handling, backpressure, and loss evidence. |
| Service discovery | Hard-coded or local service addresses | Nomad native service discovery; [Consul](https://github.com/hashicorp/consul) only if required | **ADAPT/CONDITIONAL** | Nomad native discovery is sufficient for the current POC. Consul may replace generic multi-node discovery later, not the current single-host path. | Avoids an unnecessary control plane now. A Consul decision requires DNS/mTLS/KV/scale requirements and a separate shadow task. |
| Scheduled/batch runtime work | Custom scheduled commands and process wrappers | Nomad periodic/batch jobs; [Temporal](https://github.com/temporalio/temporal) only for durable multi-step workflows | **ADAPT/CONDITIONAL** | Nomad can host bounded jobs. Temporal may replace a true durable workflow engine, not a real-time collector or service supervisor. | Keep the control plane small; require a measured workflow requirement before Temporal. |
| Data/feature throughput | SQLite scans, archive/purge and materialization bottlenecks under ST-005 | [Polars](https://github.com/pola-rs/polars), [Apache Arrow](https://github.com/apache/arrow), or [DuckDB](https://github.com/duckdb/duckdb) | **CONDITIONAL** | May replace generic batch transformation or read-heavy analytical scans. Does not replace authoritative SQLite writes, settlement labels, gap facts, or immutable archive evidence. | Vectorized execution and columnar scans may address measured throughput. Benchmark against real bounded fixtures first; no speculative migration. |
| Very high-volume event streaming | None authorized; Kafka-like systems would add a large control plane | [Redpanda](https://github.com/redpanda-data/redpanda) or Apache Kafka | **DO-NOT-INTRODUCE-NOW** | Possible future streaming substrate only if NATS and measured capacity are insufficient. | Operational cost is disproportionate to the current small system; no adoption without a capacity failure and independent design review. |

## Data truth, research, models, and decision safety

| LIVE15 function | Current owner | Upstream project/reference | Decision | Boundary |
| --- | --- | --- | --- | --- |
| Kalshi transport and typed API | Pinned `kalshi-sdk==12.0.0` plus `KalshiGateway` | Official pinned SDK | **ADAPT/KEEP** | Keep the SDK transport/auth/subscription ownership; LIVE15 keeps the immutable domain adapter. |
| Recorder, settlement, and source truth | `Recorder` / `RecorderStore` / verified archive | No generic replacement selected | **DO-NOT-REPLACE** | Only official finalized settlement, strict as-of, source identity, quarantine, and immutable evidence can authorize truth. |
| WebSocket gap/recovery semantics | LIVE15 Reliability and Recorder | No generic replacement selected | **DO-NOT-REPLACE** | Nomad can restart the process; it cannot infer snapshot validity, sequence continuity, or gap closure. |
| Raw authoritative storage | SQLite WAL hot store plus verified cold archive | SQLite/Parquet remain storage choices; DuckDB/Arrow are read-path candidates | **ADAPT/KEEP** | No replacement until measured retention/throughput evidence proves a specific bottleneck. Never use an analytics engine as settlement truth by assumption. |
| Feature/materializer contracts | LIVE15 Materializer and Dataset authority | Polars/Arrow/DuckDB conditional acceleration only | **ADAPT/CONDITIONAL** | External engines may accelerate deterministic computation; schema, provenance, leakage and snapshot boundaries stay LIVE15-owned. |
| Baseline nonlinear model | Pinned XGBoost dependency | Official XGBoost project | **ADAPT/KEEP** | Continue as a baseline until a fresh forward Challenger proves improvement; no framework swap by popularity. |
| Path Expert research | LIVE15 offline adapter | [Time-Series-Library](https://github.com/thuml/Time-Series-Library) | **RESEARCH-ONLY** | Reference/adapter for TimeXer, PatchTST, iTransformer, TimeMixer, TimesNet, or DLinear. No runtime import or Production promotion. |
| Microstructure Expert research | LIVE15 H2/H0 bounded adapter | [TLOB](https://github.com/LeonardoBerti00/TLOB), DeepLOB/MLPLOB references | **RESEARCH-ONLY** | Only after real H2/H0 capability, overlap, sequence semantics, costs, and chronological evidence pass. |
| Research orchestration | LIVE15 Training Snapshot and RDA/CES | [Qlib](https://github.com/microsoft/qlib) | **RESEARCH-ONLY** | Vocabulary and fold-plan reference only at the pinned revision; it cannot bypass LIVE15 data authority or holdout rules. |
| Hierarchical model architecture | LIVE15 Router design | [EarnHFT](https://github.com/TradeMaster-NTU/EarnHFT) | **RESEARCH-ONLY** | Architecture reference only. The pinned review found no license file; no code import or distribution is authorized. |
| Autonomous factor/model R&D | LIVE15 Factor Factory and guarded research loop | AlphaGPT / RD-Agent(Q) concepts | **RESEARCH-ONLY** | Concepts may inform offline candidate generation; all leakage, ablation, FDR, cost, forward-OOS and promotion gates remain LIVE15-owned. |
| Experiment tracking and registry | No Production-authorized replacement | MLflow or equivalent, future review | **CONDITIONAL** | Consider only after the immutable Training Snapshot and Champion/Challenger contracts are stable; never let a registry promote a model by itself. |
| Decision, Hard Risk, and execution | LIVE15 Decision / Hard Risk / Execution | No generic replacement selected | **DO-NOT-REPLACE** | Models and upstream infrastructure may propose or host work; only independent LIVE15 safety policy can veto or authorize an action. |

## Candidate dependency order (design reference only)

Current execution ordering is owned only by
`docs/project-brain/plan/current-roadmap.md`. The order below preserves dependency/design intent for
later bounded selection; it does not create NEXT/ACTIVE tasks.

1. Nomad/SCM for one isolated workload, then gradual WinSW retirement per
   workload. Migrate low-risk read-only workloads first; Recorder is late.
2. React Admin + Material UI for a display-only health/markets web POC.
3. Vector as the default telemetry/log candidate, then Grafana if a separate
   operations dashboard is needed. Compare with OTel only if trace/OTLP
   requirements justify it.
4. Measure ST-005 and Recorder ingress before choosing NATS JetStream,
   Polars/Arrow, or DuckDB; use the smallest candidate that addresses the
   measured bottleneck.
5. Do not add Consul, Temporal, Kafka/Redpanda, or another control plane without
   a concrete unmet requirement.
6. Preserve the existing LIVE15 data/model/safety contracts; evaluate model
   projects only through the documented offline, pinned, leakage-safe path.

## Replacement acceptance rule

Upstream adoption is expected to be **subtractive**. The target is fewer
LIVE15-owned generic code paths, not a larger integration framework.

- Freeze the legacy generic implementation once an upstream owner is selected;
  only narrowly scoped Production safety/rollback fixes may extend it during
  migration.
- Prefer deletion or retirement of redundant supervisors, restart/rollback
  machinery, wrappers, frontend plumbing and telemetry aggregation after each
  workload-specific replacement is proven.
- Keep integrations to pinned configuration, thin adapters and fail-closed
  validation. Do not reproduce upstream behavior locally.
- If the migration materially increases custom lifecycle/platform code, adds a
  second/third special-case path, or causes both legacy and replacement control
  planes to keep growing, stop for architecture review.
- Passing tests do not override this rule. A replacement that does not simplify
  ownership is not ready.
- User-facing Codex tasks must recover durable rules from the current Git Project
  Brain and select model/reasoning dynamically for complexity and token cost;
  do not require the user to re-paste durable context or use a fixed expensive
  model by default.

Detailed execution guidance is in
`docs/roadmap/UPSTREAM_REPLACEMENT_EXECUTION_001.md`.

Every adoption requires its own isolated task, upstream documentation/source
review, Maker, Independent Checker, local validation, durable evidence, and one
PR. No row in this matrix authorizes Production deployment, holdout access,
training, Hard Risk changes, trading writes, or a merge.
