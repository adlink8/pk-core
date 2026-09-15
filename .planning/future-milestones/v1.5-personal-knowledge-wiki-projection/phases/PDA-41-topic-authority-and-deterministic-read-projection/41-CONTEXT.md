---
phase: 41
name: Topic Authority and Deterministic Read Projection
status: preplanned_not_active
requirements: [WIKI-01]
depends_on: [v1.4 Phase 36, v1.4 Phase 37]
---

# Phase 41 Context

## Goal

Build the smallest deterministic, read-only Topic Projection contract for Project, Goal and Decision topics. It must bind every output to the actual existing authorities rather than inventing an entity or Wiki fact store.

## Decisions

| ID | Decision |
|---|---|
| W-41-01 | Topic identity is explainable: `project:{scope}`, `goal:{domain}:{scope}:{predicate}`, `decision:{recommendation_id}`; no semantic matching creates P0 identity. |
| W-41-02 | Envelopes use `personal_wiki_projection_v1` and only `topic.list`, `topic.get`, `topic.backlinks`; they bind serving/personal/external/decision snapshot references, freshness, partial and limitations. |
| W-41-03 | A Wiki projection may store version/dependency metadata only. It cannot become a personal-fact, external-fact or evidence authority. |
| W-41-04 | GET/read-only failures return typed safe recovery. Missing/bad keys, stale bindings and sealed data never fall back to guessed content. |

## Boundaries

- Reuse executed v1.4 Projection/evidence contracts; do not bypass them with direct browser/database access.
- Do not modify `docs/wiki/` or introduce LLM narrative, user notes, topic editor or vector/Chroma writes.
- If v1.4 contracts differ from this preplan after execution, update Phase 41 research before implementation.

