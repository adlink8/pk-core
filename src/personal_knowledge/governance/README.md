# Governance

## Responsibility

Repository inventory, policy gates, migration previews, recovery manifests, and
the CI preflight used to enforce project structure, privacy, and lifecycle rules.

## Boundaries

- Governance code validates metadata and repository state; it does not define
  knowledge, retrieval, or product-domain behavior.
- Migration executors require an approved exact manifest and remain fail-closed
  on dirty sources, private-path expansion, or checksum mismatch.
- Preview, audit, and preflight commands must not move, delete, promote, or
  rewrite private data.

## Entry points

- `python -m personal_knowledge.governance.preflight --ci`
- `source_manifest.py` and `data_disposition.py` for deterministic inventory and
  disposition planning
- `apply_source_migration.py` and `apply_data_migration.py` only for separately
  approved manifest-driven operations

## I/O and privacy

Inventory and reporting are metadata-only for private zones. Generated reports
belong under governed `var/runtime/governance` or `var/reports` locations and
must not include private message bodies, credentials, or secrets.

## Tests

Governance contracts live under `tests/governance/`, with migration and recovery
coverage supplemented by related integration tests.

## Ownership

Owner: engineering-governance. Status: supported.
