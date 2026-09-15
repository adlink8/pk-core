---
milestone: v1.5 Personal Knowledge Wiki Projection
status: preplanned_not_active
source: .planning/future-milestones/v1.5-personal-knowledge-wiki-projection/SPEC.md
---

# v1.5 Requirements

## P0 requirements

### WIKI-01 — Topic authority and read projection contract

Provide deterministic `topic.list`, `topic.get` and `topic.backlinks` read projections for P0 Project, Goal and Decision keys. Every envelope is snapshot-bound, freshness-aware, partial-capable, checksum/evidence-linked and reconstructable from upstream authorities. The projection has no fact-write, provider, external-action or retrieval-index write path.

### WIKI-02 — Navigable topic pages with epistemic truthfulness

Provide a directory and Project/Goal/Decision pages that clearly separate current facts, observations, inferences, recommendations, historical/conflict material, external context, decision feedback, limitations and evidence drill-down. `/evidence` stays compatible; the UI does not duplicate Decision Workspace confirmation or Action/Outcome recording.

### WIKI-03 — Materialization, dependency invalidation and safe read routing

Store only versioned projection/dependency metadata required to materialize pages. On upstream snapshot/dependency change, affected pages become stale or partial and are deterministically rebuilt. Wiki text is never written to Active KU/Chroma or treated as independent evidence. Wiki-first is an optimization with structured-authority/KU/raw-evidence fallback.

### WIKI-04 — Daily-use proof and expansion decision

Prove P0 pages are usable under privacy sealing, partial/offline authority, keyboard/mobile layouts and long Chinese/ID content. Use a defined small high-value topic cohort and evidence to decide whether Skill, Career, External Topic, user metadata, LLM narrative or broader entity types should remain deferred or earn a later candidate phase.

## Invariants

- `docs/wiki/` is developer/operator documentation and never becomes the personal Wiki authority.
- The page is a projection/materialized view, not an SSOT or an editable factual record.
- Personal and External facts remain separate; Observation/Inference/Forecast/Recommendation do not masquerade as Fact.
- Backlinks only use explicit explainable joins in P0; no vector similarity or LLM guess is shown as a fact relationship.
- Any unavailable/stale/partial input is shown honestly; no stale page is advertised as current.

## Deferred requirements

| Item | Why deferred |
|---|---|
| Skill/Career/External Topic pages | Stable aliases, disambiguation and evidence thresholds are not yet proven. |
| LLM narrative | Requires separate Candidate → Eval → Publish → rollback contract. |
| User notes/free editor | Requires a distinct user-note authority and deletion/privacy lifecycle. |
| Broad auto-page generation | Would create low-value pages and identity drift. |

