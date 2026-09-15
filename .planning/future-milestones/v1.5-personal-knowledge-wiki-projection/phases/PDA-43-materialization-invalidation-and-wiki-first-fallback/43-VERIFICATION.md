---
phase: 43-materialization-invalidation-and-wiki-first-fallback
status: preplanned_not_active
verification_mode: future_execution
requirements:
  WIKI-03: planned
depends_on_phase_verification: [41, 42]
---

# Phase 43 — Verification Plan

## Completion evidence

| Capability | Required proof |
|---|---|
| Derived-only materialization | Store contains immutable version/dependency metadata only—no fact body, raw evidence, embedding or Provider text. |
| Correct invalidation | Explicit dependency change stales only affected topics; a deleted derived record deterministically rebuilds from upstream data. |
| Controlled first build | Only the manual/local materialize receipt creates a first version. GET, browser load, read router, retry and change detector never materialize. |
| Real fallback | GET-only `topic.resolve` reaches the actual router and returns selected/attempted sources, reason and bindings for fresh/stale/partial/missing/long-tail cases. |
| No feedback loop | Wiki marker never enters KU extraction, Chroma, Active pointer, evidence authority, Provider prompt or external action. |

## Required tests

1. Derived-store schema/immutability/canonical manifest/selective invalidation tests on disposable SQLite fixtures.
2. HTTP and router integration tests for all status paths, `include_evidence=false` bounded KU fallback and source-specific provenance.
3. Non-GET 405, OPTIONS policy and zero-service-invocation transport tests.
4. Synthetic content trace tests proving no Wiki-to-KU/Chroma/evidence/Provider feedback.
5. Serving snapshot and Evidence Resolver regression tests.

## Pass / block

WIKI-03 passes only if stale pages genuinely fallback through an accessible read route and all no-feedback tests pass. TTL-only freshness, implicit GET materialization, a hidden/unreachable router or any vector/index write blocks Phase 44.

