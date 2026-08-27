# ADR 0002: Research data authority and immutable datasets

- Status: Accepted (existing)
- Sources: `docs/research_data_authority.md`, `docs/training_dataset.md`

## Decision

Research coverage is derived from the typed H0/H1/H2 registry and
`ResearchUniverseSnapshot`, with deterministic precedence, deduplication, and
conflict quarantine. Dataset v1/v2 are immutable reproduction artifacts. Frozen
holdout content is never used as development history.

## Consequences

Feature freshness, development-history recency, and forward-OOS freshness remain
separate types. No factor/model entrypoint silently converts a Dataset partition
into current research selection.
