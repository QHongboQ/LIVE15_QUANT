# NOMAD-SERVICE-DISCOVERY-PROVIDER-POLICY-001: upstream research

**Scope.** Official HashiCorp Nomad/Consul documentation and the official
`hashicorp/nomad` v2.0.5 release record, researched 2026-08-29. This note is
limited to service-discovery provider policy for the isolated Windows POC and a
later non-production shadow validation. It is not a Production cutover decision.

## Decision-relevant finding

Keep the isolated POC on the explicit `provider = "nomad"` setting already in
its jobspec. Nomad native service discovery requires no additional
infrastructure, and its supported HTTP check is sufficient for the POC's
single-host `127.0.0.1:18080` fixture. This is a bounded POC/shadow choice, not
an assertion that Nomad native discovery is the Production target: HashiCorp
describes it as lightweight/basic and recommends Consul for Production service
discovery.

## Upstream facts and implications

| Topic | Official upstream fact | Isolated POC / later shadow implication |
| --- | --- | --- |
| Provider choice | A `service` block registers with either the Nomad or Consul provider. `consul` is the default, so a jobspec must explicitly set `provider = "nomad"` to use native discovery. [Nomad service-discovery configuration](https://developer.hashicorp.com/nomad/docs/job-declare/service-discovery#declare-the-service-provider) | Retain the explicit Nomad provider; otherwise a future edit could implicitly require Consul. |
| Infrastructure boundary | Consul discovery requires access to a Consul cluster; Nomad discovery requires no additional infrastructure. [Nomad service-discovery configuration](https://developer.hashicorp.com/nomad/docs/job-declare/service-discovery) | Do not install/configure Consul solely to validate this single-host POC. A Consul shadow is a separate scoped task with its own agent, ACL, and catalog evidence. |
| POC health semantics | Checks may be registered with either provider. The Nomad provider supports `http` and `tcp` checks; its initial check status is `pending` until Nomad produces a result, and Nomad exposes check state in `nomad alloc status`. [Service block reference](https://developer.hashicorp.com/nomad/docs/job-specification/service), [check block reference](https://developer.hashicorp.com/nomad/docs/job-specification/check#parameters), [check-status CLI behavior](https://developer.hashicorp.com/nomad/docs/job-specification/check#check-status-on-cli) | The existing HTTP health check must be observed as a Nomad check result (`success`/failure), not inferred from process liveness or an HTTP request alone. |
| Deployment health | The service `on_update` policy controls how checks are evaluated during deployments; `require_healthy` requires a healthy check. Nomad manages service/check registration and deregistration across task lifecycle. [Service block reference](https://developer.hashicorp.com/nomad/docs/job-specification/service#parameters), [service lifecycle](https://developer.hashicorp.com/nomad/docs/job-specification/service#lifecycle) | The planned native update/revert exercise can use the Nomad-provider HTTP check as the deployment-health signal. No LIVE15 deployment controller is appropriate. |
| Provider consistency | All services in one task group must use the same provider value. [Service block reference](https://developer.hashicorp.com/nomad/docs/job-specification/service#parameters) | Do not mix Nomad and Consul service blocks within the fixture task group; any future Consul shadow job should be a deliberate, internally consistent variant. |
| Consul operational requirements | Nomad's Consul integration expects each Nomad client to have a reachable local Consul agent; Nomad does not provide Consul itself. Consul service mesh using network namespaces is supported only on Linux. [Consul integration assumptions](https://developer.hashicorp.com/nomad/docs/networking/consul#assumptions), [service-mesh limitation](https://developer.hashicorp.com/nomad/docs/networking/consul/service-mesh) | A Windows POC must not treat `provider = "consul"` as a drop-in service-mesh path. No Consul mesh/sidecar validation belongs in the current Windows POC. |
| Production boundary | HashiCorp describes Nomad native discovery as lightweight/basic and recommends Consul for Production workloads with broader discovery/networking needs. [Service discovery overview](https://developer.hashicorp.com/nomad/docs/networking/service-discovery) | A later shadow validation should document actual discovery consumers, scale, DNS/mTLS/KV/telemetry needs, and security policy before selecting a Production provider. This research does not select one. |

## Version scope

The HashiCorp service-discovery guide identifies the active documentation line
as Nomad **v2.0.x**. The official v2.0.5 release notes include a native service
check identifier change, but do not announce a provider-model or Windows
service-discovery compatibility change. The conclusions above therefore apply
to the verified v2.0.5 POC binary, subject to runtime evidence retained by the
POC. [v2.0.x service-discovery guide](https://developer.hashicorp.com/nomad/docs/job-declare/service-discovery), [official v2.0.5 release](https://github.com/hashicorp/nomad/releases/tag/v2.0.5).

## Shadow-validation acceptance boundary

For a future non-production shadow, preserve the explicit provider in every
jobspec and record: provider, service/catalog query result, Nomad check status,
allocation ID, advertised address/port, and the consumer's successful lookup.
If the shadow instead evaluates Consul, validate the independent Consul agent
and its ACL/catalog path as part of that new task. Do not infer either provider's
behavior from the other, and do not introduce a custom registry, health agent,
or service-mesh substitute.

## Official sources

- [Nomad: Configure service discovery](https://developer.hashicorp.com/nomad/docs/job-declare/service-discovery)
- [Nomad: Service block reference](https://developer.hashicorp.com/nomad/docs/job-specification/service)
- [Nomad: Check block reference](https://developer.hashicorp.com/nomad/docs/job-specification/check)
- [Nomad: Service discovery overview](https://developer.hashicorp.com/nomad/docs/networking/service-discovery)
- [Nomad: Consul integration](https://developer.hashicorp.com/nomad/docs/networking/consul)
- [Nomad: Consul service-mesh integration](https://developer.hashicorp.com/nomad/docs/networking/consul/service-mesh)
- [HashiCorp Nomad v2.0.5 release](https://github.com/hashicorp/nomad/releases/tag/v2.0.5)
