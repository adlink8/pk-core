---
phase: 44
name: P0 Hardening, Cohort UAT and Expansion Decision
milestone: v1.5 Personal Knowledge Wiki Projection
status: preplanned_not_active
research_type: implementation
requirements: [WIKI-04]
depends_on: [41, 42, 43]
execution_authorization: none
researched: 2026-07-22
---

# Phase 44 Research — P0 Hardening, Cohort UAT and Expansion Decision

## Scope and activation boundary

This is **planning research only**. It creates no route, UI component, test
fixture, authority record, materialized page, UAT report, runtime process or
database data. v1.5 is a preplanning package and may become active only after
v1.4 has been executed, independently accepted and explicitly re-authorized.

Phase 44 is the release-evidence and scope-decision gate for the deterministic
P0 Wiki:

```text
Project / Goal / Decision topic pages
        + explicit authority bindings and evidence references
        + stale / partial / unavailable semantics
        + small, deliberately selected daily-use cohort
        ↓
privacy-safe, accessible and degraded-runtime UAT evidence
        ↓
Promote a new candidate scope | Defer it | Retire it
```

It does **not** implement Skill, Career, External Topic, free-form user notes,
general entity pages or LLM narrative. A Phase 44 "promote" result means only
that a later candidate milestone may be proposed; it never activates a feature,
changes an authority, calls a provider or promotes a page into Active KU.

The current Cockpit README labels its Phase 40 scope as complete, while root
v1.4 planning documents still require real implementation/UAT evidence. Phase
44 must rely on the later executed v1.4 acceptance record, not that README
statement or a mock-only test result.

## Current implementation facts and inherited constraints

| Area | Current reusable evidence | Phase 44 consequence |
|---|---|---|
| Cockpit test stack | `apps/personal_decision_cockpit/package.json` provides Vitest, Testing Library, TypeScript and Vite; `appSmoke.test.tsx` mounts production routes with mocked hooks. | Component and route tests are useful regression evidence, but cannot prove browser focus, zoom, privacy in network/devtools or live authority freshness. |
| Existing live-contract fixtures | `src/test/liveContract.test.ts` parses captured response fixtures through Zod. | Keep only metadata-safe fixtures; a fixture passing schema validation is not daily-use or privacy proof. |
| Runtime truth | `ui_projection.py` probes Chroma separately in `_system_status_get`; `/health` paths may intentionally avoid Chroma probes. | UAT must never infer that retrieval is live merely from a REST 200/health response. |
| Evidence authority | `intelligence/analysis/evidence.py` validates snapshot/checksum/privacy bindings; `retrieval/evidence.py:EvidenceResolver` is a typed, privacy-aware serving resolver. | Topic evidence must use server-issued opaque references. An iframe or legacy graph widget is not a replacement authority. |
| Existing Cockpit UAT guidance | v1.4 Phase 40 research and Cockpit README already specify same-origin build, response widths, keyboard/focus, reduced motion, 200% zoom and controlled degradation. | Reuse the approach, but test Wiki-specific stale/cache and claim-type behaviour instead of declaring Cockpit UAT sufficient. |
| Privacy policy | Service exits pass `privacy_guard`; Personal State and decision interfaces are metadata-only / fail-closed. | A sealed topic is a valid UAT result. The test must confirm the sealed state is truthful, not try to expose its body. |
| Projection boundary | Phase 41/42/43 are planned to own topic identity, read projections and materialization/invalidation. | Phase 44 measures those contracts only after they exist; it must not add browser-side authority joins, cache truth or fallback inference. |

## Standard Stack

Use the existing local-first stack; Phase 44 needs no new product dependency,
analytics SaaS, browser tracking SDK, cloud report uploader or second runtime.

| Concern | Reuse | Why |
|---|---|---|
| Build and UI regressions | `npm run test`, `npm run build`, Vitest, Testing Library | Covers page grammar, claim labels, route loading and controlled fixtures. |
| Server contract/integrity | existing `pytest` contract and integration patterns under `tests/contract`, `tests/integration`, especially UI projection/evidence/privacy tests | Confirms a browser page is backed by the actual read authority rather than a client-only mock. |
| Runtime delivery | `api_server.py` static `/app` delivery and `ops/runtime/start-agent-stack.ps1` / existing health-runbook conventions | Final browser proof must target the same-origin service owner, not only the Vite proxy. |
| Evidence resolution | `personal_knowledge.intelligence.analysis.evidence` and `personal_knowledge.retrieval.evidence.EvidenceResolver` | Preserves checksum, privacy and metadata-first behaviour. |
| Accessibility/reliability evidence | scripted browser checks only if dependency/security review permits them; otherwise a documented local manual checklist | A green jsdom suite cannot substitute for real keyboard, focus, zoom and responsive behaviour. |
| UAT evidence | a versioned, redacted Markdown/JSON report format with opaque topic IDs, short hashes and test fixture identity | Makes acceptance auditable without creating a second private-data store. |

## Architecture patterns

### 1. Cohort manifest is an evaluation input, not a new personal authority

Phase 44 should pre-register a small cohort that includes at least one valid
P0 topic from each supported type:

```text
Project topic
Goal topic
Decision topic
```

Each cohort row should contain only safe metadata:

```text
opaque topic_id / type
expected projection status
expected authority bindings (short/redacted form)
freshness scenario
one intended user task
fixture-or-live-read-only classification
```

It must not contain raw topic text, user goals, original evidence, preview JSON,
HMAC material, provider output, URL containing personal labels or a copied
Personal State body. The cohort may be stored as a versioned **evaluation
manifest**, but not as a fact registry or a way to publish page content.

Use two complementary data modes:

```text
Disposable deterministic fixtures
  → prove all expected fresh/stale/partial/privacy/error branches safely

Explicitly approved, read-only local cohort
  → prove ordinary browse tasks and perceived usefulness on real current
     projections, without copying raw bodies into the repository/report
```

If real read-only UAT is not explicitly approved or cannot be conducted without
capturing private material, Phase 44 can establish deterministic safety but must
return `defer` for the daily-use proof rather than fabricate it from fixtures.

### 2. UAT has separate truth, usability and operational acceptance dimensions

The minimal daily-use journey is not "open every route." For each cohort topic,
the user must be able to answer all applicable questions:

```text
What is the currently valid context?
Which part is Fact, Observation, Inference, Forecast or Recommendation?
What is historical/conflicting rather than current?
Why does the page say this, and can I reach its evidence/authority binding?
Is information fresh, stale, partial, sealed or unavailable?
Where do I go to act, without the Wiki offering a write control?
```

Measure these dimensions independently:

| Dimension | Required evidence | Not a substitute |
|---|---|---|
| Truthfulness | Current/history and claim-type sections match the server projection; external facts remain separate; empty differs from unavailable. | Fluent page copy, a confidence color or nonzero evidence count. |
| Traceability | A user can open a bounded, privacy-aware evidence/authority drill-down and see binding/sealing state. | Legacy MCP iframe, graph neighbour or raw body copied into the page. |
| Freshness | Fresh/stale/partial/unavailable pages visibly differ; refresh/rebuild/fallback retains its actual source binding. | Client timestamp, stale cached text or HTTP health alone. |
| Usability | Cohort task completion, understandable limitations and no decision-workflow duplication. | Page view count, route mount, screenshot or time-on-page alone. |
| Privacy | No raw personal body/secret/confirmation material in URL, storage, DOM artifact, console, network diagnostic or committed fixture. | Source-code comment saying payloads are redacted. |
| Accessibility | Keyboard/focus, 320/768/1024 layouts, 200% zoom, long Chinese/opaque IDs, semantic status labels and reduced motion checks. | A desktop screenshot or a high-DPI simulation. |
| Degraded recovery | REST, one upstream authority, evidence resolver, Chroma/retrieval and legacy widget failures are distinct and recoverable without false current claims. | A blank card or a generic "loading failed" message. |

### 3. Controlled fault injection must happen at authority boundaries

Use a disposable service fixture, a controlled injected read-service failure or
a test-only loopback substitute. Never terminate arbitrary local processes,
mutate a live personal authority database, change an Active Pointer/Watermark,
or fake a successful `topic.*` envelope inside React.

Required behaviour matrix:

| Scenario | Expected truthful result |
|---|---|
| Fresh Project/Goal/Decision | Header shows source binding/freshness; current content is visibly distinct from history and non-facts. |
| Upstream binding changed | Only dependent topic becomes stale/partial; old current summary is not advertised as fresh; deterministic rebuild receives a new checksum/binding. |
| One authority unavailable | Only dependent section is partial/unavailable with typed limitation; unrelated section remains useful; zero count is never implied. |
| Evidence sealed/mismatched | Metadata/sealing/ineligibility reason is shown; no raw body, LLM replacement or nearby graph node is substituted. |
| Chroma unavailable | System/retrieval status is explicit; structured authorities may remain readable; `/health` success is not used as Chroma proof. |
| REST unavailable | Page shows an error and safe retry/recovery, not cached personal content presented as current. |
| Legacy MCP widget unavailable | Existing `/evidence` compatibility card degrades honestly; authoritative Wiki evidence path remains separately testable. |

### 4. Privacy-safe UAT evidence is binding-aware and minimal

Every planned UAT result should record:

```text
source revision / package-lock hash
Cockpit build result and source asset location
projection schema version and operation
short/redacted Personal, External, Serving and Decision snapshot bindings
cohort manifest version and disposable-vs-live-read-only classification
scenario, expected state, observed status/reason code and pass/fail
zero-write / no-provider / no-external-action fingerprint
recovery attempted and outcome
```

It must not record raw personal messages, topic prose, evidence bodies,
full local filesystem paths, private query values, confirmation/preview JSON,
HMAC/idempotency secrets, provider bodies, credentials, tunnel URLs or browser
HAR/video by default. A failure report needs a typed code and redacted artifact
reference, not a reproduction bundle full of private data.

For an intentionally user-approved live read-only cohort, record the approval
and the fact that it was read-only. No test result may "clean up" a real
append-only record by delete/reset. Phase 44 itself has no reason to perform a
guarded write; any unrelated write-flow regression stays in v1.4's disposable
low-risk test lane.

### 5. The expansion decision is a gate, not an aggregation score

The decision document should assess each proposed expansion separately:

```text
Skill page
Career direction page
External topic page
User metadata / notes
LLM narrative candidate
Broader entity types
```

For each candidate, record all required gates:

| Gate | Question |
|---|---|
| Identity | Is there a stable, explainable identity/alias/disambiguation rule rather than a text or vector guess? |
| Authority | Which current authority owns its facts, lifecycle, history and evidence? |
| Privacy | Can sealing, correction, retention and visibility be enforced without copying personal bodies? |
| Freshness | What dependency and invalidation rule prevents stale prose from looking current? |
| Utility | Did the P0 cohort show a real repeated need this candidate would solve, rather than a desire for more page types? |
| Evaluation | What deterministic and human checks could distinguish a correct page from a plausible but wrong one? |
| Boundary | Does it avoid duplicating Cockpit writes, general search, Active KU/Chroma or `docs/wiki/`? |
| Operations | Is its rebuild/failure/rollback path bounded and observable? |

The only valid outcomes are:

```text
PROMOTE_CANDIDATE — all gates have evidence; create a separately authorized candidate spec
DEFER — one or more gates lack evidence; record exact missing proof and re-evaluation trigger
RETIRE — the use case has no demonstrated utility or violates the P0 authority boundary
```

`PROMOTE_CANDIDATE` never means automatic coding, a new root milestone, provider
usage or publication. LLM narrative has an additional non-negotiable gate:
Candidate → Eval → Publish → Rollback with evidence coverage, privacy review and
no fact-write authority. It should normally remain deferred after deterministic
P0 unless that separate contract is explicitly planned.

## What a rendered page cannot prove

The following must not be inferred from a page rendering, a route smoke test or
a visually attractive screenshot:

1. **Projection correctness:** rendering cannot prove the topic was reconstructed
   from the correct snapshots or that its dependency manifest is complete.
2. **Currentness:** rendering does not prove stale material was invalidated when
   an upstream authority changed.
3. **Evidence truth:** a visible citation count does not prove evidence resolves,
   matches its checksum or remains privacy-eligible.
4. **Privacy:** a mocked metadata card does not prove browser storage, URL,
   console, network errors and test artifacts contain no sensitive content.
5. **Usability:** an automated DOM mount does not prove a user understands
   claim-type labels, limitations or the distinction between current and
   historical material.
6. **Accessibility:** screenshot-only checks cannot prove keyboard order, focus
   restoration, Escape behaviour, text zoom or reduced-motion operation.
7. **Operational truth:** a REST health response cannot prove Chroma/retrieval,
   each authority, MCP compatibility widget or static deployment is available.
8. **Scope readiness:** three successful P0 topics do not prove that Skill,
   Career, External Topic, notes or LLM narrative have safe identities and
   authorities.

## Do not hand-roll

| Do not build | Why | Reuse instead |
|---|---|---|
| Browser "offline Wiki" cache/service worker for topic payloads | Can revive sealed/stale personal facts as current and creates a privacy/retention surface. | Explicit freshness/partial panels and current server authority reads. |
| Client-side usefulness/health score | Hides the reason a page or authority failed and invites false precision. | Dimension-specific UAT matrix and promote/defer evidence table. |
| Page-view analytics or cloud telemetry | Stores personal browsing behaviour and does not prove decision usefulness. | User-approved, local metadata-only UAT record. |
| Test utility that updates live SQLite/Chroma/Watermark | Contaminates the authorities Phase 44 is judging. | Disposable fixtures or controlled read boundary failure injection. |
| Screen scraping or MCP iframe as evidence resolver | Has no stable authority binding and can silently fail/return legacy graph content. | Existing server-side evidence resolver and opaque evidence references. |
| React-side stale calculation / fallback search | Diverges from Phase 43 dependency bindings and can make stale content look fresh. | Server-provided freshness, limitations and Wiki-first fallback contract. |
| Automatic scope promotion | Turns a limited UAT sample into unreviewed data/model expansion. | Written promote/defer/retire decision plus a later explicit GSD milestone switch. |

## Common pitfalls

| Pitfall | Why it fails WIKI-04 | Required prevention |
|---|---|---|
| Select only convenient fresh pages after testing | Masks stale/partial/sealed behaviour and makes cohort evidence non-representative. | Pre-register cohort types, expected states and task prompts before execution. |
| Treat deterministic fixtures as daily-use proof | Fixtures prove bounded behaviour, not whether stable topic context is useful to the user. | Separate fixture safety evidence from approved live-read-only cohort evidence; defer utility conclusion if absent. |
| Capture screenshots/HAR/traces with page prose | Creates a new private-data artifact outside authority retention policy. | Metadata-only fixtures, redaction review, access-controlled/local ignored artifacts and typed failure records. |
| Test staleness by modifying real data then deleting it | Violates append-only/lifecycle governance and cannot safely restore authority history. | Disposable authority fixture or reversible derived-store test seam. |
| Call a page "fresh" because it loaded | Load state has no authority semantics. | Assert snapshot/checksum/freshness and dependency change behaviour. |
| Present an empty backlinks/history panel after a failed read | Turns unavailable authority into a false negative conclusion. | Distinct empty, partial, unavailable and sealed UI/contract cases. |
| Let a topic page expose prepare/confirm/action controls | Duplicates Decision Workspace and weakens guarded-write boundaries. | Static/import tests plus DOM assertions: Wiki exposes only outbound workflow links. |
| Validate only desktop English fixtures | Misses Chinese wrapping, opaque IDs, mobile navigation and zoom behaviour. | 320/768/1024 plus 200% zoom and long Chinese/ID scenarios. |
| Promote Skill/Career pages because their names appear often | Frequency does not solve alias ambiguity, authority ownership or lifecycle. | Require the separate identity/authority/privacy/evaluation gate table. |
| Promote LLM narrative because its prose looks better | Plausibility is not evidence and creates a derived-fact loop. | Keep deferred until an independently planned Candidate → Eval → Publish → Rollback contract passes. |

## Concrete future implementation and verification paths

| Area | Existing / planned path | Phase 44 role after activation |
|---|---|---|
| Topic contract | Phase 41 `topic.*` projection service and corresponding `/ui/knowledge/*` routes | Confirm exact operation/schema, source bindings, freshness, limitations and no-write modes. |
| Topic UI | `apps/personal_decision_cockpit/src/pages/knowledge/*` planned in Phase 42 | Perform keyboard/responsive/claim/evidence/backlink UAT; keep write components absent. |
| Materialization | Phase 43 derived projection/dependency registry | Exercise stale detection, deterministic rebuild and Wiki-first fallback without writes to KU/Chroma. |
| API/static host | `src/personal_knowledge/services/api_server.py` | Test production same-origin `/app/` instead of only Vite proxy behaviour. |
| Evidence | `src/personal_knowledge/intelligence/analysis/evidence.py`; `src/personal_knowledge/retrieval/evidence.py` | Verify opaque reference resolution, sealing/mismatch responses and snapshot/checksum continuity. |
| UI tests | `apps/personal_decision_cockpit/src/test/*`, including `appSmoke.test.tsx` and `liveContract.test.ts` patterns | Add topic fixtures, exact schema checks and no-write/evidence-state tests with no private bodies. |
| Server tests | `tests/contract/test_ui_projection*.py`, `tests/contract/test_evidence_resolver.py`, privacy/integration patterns | Add contract and dependency-invalidation tests; verify zero provider/external/index side effects. |
| Runtime | `ops/runtime/start-agent-stack.ps1`, `ops/runtime/smoke-agent-stack.py`, existing runbooks | Record actual readiness and distinguish REST, MCP/widget, Chroma and individual authority state. |
| Evidence report | future Phase 44 redacted UAT and expansion-decision artifacts | Keep reports metadata-only, versioned and reviewable; do not update root milestone state until all gates pass. |

## Recommended future verification sequence

### 1. Deterministic pre-UAT regression

After implementation, run the smallest targeted matrix first:

```powershell
Set-Location <repo-root>\apps\personal_decision_cockpit
npm run test
npm run build

Set-Location <repo-root>
$env:PYTHONPATH = "$PWD\src"
python -m pytest `
  tests/contract/test_ui_projection.py `
  tests/contract/test_ui_projection_state_external.py `
  tests/contract/test_evidence_resolver.py `
  tests/integration/test_personal_state_privacy.py -q
```

The exact future Wiki-specific test filenames should be selected only after
Phases 41–43 implement their final contract. The execution plan must add tests
for topic identity, P0 type mismatch, current/history grouping, explicit
backlink basis, sealed evidence, stale/partial/unavailable, derived-store
rebuild, no KU/Chroma write and no provider/external action.

### 2. Controlled integration scenarios

For disposable fixtures only:

1. Materialize a fresh Project, Goal and Decision cohort; record bindings.
2. Change one upstream fixture binding; assert only dependent topics become
   stale/partial and old text loses its current/fresh status.
3. Rebuild; assert a new projection checksum/binding and no authority,
   retrieval-index, provider or external-action writes.
4. Trigger each typed failure: one authority unavailable, sealed/mismatched
   evidence, Chroma unavailable, REST unavailable and legacy widget unavailable.
5. Assert topic UI still distinguishes an honest empty result from failure and
   never exposes write controls.

### 3. Browser and human UAT matrix

Run on the production same-origin Cockpit path, not only `npm run dev`:

| Scenario | Required observation |
|---|---|
| Cohort browse | Directory → each P0 topic type → current/history/claim sections → evidence → backlinks works with opaque identifiers. |
| Knowledge versus decision | A Decision topic links to the Decision Workspace but exposes no prepare/confirm/action/outcome mutation control. |
| Freshness/rebuild | Fresh, stale, partial, unavailable and sealed are readable, distinguishable and correctly recoverable. |
| Privacy audit | Inspect URL, local/session storage, DOM, console, network messages and approved artifacts before/after browsing; only allowed UI preferences persist. |
| Responsive | 320, 768, 1024 and 1440 layouts have no page-level horizontal overflow; long Chinese text and IDs remain readable. |
| Keyboard/accessibility | Tab/Shift+Tab order, visible focus, Escape/focus restoration for any drawer, semantic status labels, reduced motion and manual 200% browser zoom pass. |
| Runtime degradation | REST, MCP widget, Chroma and a single authority show distinct recovery/limitation states; no service health inference is overclaimed. |
| Read-only invariant | Browser and server audit shows zero topic fact writes, zero provider calls, zero external actions, zero promotion and zero KU/Chroma writes. |

### 4. Cohort usefulness and expansion decision record

The UAT record should include a short, user-reviewed result for each task:

```text
Could the user identify current context without confusing history?
Could they state why the page made its claim and reach a safe evidence path?
Could they recognize partial/stale/sealed information and choose a safe next step?
Did the page avoid requiring a broad search/re-summary for this high-value topic?
What repeated unmet need remains, if any?
```

Then publish a separate, evidence-linked `PROMOTE_CANDIDATE`, `DEFER` or
`RETIRE` decision for each expansion candidate. No page-count, render success,
single anecdote or model prose may override a missing identity/privacy/evaluation
gate.

## Planning conclusion

Phase 44 should be a **truthful-use and scope-governance gate**, not cosmetic
polish and not a launch of broader Wiki intelligence. The deterministic P0 is
ready to expand only when it proves that small Project/Goal/Decision topics are
useful in routine browse tasks while remaining reconstructable, privacy-sealed,
freshness-aware and visibly degraded when an authority is unavailable.

The most likely valid first outcome is `DEFER` for Skill/Career/External Topic,
user notes and LLM narrative unless their identity and authority contracts have
their own evidence. A quality P0 cohort is more valuable than a large page count
or a premature “personal encyclopedia.”

## Sources

### Repository sources

- `.planning/future-milestones/v1.5-personal-knowledge-wiki-projection/{README.md,ACTIVATION.md,PROJECT.md,REQUIREMENTS.md,ROADMAP.md}`.
- `44-CONTEXT.md`, Phase 41–43 contexts/research, and `.planning/future-milestones/v1.5-personal-knowledge-wiki-projection/SPEC.md`.
- `.planning/phases/PDA-40-product-hardening-and-live-uat/{40-CONTEXT.md,40-RESEARCH.md}`.
- `apps/personal_decision_cockpit/{package.json,README.md,src/test/appSmoke.test.tsx,src/test/liveContract.test.ts}`.
- `src/personal_knowledge/services/{api_server.py,ui_projection.py}`.
- `src/personal_knowledge/{intelligence/analysis/evidence.py,retrieval/evidence.py}`.
- `tests/{contract,test_evidence_resolver.py,integration}` privacy, projection and authority patterns.
- `ops/runtime/{start-agent-stack.ps1,smoke-agent-stack.py}` and existing runtime/readiness runbooks.

### External guidance inherited from v1.4 Phase 40 research

- Playwright accessibility guidance: automated checks supplement manual and inclusive assessment.
- WCAG 2.2 keyboard operation, visible focus and 200% text-resize requirements.

