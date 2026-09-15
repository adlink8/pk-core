# Project Retrospective

*A living document updated after each milestone. Lessons feed forward into future planning.*

## Milestone: v1.3 — Agent Productization

**Shipped:** 2026-07-18
**Phases:** 4 | **Plans:** 12 | **Tasks:** 9

### What Was Built

- Unified checksum-verifying read/explain contracts for External, Analysis, Pilot and Calibration across Service, REST, stdio MCP and ChatGPT HTTP MCP.
- A low-risk project orchestration state machine with exact preview confirmation, durable at-most-once generation boundaries and deterministic replay.
- A compact 16 KiB Agent envelope with typed recovery, bounded evidence drill-down and privacy-preserving stable IDs.
- A production-audited REST/MCP/tunnel supervisor, reviewed 44-tool descriptor and real ChatGPT Web ingress acceptance.

### What Worked

- Treating `/readyz` as the runtime gate separated daemon liveness from actual control-plane and MCP readiness.
- Authority SHA-256 fingerprints plus orchestration table deltas made side-effect claims independently checkable.
- A sanitized ChatGPT receipt preserved tool, state and replay evidence without storing account, tunnel, credential or raw-record data.
- Independent Luna review kept the milestone open until both read/explain and confirmed replay traversed the real ingress.

### What Was Inefficient

- The first supervisor gate used `/healthz`, which proved only process liveness and required late hardening to `/readyz`.
- The Codex app-server retained an older 15-tool discovery cache after connector refresh, so CLI discovery could not substitute for ChatGPT Web acceptance.
- Initial evidence closed the remote read path but not the remote confirmation path, requiring a second strict audit cycle.

### Patterns Established

- Validate runtime as `process alive → control plane ready → MCP channel ready → real tool receipt`.
- Keep ingress evidence separate from localhost evidence and label the transport explicitly.
- For confirmed flows, record before/after authority fingerprints and exact `sessions/events/confirmations/invocations` deltas.
- Refresh an existing connector after descriptor changes and verify through the actual consuming surface.

### Key Lessons

1. Readiness endpoints are semantic contracts; a green liveness probe is not evidence of an operable Agent path.
2. Model narration is not a receipt—tool names, compact contracts, IDs, byte bounds and database deltas must agree.
3. Exact replay acceptance needs both the same returned event and zero additional provider invocation.
4. Sanitization can preserve auditability when before/after hashes and typed counters remain explicit.

### Cost Observations

- Provider usage: no provider call during acceptance; orchestration invocations remained zero.
- Subagents: only configured `gpt-5.6-luna` routing was used for independent integration audits.
- Notable: focused 69-test Python and 23-test Node gates avoided broad, repeated paid execution.

---

## Milestone: v1.2 — External Context & Low-risk Decision Intelligence Pilot

**Shipped:** 2026-07-18
**Phases:** 4 | **Plans:** 14 | **Tasks:** 10

### What Was Built

- An independent append-only External Context Authority with reversible snapshots and exact dual-context binding.
- A strict evidence-bound LLM analysis candidate path with deterministic privacy, conflict, injection and risk gates.
- A real low-risk project decision/action/outcome chain plus a defer control path and compensating recovery.
- A preregistered personalized/generic comparison with immutable receipts, honest INCONCLUSIVE semantics and no auto-promotion.

### What Worked

- Exact IDs, checksums and source fingerprints made Phase 28→31 lineage independently reconstructable.
- One-shot provider budgets, deterministic gates and explicit zero-side-effect acceptance kept LLM work bounded.
- Deep code review caught duplicate-call, schema-validation and FAIL-path defects before closeout.
- Treating INCONCLUSIVE as a successful scientific boundary prevented an unsupported product claim.

### What Was Inefficient

- Historical UAT status vocabularies caused false open-artifact findings and required metadata normalization.
- The Phase 28 rehearsal UAT and later live cross-phase snapshot were not clearly distinguished until milestone audit.
- Initial Phase 31 token budgets were lower than the provider-reported context usage, forcing a protocol deviation.

### Patterns Established

- Preregister before provider invocation or action observation.
- Keep model output non-authoritative; publish only after deterministic evidence and safety gates.
- Store compensating events instead of mutating historical decisions, outcomes or proposals.
- Bind every downstream artifact to exact upstream IDs and checksums, then fingerprint sources before and after acceptance.

### Key Lessons

1. A minimum-evidence rule is useful only if insufficient evidence mechanically forces INCONCLUSIVE.
2. Generic-arm privacy requires an explicit null Personal context plus leakage tests, not prompt wording alone.
3. Provider-reported token accounting belongs in the frozen protocol when local estimates can diverge.
4. Rehearsal and live evidence need distinct labels even when both are valid UAT artifacts.

### Cost Observations

- Provider usage: three bounded real `gpt-5.4` tasks across Phases 29 and 31; Phase 31 used exactly two calls, zero retries and zero billed cost.
- Subagents: only configured `gpt-5.6-luna` routing was used for cross-phase integration audit.
- Notable: deterministic replay and focused test suites avoided repeat provider calls during review.

---

## Cross-Milestone Trends

### Process Evolution

| Milestone | Phases | Key Change |
|---|---:|---|
| v1.1 | 27 | Established composite SSOT, lifecycle and internal intelligence authorities |
| v1.2 | 4 | Added external authority, bounded LLM decisions and honest paired calibration |
| v1.3 | 4 | Productized guarded Agent surfaces and proved real ChatGPT tunnel ingress |

### Cumulative Quality

| Milestone | Audit | Requirements | Key verification |
|---|---|---:|---|
| v1.1 | passed | 17/17 | 70 focused integration tests |
| v1.2 | passed | 8/8 | 52 cross-phase tests plus per-phase suites |
| v1.3 | passed | 13/13 | 69 Python + 23 Node tests and real ingress receipts |

### Top Lessons (Verified Across Milestones)

1. Immutable lineage and reversible events are more reliable than in-place state correction.
2. User authority and deterministic gates must remain outside model output.
3. Runtime readiness and connector discovery must be proven on the actual consuming surface.
