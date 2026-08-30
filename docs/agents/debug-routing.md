# LIVE15 debug routing

Use the least-cost route for a bug, regression, performance issue, or unexpected runtime state:

```text
reuse existing capability / selected replacement → bounded execution → observe
  → diagnose only a concrete remaining failure
  → genuine authoritative defect: reproduce → classify → instrument → fix → regression test
```

Approved replacement work does not require speculative reproduction, hypotheses, probes, or
preflights against the retiring layer. Use the full diagnosis route when the authoritative LIVE15
behavior is genuinely defective or no selected owner can satisfy the requirement.

## 1. Select owner and observe

Reuse the project capability that already owns the behavior. If an upstream replacement is approved,
run its smallest reversible path and observe the actual result. Confirm the concrete symptom before
spending effort on diagnosis.

## 2. Reproduce and classify a remaining defect

Capture the exact symptom and build the smallest runnable pass/fail signal at the highest useful
seam: a regression test, bounded CLI/HTTP probe, replay fixture, or isolated harness. Confirm it
is the reported failure, not a nearby error. If no reliable signal can be built, stop and report
the missing evidence instead of guessing.

Classify the remaining failure as production, test, environment, data-quality, configuration,
third-party, or LIVE15-owned. For SDK/Kalshi infrastructure, identify the exact package/version/
component first. Read the pinned SDK source, examples, tests, release notes, and official docs
before a new local transport implementation. Prefer an SDK upgrade or thin adapter correction.

## 3. Instrument and repair

For a genuine LIVE15 defect, identify the smallest failing boundary and preserve downstream
contracts. Use a failing regression test when a correct seam exists; rank falsifiable hypotheses
and add only the instrumentation needed to distinguish them. Run targeted checks before broader
relevant checks. Preserve fail-closed behavior in Recorder, settlement, dataset, Risk, and
execution paths. Document root cause and evidence.
