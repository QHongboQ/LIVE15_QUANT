# GAP002 Phase-3 frozen baseline

`STATUS = PHASE3_FREEZE`

`PHASE3_COMPLETE = YES`

`GAP002_EXECUTED = NO`

## Frozen source and dependency identity

- Protected source baseline: `fe1e50b9c1381ab7b753b0c6dbd1001512653f37` (`origin/main` at task start).
- Pinned dependency: `kalshi-sdk==12.0.0`; `requirements.lock` blob
  `f643dfedd40e2ab7f3d3ca6147cee9bee092dbc8`.
- Critical-path blobs: `websocket.py` `81eede3f18161f1fecdb010afcbf7ab448261a7f`,
  `production_recorder_host.py` `28f5e7616f4c582db378cc5ff185ca979e221b21`,
  `recorder_provider.py` `3f77154887fd56b6f4a1644130f9794231fedc07`,
  `reliability.py` `025397e6ce527c35be12118eaca6c9c59fdca880`,
  `recorder_consumer.py` `61842d87784accbda5f5516384be104e35554bb9`,
  `native_recorder.py` `137823c8c08b45c30d0b72241a8a11398994caef`,
  `storage.py` `68c4c96c249d1be8cf7503a467b997a2d0494a5f`, and
  `gaps.py` `24338082b274daca5bb046f05b6a5a20c568928c`.
- Configuration/identity blobs: `config.py` `098416532216d102be6a275b6871efe339495fb2`,
  `client.py` `ead83b6ebc0928c54da80cbe8b9e4658541d311b`,
  `live15-recorder.xml` `fad279bde0f9e2ef899c706c268ce36e433012e1`,
  `runtime-ownership.json` `8e051c2f28a2313238998144b6bf412e7ed429e8`.

## Frozen contracts

The authoritative path is `kalshi-sdk` typed transport/subscription and reconnect,
LIVE15 Gateway/host adaptation, reliability coordinator/provider, Recorder consumer and
domain writer, then `RecorderStore`/`KalshiNativeRecorder` health and gap evidence.
The frozen configuration references (without secret contents) are the Kalshi API-key ID
path, private-key path, `recorder_data_path`, and `recorder_health_path`. The Recorder
identity is `WinSW:LIVE15Recorder`, service `LIVE15Recorder`, `LocalSystem`, with database
`data/live15.sqlite3` and health `data/health.json` relative to the configured runtime root.

## GAP002 acceptance contract

Using the existing provider/consumer and protocol contracts, a future PASS must prove that:

1. a sequence defect, discontinuity, or reconnect invalidates every affected authoritative
   book and opens the corresponding typed `kalshi_ws` gap;
2. deltas cannot restore authority; complete fresh authoritative snapshots are required;
3. synchronized consumption resumes only after valid recovery;
4. the corresponding gap is durably closed and no unresolved active `kalshi_ws` gap remains
   for the recovered path.

The bounded observation rule is one bounded recovery episode on the frozen critical path:
capture a pre-episode healthy checkpoint, observe the induced disconnect/reconnect and
recovery through synchronized consumption, then retain the terminal health/checkpoint and
gap state. This is sufficient to prove the predicates without service death or a generic
long soak; any live execution must choose and record concrete timestamps before Phase 4A.

## Required Phase-4A receipt set

Retain only: frozen source/dependency hashes; sanitized runtime/config path proof; Recorder
service identity; episode start/end timestamps; pre/post health and synchronization snapshots;
typed gap OPEN/RECOVERED evidence; exact existing test/runtime command and result; and a
sanitized error/result if the episode fails. Never retain secret contents.

## Read-only runtime preflight

`sc.exe query LIVE15Recorder` reported `RUNNING`; `sc.exe qc` reported WinSW binary
`D:\LIVE15_QUANT\.local-tools\winsw\LIVE15Recorder.exe`, automatic start, and
`SERVICE_START_NAME = LocalSystem`. The configured `data/health.json`,
`data/live15.sqlite3`, and `.venv\Scripts\python.exe` paths exist. A read-only health
snapshot was available but currently reports `status = degraded`, stale/reconnecting
Kalshi WS workers, and no fatal error; this is recorded as current runtime shape only,
not as GAP002 acceptance. No service, ACL, credential, Nomad, or WinSW mutation occurred.

`OPERATOR_ACTION_REQUIRED = NO` for this baseline: required identity/path facts were
obtainable without elevation. Phase 4A remains separately gated and was not executed.

## Authority and isolation

This receipt is the single detailed Phase-3 baseline inventory. The roadmap owns phase
progression; `docs/project-brain/constraints/execution/parallel-development.md` owns the
frozen-surface isolation rule. The dependency classifications remain solely in
`docs/project-brain/dependencies/gap002-closure.md`.
