---
milestone: v1.5
name: Personal Knowledge Wiki Projection
status: preplanned_not_active
depends_on_milestone: v1.4 Decision Cockpit UI
activation_policy: v1.4 completed and independently accepted; then explicit user authorization and normal GSD milestone switch
execution_authorization: none
---

# v1.5 Personal Knowledge Wiki Projection — GSD Preplanning Package

This package deliberately uses the normal GSD requirements → roadmap → phase context → research → plan → verification structure, but it is **not an active milestone**. v1.4 has not been executed or completed, so activating v1.5 in root `STATE.md` would falsely state a lifecycle transition.

## Product boundary

```text
Decision Cockpit: current state, decisions, confirmation, actions, outcomes, runtime
Wiki Projection: stable topic context, history, explicit relations, evidence navigation and freshness
Authority: canonical/KU/state/external/decision feedback remain the only fact authorities
```

v1.5 is a read-only, deterministic materialized projection. It is not a new fact store, an LLM-authored encyclopedia, a duplicate decision workflow, or a replacement for `docs/wiki/` developer documentation.

## Planned phases

| Phase | Goal | Requirements |
|---|---|---|
| 41 | Establish P0 topic identity and read-only projection contract | WIKI-01 |
| 42 | Make Project, Goal and Decision topics navigable and evidence-backed | WIKI-02 |
| 43 | Materialize, invalidate and rebuild Wiki projections without feedback into retrieval SSOT | WIKI-03 |
| 44 | Prove daily usability and decide evidence-based expansion | WIKI-04 |

## Activation gates

1. v1.4 Phase 36–40 passes its real implementation and UAT gates; a plan alone is not enough.
2. Actual Cockpit REST projection, evidence resolver and guarded write contracts are stable and documented from executed evidence.
3. P0 is kept to deterministic Project, Goal and Decision topics; no free editor, LLM narrative, skill/career auto-pages or external-topic authority.
4. A fresh GSD milestone switch creates the active requirements/roadmap/state entries; these preplanned files then become the canonical implementation inputs.

