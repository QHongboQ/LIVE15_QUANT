# Settlement truth conflict adjudication

Task: `SETTLEMENT-TRUTH-CONFLICT-ADJUDICATION-001`

This is a read-only incident receipt. No Production row was edited, deleted, or relabeled.

## Authority

`PROJECT_CHARTER.md` defines finalized Kalshi `yes`/`no` as the sole terminal label truth.
`docs/continuous_recorder.md` requires an immutable settlement result and a fail-closed diagnostic
for a conflicting result. `expiration_value` is retained as source metadata, but it is not a second
terminal label.

## Existing durable truth and incoming official observations

All four rows were first accepted from the credentialless official endpoint
`https://external-api.kalshi.com/trade-api/v2/markets/{ticker}`. The later observations came from
that same endpoint and retained the exact ticker, event, 15-minute UTC window, target, result,
settlement timestamp, and binary settlement value. Kalshi had populated the previously absent
optional `expiration_value`.

| Asset | Ticker | Accepted result/value | Accepted at (UTC) | Accepted `expiration_value` | Later official `expiration_value` | Existing hash | Recorded incoming hash |
| --- | --- | --- | --- | --- | --- | --- | --- |
| BNB | `KXBNB15M-26AUG311430-30` | `no` / `0.0000` | `2026-08-31T18:37:29.176292+00:00` | absent | `692.73` | `d930088e2ceb413d41f519d1ae2388ae373ed22ab04a79b92b23ef33a0bc589e` | `07ab966afc9bd6cbe3c6ca7b046d3eeac773ad8da4a2e0255bcc1ae20c89bf2a` |
| SOL | `KXSOL15M-26AUG311430-30` | `yes` / `1.0000` | `2026-08-31T18:37:29.176927+00:00` | absent | `103.7825` | `cdf2ea0a45c31850d3103758a7b5fb5ab7524e817305c8cd46b9f9727f45ae56` | `77f1fde1bce2c1565ff508d6da51fb306d34feb1a6da278afa9fb299983ba169` |
| BTC | `KXBTC15M-26AUG311430-30` | `no` / `0.0000` | `2026-08-31T18:37:30.068798+00:00` | absent | `78904.27` | `0d18f0ea48756dc8f98c7ed8cfa6c6a81547b326e174f20582e5133363e4e808` | `9474b42b1467177ac32cb6b1453f7dd4b64811bfb1fddb4c98203bfe55a1deda` |
| ETH | `KXETH15M-26AUG311430-30` | `no` / `0.0000` | `2026-08-31T18:37:30.068117+00:00` | absent | `2471.16` | `050e118f609d23a98831ed941d8a5db511512fa9cb273020c3af7d051f8fd702` | `3d8eda0820e552396f19c206c3dead88fb0df3d363d4d0e22f483b701dbb818f` |

The later values above reproduce every recorded incoming hash exactly under the deployed
settlement fingerprint. This proves that `expiration_value` was the only hashed field that changed.

Each event currently contains exactly one official market. All four markets report
`exchange_index = 2`; no alternate market, shard, ticker reuse, target mapping, or series mapping
was found. The official Get Market schema documents both `expiration_value` and
`settlement_value_dollars`; there is no evidence of a schema change or a changed terminal result.

## Adjudication

Root cause: `SETTLEMENT_NORMALIZATION_BUG`.

The Recorder fingerprint treated optional post-finalization `expiration_value` metadata as if it
were immutable terminal settlement truth. The fail-closed storage behavior then correctly rejected
that overly broad fingerprint and Nomad correctly exhausted its bounded restart budget.

The bounded code correction keeps the first stored row and its original full content hash
unchanged. It compares repeat observations using the actual terminal truth tuple while continuing
to fail closed for identity, target, result, settlement timestamp, binary settlement value, or
stored-content-hash changes. Existing conflict diagnostics remain untouched.

No Production data remediation is required. Recorder restart is not safe until this correction is
reviewed, merged, packaged, deployed, and verified through the normal release boundary.
