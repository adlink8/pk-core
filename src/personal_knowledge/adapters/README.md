# Source adapters
## Responsibility
Read-only adapters for AgentsView, Google and other immutable source snapshots.
## Boundaries
Never write live external sources; emit canonical source records only.
## Entry points
Use the adapter contracts and source-specific implementations in this package.
## I/O and privacy
Inputs are R4; outputs preserve source ID, timestamp, eligibility and run lineage.
## Tests
Adapters use synthetic fixtures and temporary databases only.
## Ownership
Owner: ingestion. Status: supported.

