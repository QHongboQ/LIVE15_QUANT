# FACTOR-001 — Leakage-safe symbolic factor factory

Status: **DEVELOPMENT INFRASTRUCTURE ONLY**

FACTOR-001 supplies the smallest useful foundation for testing structured symbolic factors. It
does not train or promote a model and it is not connected to Recorder, Paper, Shadow, Execution,
Hard Risk, or Production.

## Layered design

1. **Typed DSL** — JSON objects are parsed into `FactorExpression` nodes. The parser accepts only
   registered feature names, finite constants, and the fixed operator registry. Canonical JSON is
   stable across equivalent parses.
2. **Safe VM** — `SafeFactorVM` consumes an explicit `FactorContext` containing decision-time
   current values and history. It filters to as-of timestamps and invokes the existing
   `LeakageChecker` on every primitive provenance record. Missing values preserve an explicit
   reason; `SAFE_DIV` never fabricates a value for a zero denominator.
3. **Lineage and evaluation** — `FactorSpec`, `EvaluationPlan`, and `FactorEvaluationResult`
   carry Dataset v2 identity, experiment, formula, lookback, split, event, asset, day, and
   validation metrics. Evaluation is train/validation only and keeps the holdout opaque.
4. **Factor Zoo** — `FactorZoo` stores lightweight records and statuses. It is a manifest, not a
   model artifact store. Redundancy diagnostics report correlation and primitive overlap without
   automatically selecting a champion.

## Frozen contract and guardrails

- Dataset lineage is exactly `live15-dataset-v2-4bb4934bf328b6b024ff`.
- Holdout state is `UNREVEALED_FROZEN`; any `test`/holdout row is rejected before evaluation.
- The contract requires a 600-second purge/embargo and rejects factors whose lookback cannot fit.
- Target timestamps, when supplied, must be strictly after the decision and within the declared
  window; one event cannot appear in two splits.
- Complexity is bounded at depth 3, five operators, six primitives, and 300 seconds lookback.
- Search budget defaults to 100 candidates; no broad hyperparameter or factor sweep is included.
- The feature registry currently contributes 42 primitive definitions. No new microstructure,
  news, options, funding, or cross-exchange feature family is added here.

The deterministic demo contains six candidate definitions solely to exercise identity, budget,
and manifest code. It performs no Dataset v2 search, so there are no accepted or rejected live
factors. All demo records remain proposed/development metadata.

## AlphaGPT boundary

AlphaGPT is recorded only as a research reference. FACTOR-001 does not copy its source, import
it, or add a runtime dependency; the local DSL/VM remains deliberately smaller and auditable.
Any future use would require a separate evidence and licensing review.

## Tests

`tests/test_factor_factory.py` covers DSL serialization and identity, invalid expressions,
complexity, safe division, missing propagation, temporal no-lookahead, lookback, Dataset v2 and
holdout guards, LeakageChecker integration, deterministic VM behavior, bounded search, Factor Zoo
lineage, and redundancy diagnostics. FACTOR-002 is deferred until independent evidence materially
increases beyond the current six-day Dataset v2 development freeze.
