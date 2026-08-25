# LIVE15 agent knowledge map

This directory contains durable, agent-facing process guidance. It is intentionally small and
points to the authoritative project documents rather than copying their history.

- `debug-routing.md`: observe → reproduce → classify ownership → instrument → fix → regression test.
- `change-protocol.md`: bounded change, acceptance, validation, reporting, and stop rules.
- `../training_dataset.md`: dataset and as-of semantics.
- `../model_artifact_lineage.md`: external model artifact reconstruction contract.
- `../kalshi-sdk-v12-migration.md`: SDK ownership and upgrade policy.

The current runtime state is read from the repository's status files and Control Center; agents
must not invent a durable “current state” snapshot here.
