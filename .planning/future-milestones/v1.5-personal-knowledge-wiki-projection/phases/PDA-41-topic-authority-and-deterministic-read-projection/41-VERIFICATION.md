---
phase: 41-topic-authority-and-deterministic-read-projection
status: preplanned_not_active
verification_mode: future_execution
requirements:
  WIKI-01: planned
execution_prerequisite: v1.4 audit accepted and 41-PREFLIGHT-RECORD GO
---

# Phase 41 — Verification Plan

## Hard preflight

Before any Wiki implementation, `41-PREFLIGHT-RECORD.md` must show an evidence-backed GO for executed v1.4 Projection/REST/Evidence interfaces, safe error behavior, CORS/Origin mutation policy, canonical P0 fields and disposable binding fixtures. Any changed/missing/unsafe result is STOP → refresh research/plan/checker.

## Completion evidence

| Capability | Required proof |
|---|---|
| Strict identity | Parser accepts only canonical Project/Goal/Decision keys; no fuzzy/semantic/LLM identity path. |
| Unified reads | `topic.list/get/backlinks` all expose `personal_wiki_projection_v1`, snapshots, freshness, partial, checksum and bounded source/evidence refs. |
| Truth/privacy | Fact/history/conflict/external categories remain separate; sealed/mismatch/unavailable yields typed safe recovery. |
| Read-only transport | GET parity with direct service; all non-GET methods are 405 with zero service invocation; OPTIONS preserves v1.4 security policy. |
| No new authority | Fixture fingerprints prove zero writes to all upstream authorities, KU/Chroma, Provider, Active Pointer and external actions. |

## Required tests

1. Unit parser/key/error tests.
2. Direct service contract tests for all three operations and partial/privacy/binding failures.
3. REST parity, non-GET/OPTIONS and safe-error tests.
4. Synthetic side-effect spies and delete/rebuild-without-page-store proof.

## Pass / block

WIKI-01 passes only if every item above passes on disposable fixtures. A missing canonical field, CORS regression, unbound evidence reference, opaque exception, or any write side effect blocks Phase 42.

