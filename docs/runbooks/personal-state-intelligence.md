# Personal state intelligence: read-only operations

Phase 25 exposes snapshot-bound personal goals, constraints, observations and
change explanations through one shared backend. It is an analysis view, not a
knowledge or serving authority. Responses are metadata-only by default: state
values are represented by checksums and evidence by typed refs/statuses.

## Safe commands

```powershell
python -m personal_knowledge.intelligence.cli state current --json
python -m personal_knowledge.intelligence.cli state history --limit 50 --json
python -m personal_knowledge.intelligence.cli changes recent --window-start 2026-01-01T00:00:00Z --json
python -m personal_knowledge.intelligence.cli state explain --assertion-kind goal --subject user --domain work --scope personal --predicate complete_target --json
python -m personal_knowledge.intelligence.cli acceptance --dry-run --metadata-only --json
```

Use `--snapshot-id` and/or `--run-id` to pin a replay. A run from another
snapshot fails with `cross_snapshot_run`. Limits outside 1–100 fail with
`invalid_limit`; acceptance is deliberately narrower (1–25).

## Acceptance contract

`acceptance --dry-run --metadata-only` is the only Phase 25 live acceptance
path. It:

- resolves one active snapshot and, when available, the latest committed
  personal-state run;
- computes a bounded metadata-only candidate replay without publishing it;
- emits a deterministic run-plan checksum and aggregate reason codes;
- fingerprints serving authority, KU, lifecycle, watermark and analysis tables
  before and after;
- reports `mutations=0`, `private_bodies=0`, `network_calls=0` and
  `paid_calls=0` only when the fingerprints are identical;
- reads Phase 24 checkpoint and strict statuses and reports
  `release_blocked` while any genuine-human or quality gate remains open.

The command never imports labels, finalizes a review, registers/applies a
lifecycle manifest, activates/rolls back a serving snapshot, advances a
watermark, calls a network or paid provider, or edits a checkpoint file.

## Failure semantics

An absent committed analysis run produces a bounded abstention reason rather
than inventing state. Missing/tampered snapshots or manifests, cross-snapshot
runs, ineligible evidence and private/secret payloads fail closed. A successful
metadata-only acceptance does **not** close Phase 24, authorize a live analysis
write, or change the serving authority.
