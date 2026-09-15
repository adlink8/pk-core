---
phase: 44-p0-hardening-cohort-uat-and-expansion-decision
status: preplanned_not_active
verification_mode: future_execution
requirements:
  WIKI-04: planned
depends_on_phase_verification: [41, 42, 43]
---

# Phase 44 — Verification Plan

## Completion evidence

| Capability | Required proof |
|---|---|
| P0 cohort | A small synthetic/de-identified Project/Goal/Decision cohort covers fresh/stale/partial/unavailable/privacy/evidence/runtime states. |
| Product usability | Responsive 320/768/1024/1440, keyboard/focus/Escape/reduced-motion/200% zoom, long Chinese/opaque IDs and offline/degraded recovery are verified. |
| Privacy/truth | Artifacts are redacted, concrete-secret scans pass and each page exposes source/freshness/evidence/limitations rather than cached assertions. |
| Live boundary | Fixtures prove contracts only. Any live read-only UAT needs separately recorded user authorization; no authorization yields DEFER, not pass. |
| Expansion discipline | Skill, Career, External Topic, Notes, LLM narrative and broader entities each receive an eight-gate PROMOTE_CANDIDATE / DEFER / RETIRE decision. |

## Required checks

1. Full contract/component/build/degraded matrix with zero provider/write/external-action fingerprints.
2. A clearly separated, authorized read-only live UAT when available; never implicit live data use.
3. Artifact review of fixture/report/log/screenshot/console/network output. Security terms in protocols are allowed; concrete credentials, raw private content, confirmations/previews and tunnel URLs are not.
4. A redacted UAT report and a written expansion decision. `PROMOTE_CANDIDATE` is not activation or implementation authorization.

## Pass / block

WIKI-04 passes only with useful P0 cohort evidence, privacy/degraded/a11y proof and a written scope decision. “Pages render”, a plan, or a fixture-only report cannot justify automatic domain expansion or LLM/editor work.

