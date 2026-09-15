# Application pipelines

## Responsibility

Compose ingestion, domain builds, lifecycle, and controlled publication.
**Canonical owner** of former `domains/*/build_*` orchestration (Phase 21) and
of **SCHEMA_SQL** for knowledge units (Phase 22 facade clear).

## Layout

```
application/
  conversation/   # summary, vector store, agentsview, graph helpers, …
  graph/          # relation candidates, merge layer, triple store, judge, query
  knowledge/      # KU pipeline, refresh, promote, SCHEMA migrate, …
  memory/         # memory store/graph, promotions, lifecycle, …
  ku.py           # product CLI: pk-ku
  sync.py         # product CLI: pk-sync
  run_pipeline.py # STEP_MODULES → application.* (legacy integrated path)
  …               # google / import / integrated system entrypoints
```

## Boundaries

- Orchestration and product CLIs live here.
- Shared LLM client: `core/llm.py`.
- **Must not** import `personal_knowledge.domains.*` (facade debt = 0; `pk-ku doctor`).
- Invokes evaluation gates when promoting (e.g. knowledge index).

## Entry points

```powershell
# Product (preferred)
pk-sync conversations --write
pk-ku workflow
pk-ku doctor --skip-ports
pk-ku inspect

# Modules
python -m personal_knowledge.application.sync conversations --write
python -m personal_knowledge.application.ku doctor --skip-ports
python -m personal_knowledge.application.conversation.summary --dry-run
python -m personal_knowledge.application.knowledge.refresh_knowledge_units --help

# Retired integrated batch (forensics only)
# PK_ALLOW_LEGACY_PIPELINE=1 python -m personal_knowledge.application.run_pipeline --legacy-integrated --dry-run
```

## I/O and privacy

Application pipelines read and write only through governed project paths. Raw
private bodies stay local; publication requires evidence, privacy, evaluation,
and explicit write gates. AgentsView live is always read-only.

## Tests

Dry-run, idempotency, failure and lifecycle tests under `tests/`.

## Ownership

Owner: application. Status: supported.
Last layout review: 2026-07-16 (Phase 22; SCHEMA here; application→domains = 0).
