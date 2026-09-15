---
phase: 44
name: P0 Hardening, Cohort UAT and Expansion Decision
status: preplanned_not_active
requirements: [WIKI-04]
depends_on: [41, 42, 43]
---

# Phase 44 Context

## Goal

Prove a small Project/Goal/Decision cohort can be browsed safely and usefully in ordinary use, then make a formal evidence-based promote/defer decision for the next Wiki capability.

## Decisions

| ID | Decision |
|---|---|
| W-44-01 | UAT measures usability, truthfulness, freshness, evidence reachability and degraded recovery—not a vague “personal intelligence score.” |
| W-44-02 | Cohort starts intentionally small and high-value; page count is not a success metric. |
| W-44-03 | Skill/Career/External Topic pages, LLM narrative and freeform notes need separate identity, authority, privacy and evaluation gates. |
| W-44-04 | No future capability is activated merely because P0 pages render; a written promote/defer/retire decision is the deliverable. |

## Boundaries

- Browser/UAT proof must distinguish deterministic fixtures from live personal data.
- Privacy-sealed content must remain sealed in screenshots, exports, logs and test fixtures.
- No user data deletion, no provider call and no external action without a separately scoped authorization.

