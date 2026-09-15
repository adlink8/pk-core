---
milestone: v1.5 Personal Knowledge Wiki Projection
status: preplanned_not_active
activation_gate: v1.4 complete plus explicit authorization
---

# v1.5 Roadmap

## Phase 41 — Topic Authority and Deterministic Read Projection

**Goal:** Define P0 topic identity, envelope, dependency records and read-only projection without introducing another fact store.

**Requirements:** WIKI-01

**Completion evidence:** P0 keys resolve deterministically; every response binds source snapshots/freshness/evidence; bad keys/bindings fail closed; no write/provider/index side effect.

## Phase 42 — Topic Directory and Evidence-backed Topic Pages

**Goal:** Let users browse Project, Goal and Decision topics as truthful context pages without duplicating the Cockpit decision workflow.

**Requirements:** WIKI-02

**Completion evidence:** Directory/page navigation and evidence drawer work for all P0 types; claim types/history/external context are visually separated; no page presents a confirm/action control.

## Phase 43 — Materialization, Invalidation and Wiki-first Fallback

**Goal:** Make high-value topic pages a safely rebuildable materialized view with explicit stale behavior and no self-retrieval loop.

**Requirements:** WIKI-03

**Completion evidence:** upstream changes mark only affected topics stale; rebuild produces a new bound projection; Wiki output cannot enter KU/Chroma; read selection falls back safely when Wiki is stale/partial/missing.

## Phase 44 — P0 Hardening, Cohort UAT and Expansion Decision

**Goal:** Demonstrate a small useful P0 cohort in daily browsing, then make a documented, evidence-based decision on the next domain rather than expanding by default.

**Requirements:** WIKI-04

**Completion evidence:** responsive/privacy/degraded UAT pass on the cohort; topic usefulness and staleness behavior are measured; future Skill/Career/LLM/editor scope has a recorded promote/defer decision.

## Requirement coverage

| Requirement | Phase |
|---|---|
| WIKI-01 | 41 |
| WIKI-02 | 42 |
| WIKI-03 | 43 |
| WIKI-04 | 44 |

