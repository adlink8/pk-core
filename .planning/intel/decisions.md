# Migrated Decisions

- Local-first SQLite remains the structured fact and lineage store.
- AgentView is read through a privacy-safe snapshot and canonical conversation layer.
- Vector stores are candidate retrieval indexes, not authoritative fact stores.
- AI outputs must pass schema, evidence, privacy and evaluation gates before promotion.
- Publication is versioned, atomic and reversible.
- Training-style RAG treats retrieval knowledge as non-parametric secondary training over personal history.
