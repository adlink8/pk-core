# Memory domain

## Responsibility

**Compat package only.** Long-term memory build/lifecycle and evaluation moved
to `application.memory` / `evaluation.memory` in Phase 21. Modules here are
re-export facades.

**Note:** memory / personal_events is **not** the knowledge SSOT. Knowledge SSOT
= KU tables + active vector collection (`pk-ku` / active pointer).

## Canonical locations

| Kind | Package |
|------|---------|
| Build / lifecycle / promotions | `personal_knowledge.application.memory` |
| Eval / compare / analyze / audit | `personal_knowledge.evaluation.memory` |

Do not add new logic under `domains/memory/`.

## Boundaries

Compatibility re-exports only. Memory experiments are not the authoritative KU
knowledge store and cannot silently enter product retrieval.

## Entry points

Use canonical `application.memory` and `evaluation.memory` modules; this package
exists only for legacy imports.

## I/O and privacy

Facades add no I/O. Memory data remains local, evidence-scoped, and separate
from the knowledge SSOT.

## Tests

Memory compatibility and lifecycle contracts are covered under `tests/`.

## Ownership

Owner: memory. Status: supported compatibility re-export only.
Last layout review: 2026-07-16 (Phase 22; facade debt clear).
