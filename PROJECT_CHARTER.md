# LIVE15 project charter

## Strategic objective

Build a local, Kalshi-native research system for ten fixed 15-minute series.
It must preserve auditable data provenance, decision-time correctness, and
reproducible research before any promotion beyond paper-only behaviour.

## Success definition

LIVE15 can produce reproducible, chronologically validated research artifacts
from authoritative source evidence; demonstrate fresh forward challenger
evidence; and keep all live decision inputs, runtime health, and execution
boundaries fail-closed.

## Permanent invariants

- Kalshi finalized `yes`/`no` settlement is the sole terminal label truth.
- Predictive feeds are inputs, never settlement labels.
- Inputs must satisfy strict as-of, freshness, synchronization, and gap rules.
- Dataset v1 final test and Dataset v2 frozen holdout are immutable experiment
  artifacts, not current research-history stores.
- Research coverage is defined by the Research Data Authority's H0/H1/H2 source
  registry and `ResearchUniverseSnapshot`.
- Recorder raw truth, archive quarantine, sequence integrity, and settlement
  lineage fail closed.
- Hard Risk is independent; Production writes remain disabled unless a human
  explicitly authorizes them.

## Explicit non-goals

- Real-money trading, silent risk-cap changes, and synthetic labels.
- Treating browser/reference feeds as authoritative venue or settlement data.
- Reading frozen-holdout payloads to tune a factor/model.
- Allowing agent convenience to override source, archive, risk, or deployment
  authority.

## Authority boundaries

| Boundary | Agent may do | Human approval required |
| --- | --- | --- |
| Strategy | Inspect, propose, document alternatives | Change objective, invariant, or ADR decision |
| Code/research | Implement reviewed bounded tasks with tests | Promotion, holdout access, or model-policy change |
| Runtime | Diagnose and prepare package changes | Service deployment/restart and Production configuration |
| Data/execution | Read authorized evidence | Production writes, retention destruction, labels, Hard Risk |

When a request conflicts with this charter or an accepted ADR, stop and surface
the decision instead of changing direction.
