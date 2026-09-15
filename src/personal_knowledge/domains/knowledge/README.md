# Knowledge domain

## Responsibility

**Compat package only.** Product ownership of knowledge-unit build, lifecycle,
and schema DDL lives under `application` / `evaluation`. Modules here are
re-export facades (`sys.modules[__name__] = canonical`).

## Canonical locations

| Kind | Package |
|------|---------|
| Build / refresh / promote / pipeline | `personal_knowledge.application.knowledge` |
| Product CLI | `personal_knowledge.application.ku` (`pk-ku`) |
| Canary / extraction / RAG eval | `personal_knowledge.evaluation.knowledge` |
| Schema DDL constant (`SCHEMA_SQL`) | `personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables` |

Do **not** add new logic under `domains/knowledge/`. New code imports
`application.*` / `evaluation.*` only.

## Boundaries

Compatibility re-exports only. Do not add schema, lifecycle, extraction,
evaluation, or publication logic here.

## Entry points

Product callers use `pk-ku`; code imports canonical `application.knowledge` or
`evaluation.knowledge` modules.

## I/O and privacy

Facades add no I/O and cannot bypass evidence, privacy, evaluation, or promotion
gates.

## Tests

Import compatibility and knowledge contracts are covered under `tests/`.

## Ownership

Owner: knowledge. Status: supported compatibility re-export only.
Last layout review: 2026-07-16 (Phase 22; facade debt clear; SCHEMA in application).
