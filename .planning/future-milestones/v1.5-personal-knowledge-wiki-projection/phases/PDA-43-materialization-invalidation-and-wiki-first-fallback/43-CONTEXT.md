---
phase: 43
name: Materialization, Invalidation and Wiki-first Fallback
status: preplanned_not_active
requirements: [WIKI-03]
depends_on: [41, 42]
---

# Phase 43 Context

## Goal

Make high-value topic pages durable materialized views without treating them as self-validating cache or sending projection text back into retrieval/index authority.

## Decisions

| ID | Decision |
|---|---|
| W-43-01 | Materialization stores topic/version/dependency/snapshot/checksum/freshness metadata, never copied personal facts or opaque page-only truth. |
| W-43-02 | An upstream dependency change invalidates only referenced topics; stale/partial/unavailable is visible until deterministic rebuild. |
| W-43-03 | Read order is fresh Wiki -> structured authority -> Active KU/search -> raw evidence. Any stale/partial/missing Wiki must skip to the next authoritative layer. |
| W-43-04 | Wiki page body/summary is excluded from KU extraction, Active Chroma writes and evidence authority inputs. |

## Boundaries

- No background provider execution or automatic promotion.
- Do not introduce “eventual fresh” behavior without an explicit invalidation/evidence proof.
- Rebuild must work after deleting a projection record; test with disposable fixtures only.

