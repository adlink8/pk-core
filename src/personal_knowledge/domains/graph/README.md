# Graph domain

## Responsibility

**Compat package only.** Build/judge/query orchestration lives under
`application.graph`; eval under `evaluation.graph`. Modules here are re-export
facades.

`build_graph_relation_candidates_v2` was **deleted** (dead code).

## Canonical locations

| Kind | Package |
|------|---------|
| Build / judge / query / triple-store | `personal_knowledge.application.graph` |
| Eval judgments | `personal_knowledge.evaluation.graph` |

Do not add new logic under `domains/graph/`.

## Boundaries

Compatibility re-exports only. Graph build, query, judgment, and evaluation
logic belongs in canonical application/evaluation packages.

## Entry points

Use `application.graph` and `evaluation.graph`; this package supports legacy
imports only.

## I/O and privacy

Facades add no I/O. Graph consumers must retain evidence IDs and avoid exposing
private source bodies.

## Tests

Graph compatibility and evidence contracts are covered under `tests/`.

## Ownership

Owner: graph. Status: supported compatibility re-export only.
Last layout review: 2026-07-16 (Phase 22; facade debt clear).
