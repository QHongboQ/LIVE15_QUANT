# LIVE15 debug routing

Use this sequence for a bug, regression, performance issue, or unexpected runtime state:

```text
OBSERVE → REPRODUCE → CLASSIFY OWNERSHIP → INSTRUMENT → FIX → REGRESSION TEST
```

## 1. Observe and reproduce

Capture the exact symptom and build the smallest runnable pass/fail signal at the highest useful
seam: a regression test, bounded CLI/HTTP probe, replay fixture, or isolated harness. Confirm it
is the reported failure, not a nearby error. If no reliable signal can be built, stop and report
the missing evidence instead of guessing.

## 2. Classify ownership

Distinguish production, test, environment, data-quality, configuration, third-party, and LIVE15
defects. For SDK/Kalshi infrastructure, identify the exact package/version/component first. Read
the pinned SDK source, examples, tests, release notes, and official docs before reimplementing
transport behavior. Prefer an SDK upgrade or a thin adapter correction; use a controlled fork
only after explicit review of an unresolved upstream defect.

For LIVE15-owned behavior, identify the smallest failing boundary and preserve downstream
contracts. Do not broaden a local failure into a transport, storage, model, or risk rewrite.

## 3. Instrument one hypothesis at a time

Rank 3–5 falsifiable hypotheses. Each probe must predict what would change if that hypothesis
were true. Add temporary, uniquely tagged diagnostics at the boundary that distinguishes them;
remove or bound diagnostic output before completion. Never log credentials, keys, signatures, or
raw sensitive payloads.

## 4. Fix and verify

Write the regression test at the seam that reproduces the actual failure, make the smallest safe
fix, and run targeted tests before broader relevant checks. Preserve fail-closed behavior in
Recorder, settlement, dataset, Risk, and execution paths. Document root cause and evidence.
