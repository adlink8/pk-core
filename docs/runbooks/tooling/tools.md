# Developer and migration tools
## Responsibility
One-off probes, audits, migrations and compatibility diagnostics.
## Boundaries
Production modules cannot import this package; tools must not become hidden product APIs.
## Entry points
Run a named script explicitly after reading its help and safety behavior.
## I/O and privacy
Default to read-only/dry-run; private output stays ignored and local.
## Tests
Critical migrations require focused fixtures or a documented verification command.
## Ownership
Owner: platform. Status: migration-tools. Review/expiry checkpoint: 2026-10-01.

