# NOMAD-RECORDER-LIFECYCLE-CUTOVER-PREP-002

STATUS = CUTOVER_COMPLETED / NOMAD_RUNTIME_OWNER
AUDITED_MAIN_SHA = `034a34c2fd53506db99e7c96b7c2b7d3815fee98`
RECORDER_LIFECYCLE_OWNER = NOMAD
CUTOVER = COMPLETED
CUTOVER_RESULT = PASS
WINSW_RECORDER = STOPPED_ROLLBACK_ONLY
ADDITIONAL_PREFLIGHT_REQUIRED = NO
RECORDER_RUNTIME = CANONICAL_LIVE15_PRODUCTION_RUNTIME
NEW_RECORDER_RUNTIME_REQUIRED = NO
HEALTH_BRIDGE_REQUIRED_FOR_CUTOVER = NO
CHECK_RESTART_USED = NO
CONSUL_USED = NO
GAP002_EXECUTED = NO
ARTIFACT_BINDING = PREPARED

This PR retains one declarative Nomad service job. The separately authorized
direct reversible cutover is complete: WinSW is stopped, Nomad is running, the
deployment is successful, and the single-writer check passed. WinSW remains
available only as the rollback owner.

## Candidate jobspec

`deploy/nomad/live15-recorder.nomad.hcl` uses the existing Nomad agent and the
established `CANONICAL_LIVE15_PRODUCTION_RUNTIME`. Host-specific paths and
release identities remain explicit operator inputs. Nomad owns placement,
task restart, deployment health, and native failed-update revert; the existing
Recorder entrypoint remains the owner of domain behavior.

The jobspec adds no wrapper, supervisor, custom health bridge, Consul dependency,
permission repair path, or second package/provenance mechanism.

## Completed direct reversible cutover

The human-authorized cutover followed this single-owner sequence:

1. Stop WinSW `LIVE15Recorder`.
2. Confirm the old PID is gone and no Recorder writer remains.
3. Start the reviewed `live15-recorder` Nomad job.
4. Observe the actual allocation, process, and current Recorder behavior.
5. If startup had failed, the rollback path was to stop Nomad, confirm its PID
   was gone, restore WinSW, and verify the single-writer state.
6. Retain the unchanged WinSW definition until bounded acceptance completes.

Observed result: Nomad deployment successful and single-writer cutover passed.
No separate identity, filesystem-access, permission, ACL, or speculative
preflight was required. This confirms the Recorder process/lifecycle cutover;
it does not claim that the existing Kalshi WS application-layer degraded state
is fixed. That state remains under separate diagnosis.
WinSW Recorder and Nomad Recorder must never run concurrently.

## Rollback

Rollback is the reverse single-owner sequence: stop `live15-recorder`, confirm
the allocation/task/process exit and absence of a second writer, then start the
retained `LIVE15Recorder` service and re-verify its PID and health state. Do not
delete the Nomad job or WinSW definition during the acceptance window.

The PR change itself remains declarative; the separately authorized runtime
cutover is recorded above. No Production cutover beyond that approved action is
implied.
