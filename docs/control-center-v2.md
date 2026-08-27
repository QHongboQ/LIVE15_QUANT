# Control Center v2

The localhost Control Center is an administrative read-only surface over the Recorder's
typed projections. It does not train models, mutate Dataset v2, expose holdout rows, or
provide trading/credential endpoints. The only mutating routes remain the existing
localhost-bound Recorder lifecycle controls.

## Information architecture

The sidebar is grouped into Overview (Dashboard, Markets), Data (Data Pipeline, Training
Truth, Archive, Storage), and Operations (Operations, Warnings / Errors, System / Health).
Routes are hash-based and stable: `#/dashboard` (also `#/`), `#/markets`,
`#/markets/<asset>`, `#/data`, `#/training`, `#/archive`, `#/storage`, `#/operations`,
`#/events`, and `#/system`.

## Training truth

`GET /api/training` returns four separate records:

* `raw_finalized_pool` is official finalized settlement truth from the Recorder store.
* `current_trainable` is the mutable, checkpointed materializer projection. Missing or
  unreadable state is `UNKNOWN` with a reason code and `null` counts, never synthetic zero.
* `latest_completed_dataset` is the newest immutable completed build. It can be `STALE`
  when new raw settlements arrived after its source snapshot; stale does not mean the
  events were rejected.
* `frozen_experiment_facts` contains only explicitly persisted experiment records. An empty
  list is reported as `N/A`; the UI does not infer experiment state from row counts.

The legacy `/api/coverage` route remains for compatibility. New UI views use the typed
`/api/training` projection so current trainability cannot be confused with a completed
snapshot.

## Operational projections

`GET /api/data`, `/api/archive`, `/api/storage`, and `/api/operations` are bounded, typed,
read-only projections. Archive and storage pages expose purge eligibility as dry-run
observations only. Quarantined and failed archive chunks remain visible and cannot be
crossed by a UI action. Missing, stale, unsupported, and unavailable values remain explicit.

All API reads use bounded SQLite read-only connections and the Recorder heartbeat allowlist;
secrets and credential material are never serialized to the browser.
