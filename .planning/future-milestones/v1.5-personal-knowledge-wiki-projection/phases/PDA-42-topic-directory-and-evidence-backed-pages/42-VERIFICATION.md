---
phase: 42-topic-directory-and-evidence-backed-pages
status: preplanned_not_active
verification_mode: future_execution
requirements:
  WIKI-02: planned
depends_on_phase_verification: 41
---

# Phase 42 — Verification Plan

## Completion evidence

| Capability | Required proof |
|---|---|
| Navigation | `/knowledge` directory and typed Project/Goal/Decision routes work from server-issued opaque topic IDs; `/evidence` remains compatible. |
| Epistemic truth | Fact, Observation, Inference, Forecast, Recommendation, Historical/Conflict and External sections are separately labeled in DOM and screen-reader text. |
| Evidence/backlinks | Drawer uses stable resolver references only; explicit backlinks show join basis/provenance and distinguish empty from partial/unavailable. |
| Cockpit separation | Wiki has navigation handoffs only—no prepare, preview, confirm, action/outcome, suppress, provider or POST path. |
| Usability | 320px, keyboard/Escape/focus return, long Chinese/opaque ID and partial states remain usable. |

## Required tests

1. Zod/route component tests for all P0 topic types, stale/partial/unavailable/sealed fixtures and long content.
2. Evidence Drawer tests for opaque references, focus behavior and no raw personal/evidence material in URL/storage/console.
3. Backlink tests for explicit join allowlist and zero vector/LLM/semantic relation creation.
4. Static forbidden-import/call scan of Wiki source directories for direct `fetch`, search/semantic hooks, provider, POST, orchestration/proactive and confirmation/session paths.

## Pass / block

WIKI-02 passes only when all topic pages are truthful read-only projections. Any direct authority join, semantic backlink, write affordance, raw evidence leak or a Decision Workspace duplication blocks Phase 43.

