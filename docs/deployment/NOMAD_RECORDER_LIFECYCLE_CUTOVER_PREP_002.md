# NOMAD-RECORDER-LIFECYCLE-CUTOVER-PREP-002

STATUS = READY_FOR_DIRECT_REVERSIBLE_CUTOVER / NO_PRODUCTION_CUTOVER
AUDITED_MAIN_SHA = `034a34c2fd53506db99e7c96b7c2b7d3815fee98`
RECORDER_LIFECYCLE_TO_NOMAD = READY_FOR_DIRECT_REVERSIBLE_CUTOVER
ADDITIONAL_PREFLIGHT_REQUIRED = NO
RECORDER_RUNTIME = CANONICAL_LIVE15_PRODUCTION_RUNTIME
NEW_RECORDER_RUNTIME_REQUIRED = NO
HEALTH_BRIDGE_REQUIRED_FOR_CUTOVER = NO
CHECK_RESTART_USED = NO
CONSUL_USED = NO
LIVE_RECORDER_MUTATED = NO
GAP002_EXECUTED = NO
ARTIFACT_BINDING = PREPARED

This PR prepares one declarative Nomad service job. It does not submit the job,
stop WinSW, start an allocation, read credentials, or mutate the Production
RecorderStore. The existing WinSW definition remains the owner until a separate
human-authorized cutover.

## Candidate jobspec

`deploy/nomad/live15-recorder.nomad.hcl` uses the existing Nomad agent and the
established `CANONICAL_LIVE15_PRODUCTION_RUNTIME`. Host-specific paths and
release identities remain explicit operator inputs. Nomad owns placement,
task restart, deployment health, and native failed-update revert; the existing
Recorder entrypoint remains the owner of domain behavior.

The jobspec adds no wrapper, supervisor, custom health bridge, Consul dependency,
permission repair path, or second package/provenance mechanism.

## Direct reversible cutover

After the release identity and variables have passed the existing release gate,
an operator may perform this sequence under the separate deployment approval:

1. Stop WinSW `LIVE15Recorder`.
2. Confirm the old PID is gone and no Recorder writer remains.
3. Start the reviewed `live15-recorder` Nomad job.
4. Observe the actual allocation, process, and current Recorder behavior.
5. If startup fails, stop the Nomad job, confirm its PID is gone, restore
   WinSW, and verify the single-writer state.
6. Retain the unchanged WinSW definition until bounded acceptance completes.

The observed cutover behavior is the acceptance signal; no separate identity,
filesystem-access, permission, ACL, or speculative preflight is required.
WinSW Recorder and Nomad Recorder must never run concurrently.

## Rollback

Rollback is the reverse single-owner sequence: stop `live15-recorder`, confirm
the allocation/task/process exit and absence of a second writer, then start the
retained `LIVE15Recorder` service and re-verify its PID and health state. Do not
delete the Nomad job or WinSW definition during the acceptance window.

No service, allocation, credential, runtime, Recorder, Nomad, or Production
state is mutated by this documentation/jobspec change.
