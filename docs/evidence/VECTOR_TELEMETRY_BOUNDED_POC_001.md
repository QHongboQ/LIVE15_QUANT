# Vector telemetry bounded POC

**Task:** `VECTOR-TELEMETRY-BOUNDED-POC-001`
**Baseline:** `origin/main` `2dce889e6833ce102ae25b9ce541d774ca1713c8`
**Status:** technical POC PASS; production replacement NO-GO
**Scope:** a portable, workspace-local Vector process only. No service installation, production
configuration, Recorder hot-path, database, or live log input was used.

## Upstream resolution

| Required evidence | Result |
| --- | --- |
| Upstream official docs | Vector's [file source](https://vector.dev/docs/reference/configuration/sources/file/), [file sink](https://vector.dev/docs/reference/configuration/sinks/file/), and [validation command](https://vector.dev/docs/administration/validating/) document the exact POC components and fail-closed config validation. |
| Upstream tutorials/examples | The file-source documentation includes a minimal file configuration; the official configuration reference covers TOML component topology. |
| Upstream GitHub source/tests | Official [Vector releases](https://github.com/vectordotdev/vector/releases) supplied the portable Windows archive and published checksum. |
| Upstream GitHub issues/PRs | No issue was needed: the selected upstream path completed the bounded local POC. |
| Mature alternatives | Not evaluated: this is a feasibility POC for the already-approved later candidate, not a provider-selection decision. |
| Standard upstream path found | YES — Vector file source -> file sink with `vector validate`. |
| Upstream resolution exhausted | NO — no failure remains to escalate. |

## Existing-owner decision

| Question | Finding |
| --- | --- |
| Existing authority found | YES — LIVE15 keeps domain health, Recorder truth, and terminal projections. |
| Existing capability found | YES — the application already emits domain-specific structured logs and health. |
| Existing implementation suitable for generic aggregation replacement | NO evidence yet. The POC did not identify a redundant generic collector/aggregator that Vector can delete. |
| Existing plan found | YES — Project Brain records Vector as a later, conditional generic telemetry candidate. |
| Why a new LIVE15 owner is not required | Vector provides the tested generic file-to-file pipeline; no LIVE15 telemetry framework was written. |

## Pinned isolated execution

The official portable `vector-0.58.0-x86_64-pc-windows-msvc.zip` was downloaded only into a
workspace-local POC directory. Its SHA-256 was verified against the published release value:

```text
72bbedf4772302f7f67e7db2120fe5b42e39ae65873c895876fc2038050c10c5
```

No MSI, Windows service, registry entry, Program Files write, or production configuration was
used. The temporary Vector process read only the repository's existing sanitized fixture
`tests/fixtures/kalshi_ws/production_sparse_snapshot_sanitized.json` and wrote only an isolated
workspace output file. The POC configuration was:

```toml
data_dir = '.../state'

[sources.fixture]
type = 'file'
include = ['.../production_sparse_snapshot_sanitized.json']
read_from = 'beginning'
ignore_checkpoints = true
file_key = ''
host_key = ''

[sinks.local_file]
type = 'file'
inputs = ['fixture']
path = '.../output/events.jsonl'

[sinks.local_file.encoding]
codec = 'json'
```

## Acceptance evidence

| Check | Result |
| --- | --- |
| `vector validate --deny-warnings` | PASS |
| Fixture events delivered to isolated sink | PASS — 25 JSONL events / 2,760 bytes on the first run |
| Production dependency | NONE |
| System service installation | NONE |
| Recorder hot-path change | NONE |
| Domain health/truth change | NONE |
| Five-second isolated resource observation | 19.39 MiB working set; 6.94 MiB private memory; 0.219 CPU seconds |
| Vector failure isolation | PASS by boundary: source and sink are local fixture/output only; the short-lived process was stopped after observation and no LIVE15 runtime process is an input, sink, or dependency. |

## Decision

`VECTOR_POC = TECHNICAL_PASS / PRODUCTION_NO_GO`.

The upstream tool performs the bounded generic pipeline with a thin configuration and verified
portable artifact. However, introducing it now would add a process and configuration without
retiring a proven redundant LIVE15 generic telemetry owner. That fails the task's subtractive
replacement condition. No deployment, install, service, application integration, or further
implementation is proposed.

Before reopening a production Vector decision, identify one specific generic aggregation owner to
retire, define a pinned release/install/recovery contract, measure source/sink resource bounds on
representative non-production logs, and prove that a Vector failure cannot affect Recorder truth.
