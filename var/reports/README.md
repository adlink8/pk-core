# Reports

## Responsibility

Private generated evaluation, analysis, and governance reports.

## Boundaries

Reports are generated artifacts, not source-of-truth data or source code. Each
current artifact should identify its producer, inputs, run/version, and status.

## Entry points

Evaluation and governance commands write through governed project paths under
`var/reports`; consumers should use versioned artifacts or validated latest
pointers.

## I/O and privacy

Privacy class: private-generated. Reports must contain metrics, stable IDs, and
redacted evidence only; never commit private bodies, credentials, or secrets.

## Tests

Artifact lineage, privacy, and retention contracts live under
`tests/governance/` and evaluation tests.

## Ownership

Owner: evaluation. Status: private-generated.
