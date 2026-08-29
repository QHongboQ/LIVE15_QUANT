# NomadAutomationReceipt v1

This is a contract-only, versioned evidence shape for a future thin LIVE15
adapter. It is not a Nomad control API, a health authority, or a Production
authorization. The example is derived from the completed isolated POC receipt;
it is not a new runtime observation.

## Required fields

| Field | Required shape and rule |
| --- | --- |
| `schema_version` | Exact string `1`. |
| `observed_at_utc` | RFC 3339 timestamp with an explicit UTC offset; not in the future. |
| `nomad_version` | Semver string; the verified POC value is `2.0.5`. |
| `service_account` | Account reported by SCM; the POC value is `LocalService`. |
| `service_state` | SCM state string; a healthy receipt requires `running`. |
| `job_id` | Non-empty Nomad job identifier. |
| `allocation_id` | Non-empty allocation identifier tied to `job_id`. |
| `desired_status` | Nomad desired status, normally `run` or `stop`. |
| `client_status` | Nomad client status, such as `pending`, `running`, `complete`, `failed`, or `lost`. |
| `deployment_status` | Nomad deployment status, such as `healthy`, `failed`, `blocked`, or `unknown`. |
| `checks` | Non-empty array of `{name,status,status_code}`; status is `success`, `failure`, `pending`, or `unknown`, and `status_code` is an integer or `null`. |
| `provider` | Exact POC value `nomad`; Consul is a separate task. |
| `lifecycle_event` | One of `observation`, `workload_restart`, `service_restart`, `update`, `revert`, or `reconciliation`. |
| `source_refs` | Non-empty array of `{path,sha256}`. Every path must remain under `D:\LIVE15_NOMAD_POC`; every hash is a 64-character lowercase/uppercase SHA-256 digest. |
| `production` | Exact boolean `false`. |
| `control_performed` | Exact boolean `false` for this observation receipt. |

## Fail-closed rules

Reject the receipt when a required field is missing or malformed, the
timestamp is future-dated, the allocation/job relationship is not evidenced,
`provider` is not `nomad`, a check is missing its status or code, a source path
escapes `D:\LIVE15_NOMAD_POC`, a source hash is absent, or either safety flag is
not exactly `false`. `D:\LIVE15_QUANT`, holdout paths, request-derived
executables, PIDs, and direct HTTP responses are never authoritative receipt
sources. A receipt may report an unhealthy state, but it must not convert that
state into a restart, rollback, risk, sizing, execution, or trading decision.

## Verified isolated POC example

The machine-readable example is
`docs/deployment/examples/nomad-automation-receipt-v1.example.json`. Its
source hashes identify the durable soak log and checkpoint recorded by the
POC handoff. It preserves `production=false` and `control_performed=false` and
does not claim Production self-healing.

