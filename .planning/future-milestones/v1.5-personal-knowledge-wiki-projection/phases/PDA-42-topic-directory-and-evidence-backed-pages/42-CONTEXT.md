---
phase: 42
name: Topic Directory and Evidence-backed Topic Pages
status: preplanned_not_active
requirements: [WIKI-02]
depends_on: [41]
---

# Phase 42 Context

## Goal

Turn P0 Topic Projections into a navigable directory and three page types that explain long-lived context, while leaving current decision, confirmation, action and outcome workflows in the Cockpit.

## Decisions

| ID | Decision |
|---|---|
| W-42-01 | Navigation evolves “Knowledge & Evidence” with `/knowledge` and P0 typed topic routes while retaining `/evidence` compatibility. |
| W-42-02 | Every page keeps current fact, observation, inference, forecast, recommendation, history/conflict and external context in visibly distinct sections. |
| W-42-03 | Evidence Drawer accepts stable server-provided reference only; backlinks use explicit keys only and can truthfully be empty. |
| W-42-04 | Pages are read-only. Corrections link out to existing lifecycle/guarded flow rather than editing the page or duplicating a Decision Workspace. |

## Boundaries

- Directory is for published high-value P0 topics, not a second general search product.
- Do not show a universal score, hide stale state, transform external facts into personal facts or add a confirm/action button.
- P0 has no iframe/widget dependency for authority; legacy graph/widget stays diagnostic only.

