# SHADOW-REC-001: non-Production Recorder/service-discovery validation contract

**Status:** contract-only; no runtime execution authorized by this document.

**Scope.** This contract defines the evidence required before a later,
separately authorized non-Production Shadow Recorder/service-discovery test.
It does not select a Production provider, install Consul, change the isolated
Nomad POC, or replace the SDK-authoritative Recorder.

## Boundary and upstream baseline

- The existing Windows POC remains an explicit Nomad-provider jobspec
  (`provider = "nomad"`) with its loopback HTTP check. The provider-policy
  research document records the upstream rationale and links to the official
  Nomad service-discovery and check references.
- Nomad owns service registration, health evaluation, allocation lifecycle,
  restart, update and revert behavior. No LIVE15 supervisor, registry,
  health-agent or service lifecycle substitute is permitted.
- A Consul-provider shadow is a different task. It requires its own reachable
  Consul agent, ACL/catalog evidence and internally consistent jobspec; it is
  not an implicit fallback for this contract.

## Required validation evidence

For one fixed, non-Production jobspec/configuration revision, retain a
timestamped, hash-addressed receipt containing all of the following:

1. The exact jobspec/configuration revision and Nomad version.
2. Allocation ID, task group, service name, explicit provider, advertised
   address and port.
3. The native Nomad service/catalog query result showing the registered
   endpoint. Use the official version-matched CLI/API path; do not parse a
   custom registry.
4. The native Nomad allocation check result, including the transition to
   `success` and any observed failure/`pending` state. A direct HTTP response
   or process liveness alone is not health evidence.
5. A bounded consumer lookup against the recorded endpoint that succeeds only
   while the native check is healthy.
6. A negative observation proving that an unhealthy, missing or deregistered
   endpoint is not accepted as a usable discovery result.
7. Receipt hashes and the exact POC/shadow paths containing logs and query
   output. Secrets, credentials and Production identifiers must be absent.

## Acceptance and fail-closed rules

The task passes only when every required item is present, internally
consistent, and tied to the same revision and allocation. Missing, stale,
unsynchronized, gapped, ambiguous-provider or failed-check evidence is a
fail-closed result; it must not be repaired by forward-filling, synthetic
health, or a second discovery mechanism.

The contract is invalidated by any Production write, holdout access, trading
write, service-mesh/Consul installation outside its separately authorized
scope, custom supervisor, or automatic restart/write path.

## Entry and exit gates

**Entry:** a separately approved non-Production task names the exact host,
jobspec/configuration revision, provider, service and consumer, and confirms
that no Production process or data is in scope.

**Exit:** the Maker records the immutable receipt and cleanup result; an
Independent Checker verifies the evidence, hashes, provider consistency and
protected boundaries. A Checker PASS does not authorize merge, deployment or
Production cutover. Any new elevation or human decision is recorded as a
task-local `HUMAN_GATE/BLOCKED` while other safe Project Brain work continues.

## Upstream references

See `NOMAD_SERVICE_DISCOVERY_PROVIDER_POLICY_001_UPSTREAM_RESEARCH.md` for the
version-pinned official HashiCorp documentation and v2.0.5 release links. This
contract deliberately reuses those upstream semantics rather than defining a
LIVE15 discovery protocol.
