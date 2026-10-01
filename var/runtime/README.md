# Runtime Zone

## Responsibility

Private runtime state: caches, watermark cursors, run journals, and ephemeral
service scratch output.

## Boundaries

Everything here is regenerable runtime state, not source code, documentation,
or source-of-truth data. Deleting the zone must never lose authoritative
knowledge.

## Entry points

Populated by governed project services and CLI runs (REST API, pk-ku, eval
harness). Consumers must treat contents as disposable caches.

## I/O and privacy

Privacy class: private-runtime. Files may embed private fragments observed at
run time and must never be committed; only this marker documentation is
versioned.

## Tests

Retention and artifact lineage contracts live under `tests/governance/` and
evaluation tests.

## Ownership

Owner: platform. Status: private-runtime.
