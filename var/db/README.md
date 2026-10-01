# Database Zone

## Responsibility

Private runtime databases: canonical SSOT SQLite stores, FTS indexes, and
migration ledgers.

## Boundaries

Databases here are runtime state, not source code or documentation. Schema is
owned by migration scripts; nothing in this zone is read as a source of truth
by external consumers.

## Entry points

Writers are governed project CLIs and services (pk-sync, pk-ku, REST API).
Direct manual edits are prohibited; access goes through the application layer.

## I/O and privacy

Privacy class: private-runtime. Contents contain private conversation bodies
and derived knowledge and must never leave the machine. Only schema-adjacent
marker documentation is versioned.

## Tests

Schema and migration contracts live under `tests/` and governance preflight
checks.

## Ownership

Owner: data-platform. Status: private-runtime.
