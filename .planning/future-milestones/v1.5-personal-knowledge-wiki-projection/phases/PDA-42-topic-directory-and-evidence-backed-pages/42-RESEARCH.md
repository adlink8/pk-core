---
phase: 42
name: Topic Directory and Evidence-backed Topic Pages
milestone: v1.5 Personal Knowledge Wiki Projection
status: preplanned_not_active
research_type: implementation
requirements: [WIKI-02]
depends_on: [41]
execution_authorization: none
researched: 2026-07-22
---

# Phase 42 Research — Topic Directory and Evidence-backed Topic Pages

## Scope and activation boundary

This is **preplanning research only**. v1.4 is still a planned milestone, so Phase 42 must not add a route, component, endpoint, schema, migration, test fixture, runtime process or database data now.

Phase 42 consumes the Phase 41 read-only `topic.list`, `topic.get` and `topic.backlinks` contract. It creates the human-facing browse surface for the P0 topic types:

```text
Project → durable project context
Goal    → durable current/historical goal context
Decision → decision → action → outcome context
```

The Cockpit stays the current-state and workflow product. Wiki pages are explanatory, read-only topic projections:

```text
Cockpit /decisions/:id
  = inspect options, prepare, preview, confirm, resume guarded work

Wiki /knowledge/decision/:id
  = understand a published decision's authority, history, links, evidence,
    outcome and limitations; no prepare/confirm/action button
```

`docs/wiki/` remains developer/operator documentation and is unrelated to these routes.

## Current implementation facts

### Existing client composition is reusable, not a Wiki implementation

- `apps/personal_decision_cockpit/src/app/router.tsx` currently has `/state`, `/decisions`, `/actions`, `/external`, `/proactive`, `/evidence` and `/system`; it has no `/knowledge` route, topic route or backlinks route.
- `AppShell.tsx` currently labels the eighth navigation item as `证据中心 / Evidence`. Phase 42 may evolve that label to `知识与证据`, but must preserve `/evidence` as the diagnostic compatibility route defined by the v1.5 context.
- The frontend already standardizes same-origin reads through `api/client.ts`, Zod schemas in `api/schemas.ts` and TanStack Query hooks in `api/hooks.ts`. Topic reads must use the same pattern; a direct SQLite/Chroma/browser query would violate the Cockpit boundary.
- `PersonalStatePage.tsx` is a useful truthfulness baseline: it visibly groups `fact`, `observation` and `inference`, marks lifecycle states and keeps values metadata-only. Topic pages must preserve that property rather than flattening items into a fluent, apparently factual summary.
- `DecisionWorkspacePage.tsx` and `api/orchestration.ts` already own the guarded decision workflow. Phase 42 must link there when a user needs to act, rather than importing `ConfirmDrawer`, `NewSessionFlow` or `sessionPrepare` into a Wiki page.
- `EvidencePage.tsx` only embeds legacy MCP Widgets from `127.0.0.1:8789`. Its own text says formal authority wiring is future work. An iframe is therefore not a P0 evidence authority and cannot be the only evidence path for a Wiki Topic Page.

### Existing server authorities are granular and not yet topic-shaped

- `src/personal_knowledge/services/ui_projection.py` defines a Cockpit-only `decision_cockpit_projection_v1` envelope and read operations such as `personal_state.get`, `external_delta.get`, `decision_workspace.get` and `actions_recent.get`; no `topic.*` operation exists.
- `personal_state.get` provides current eight-domain assertion metadata, a personal snapshot binding, lifecycle status and `evidence_count`, but it deliberately does not expose assertion values or stable evidence records to the browser.
- `decision_workspace.get` can expose recommendation/history/outcome/effectiveness data for a known `recommendation_id`, with a Personal snapshot binding and a bounded support list. It is a useful Phase 41 input for a Decision topic, not a substitute for a Wiki page contract.
- `api_server.py` routes existing UI operations under `/ui/*`, intelligence explanation under `/intelligence/state/explain`, and decision records under `/decision/*`. Phase 42 should only consume the Phase 41 topic REST endpoints and a stable evidence resolver endpoint/operation introduced by the executed v1.4/Phase 41 contracts. It must not aggregate authority records in the browser.
- `IntelligenceService` and `state_projection.py` already validate typed evidence with snapshot/checksum/privacy constraints. The Wiki evidence drawer should receive stable server-provided references and rendering-safe metadata, not reconstruct or invent evidence links from labels.

## Standard Stack

Use the existing Cockpit stack; Phase 42 needs no new production dependency.

| Concern | Use | Why |
|---|---|---|
| SPA routing | `react-router-dom` v6, existing `createBrowserRouter` | Typed topic routes can live beside current Cockpit routes without an extra app or server. |
| Server read transport | same-origin REST from `api_server.py` | Keeps privacy sealing, authority access and response boundaries on the server. |
| Server projection | Phase 41 `personal_wiki_projection_v1` `topic.list/get/backlinks` | One read-only authority-bound envelope; the browser must not compose several authority APIs. |
| Response validation | existing Zod 3 schemas | Rejects an envelope/version/operation mismatch before it renders as personal truth. |
| Read caching | existing TanStack Query v5 | Query key must include `topic_type`, topic key/id and server-bound projection version/snapshot identity; cache is UI convenience only. |
| UI | React 18, TypeScript, Tailwind utility patterns already used by `StatePanel`, `SnapshotChip`, `FreshnessBadge`, `AuthorityBadge` | Maintains the Cockpit visual and accessible error/degraded states. |
| Component tests | Vitest + Testing Library + existing fixtures | Tests page sections, labels, links and forbidden controls without a live authority. |
| End-to-end/UAT later | existing browser/REST test approach from v1.4 Phase 40 | Verifies navigation, keyboard behavior, responsive Chinese/ID layout and partial authority state after real contracts exist. |

## Architecture Patterns

### 1. Server-owned topic envelope, browser-owned rendering

Phase 41 must produce a bounded, ready-to-render projection. Phase 42 only validates, renders and navigates it.

```text
Canonical/KU/State/External/Decision authority
        ↓   (Phase 41 explicit topic identity + read-only joins)
topic.get / topic.list / topic.backlinks envelope
        ↓   (same-origin REST + Zod exact operation/schema check)
Directory / Topic Page / Evidence Drawer
```

Required topic envelope properties for Phase 42:

```text
schema_version = personal_wiki_projection_v1
operation      = topic.list | topic.get | topic.backlinks
topic_id / topic_type / display label
source snapshot bindings and projection checksum
freshness = fresh | stale | partial | unavailable
authorities and limitations
section-specific stable evidence references
```

The client must verify the expected operation exactly. The current generic Cockpit `envelope()` schema accepts a free string for `operation`; Wiki schemas must refine it to the endpoint they call so a response from another projection cannot be rendered as a topic page.

### 2. Deterministic directory, not a second general search engine

`/knowledge` should list only Phase 41-published P0 topics. Each row/card has:

```text
type, title, stable key/ID, current freshness, last generated time,
authority/snapshot availability, and bounded related-decision metadata
```

It must not query semantic search, auto-create a page from a keyword, or promote an empty vector match into a Project/Goal/Decision entity. Empty and unavailable are separate:

```text
no published topic → honest empty state + link to existing evidence/search surface
authority unavailable → partial/unavailable state, not “no topic exists”
```

### 3. Fixed page grammar preserves epistemic truthfulness

Render a topic page in this stable order:

```text
Header: title, P0 type, freshness, generated time, snapshot/authority chips
Current context: only current, authority-bound items
Facts
Observations
Inferences
Forecasts / Recommendations (if present, labelled as non-facts)
Historical / superseded / conflict material
Related Personal State, decision feedback and external context
Explicit backlinks
Limitations, missing evidence and privacy-sealed notices
Evidence Drawer
```

Use text labels and icon/shape in addition to semantic color. A state item's `provenance_class` and lifecycle state are source metadata, not a frontend classifier. The server provides the grouping; the client must not infer Fact from confidence, wording or a non-empty evidence count.

### 4. Evidence drawer is a bounded reference resolver

The page passes only an opaque, server-issued stable reference (plus the expected source binding/checksum) to an evidence read operation. The server resolves and privacy-seals the response.

```text
Topic section → stable evidence_ref + authority/snapshot/checksum
             → read-only evidence resolver
             → metadata / eligibility / reason / safe drill-down route
```

The initial drawer should show enough to audit why the page said something:

```text
evidence type, reference ID, authority, snapshot/checksum match state,
privacy/sealing state, eligible/ineligible reason, and a safe deeper link
```

It does not put raw personal text in URL parameters, local storage, console logs or broad test fixtures. If an evidence item is sealed, missing or mismatched, display that precise limitation; do not fall back to an LLM summary or nearby memory-graph node.

### 5. Backlinks are explicit typed joins

P0 backlink objects must name the relation and source of the join:

```text
from_topic_id, to_topic_id, relation_type,
join_basis (topic_id | domain+scope | recommendation_id | support.record_id),
source_authority, source_snapshot/checksum
```

Only authoritative, explainable join bases are rendered. An empty list is valid and should say “no currently provable backlinks”; it must not trigger a client-side similarity search. A partial authority result is visually marked partial, not rendered as an authoritative zero.

### 6. Keep workflow actions as outbound links

For a Decision topic, the only actionable controls are navigation links such as:

```text
“Open Decision Workspace” → /decisions/:recommendationId
“View action and outcome”  → /actions?... (if a stable filter is implemented)
“Review source state”      → /state/:domain
```

There is no `prepare`, `preview`, `confirm`, `execute`, `suppress`, `record outcome` or general editor control in a Topic Page. This prevents the Wiki from overlapping or weakening the Cockpit's guarded write boundary.

### 7. URL identity rules

Use an opaque server-issued `topic_id` in the canonical route after Phase 41 resolves a typed user-facing key:

```text
/knowledge                         directory
/knowledge/project/:topicId        resolved Project topic
/knowledge/goal/:topicId           resolved Goal topic
/knowledge/decision/:topicId       resolved Decision topic
```

Do not encode raw subject/predicate/value, raw evidence IDs or human personal content in a URL. The server validates that the requested type matches the topic record; a mismatch returns a typed `not_found` or `type_mismatch` state rather than guessing a redirect.

## Do Not Hand-Roll

| Problem | Required reuse | Do not build |
|---|---|---|
| Fact authority, lifecycle and evidence validation | Existing Personal State, Decision, External and Evidence Resolver services | A browser-side fact merger, page-local lifecycle table or heuristic evidence checker. |
| Privacy sealing | `privacy_guard`, established API serialization and metadata-only authority outputs | Custom masking in React or raw evidence copied into fixtures/local storage. |
| Topic identity and dependencies | Phase 41 deterministic topic registry and projection contract | Slug-from-title identity, vector/entity matching or implicit alias inference. |
| Current/partial/stale semantics | Server `freshness`, `authorities`, `limitations`, snapshot bindings | Client timestamps or “last seen” guessing. |
| Decision confirmation/actions | Existing `GuardedOrchestrationInterface` and Cockpit workspace | A Wiki confirm drawer, direct POST request or UI-only idempotency mechanism. |
| Deep evidence | Server evidence resolver with stable references | Iframe scraping, opening generic legacy graph results, or fetching raw messages from the browser. |
| Routing/query/cache | Existing React Router, TanStack Query, Zod and `apiGet` | A parallel page state store, manual `fetch` bypassing validation, or a second frontend app. |

## Common Pitfalls and required verification

| Pitfall | Why it is dangerous | Required planned verification |
|---|---|---|
| Wiki and Decision Workspace become duplicates | A read page accidentally gains confirmation/action controls and bypasses the review flow. | Component tests assert topic-page controls contain only links; no orchestration imports/POST helpers are reachable from Wiki components. |
| Flowing prose collapses claim types | A labelled inference/recommendation appears to be a personal fact. | Fixtures cover Fact/Observation/Inference/Forecast/Recommendation/Historical/Conflict; DOM tests assert each section and label. |
| `/evidence` iframe is treated as authority | MCP may be unavailable and legacy graph is not current Personal State. | Evidence drawer tests use a server-issued reference; iframe failure is isolated and page evidence still has typed unavailable state. |
| Empty means unavailable | A failed authority creates a seemingly clean but false “no backlinks/no history” page. | Contract/DOM fixtures separately cover empty, partial and unavailable; no unavailable state may render a zero-count conclusion. |
| Topic identity drifts | URLs generated from display text can bind to a different topic or expose personal text. | Invalid/type-mismatched opaque ID tests return typed error; route tests use encoded opaque IDs only. |
| External facts become personal facts | The same visual card style or section merges their meaning. | Page tests assert external content renders inside an explicitly labelled external section with source/region/validity; it never appears in Personal facts. |
| Browser recomputes authority joins | Client code silently diverges from server snapshot and evidence rules. | API schema tests reject missing `topic_id`, snapshot, freshness and evidence reference fields; page receives ready projection only. |
| Wiki turns into broad search | Long-tail keyword results create false pages and page-count drift. | Directory tests assert only published P0 topics appear and a no-results state links out without creating a route/topic. |
| Privacy leaks through debug paths | Deep-link/query/console/fixture embeds original content. | Static review and tests use opaque IDs/checksums; no raw content in URLs, errors, browser logs or committed fixtures. |
| Long Chinese text/IDs break navigation | P0 topic metadata and checksums overflow or become unreadable on mobile. | 320px visual/UAT and RTL-style wrapping tests for long Chinese titles, opaque IDs, empty and partial states. |

## Recommended implementation plan shape

Phase 42 should remain a small, read-only UI phase after Phase 41 has real contracts. Split the future implementation into two plans, in dependency order:

1. **Directory, routing and typed read contract**
   - Add exact `personal_wiki_projection_v1` Zod schemas and topic hooks.
   - Add `/knowledge` and typed opaque topic routes; rename the navigation surface to Knowledge & Evidence while retaining `/evidence`.
   - Render published P0 Project/Goal/Decision directory cards with exact freshness/partial/empty/unavailable behavior.
   - Verify router, schema and directory accessibility against fixtures.

2. **Topic page grammar, evidence drawer and explicit backlinks**
   - Implement a shared read-only TopicPage frame plus strictly typed Project/Goal/Decision section adapters.
   - Add claim/lifecycle/external/limitation panels, explicit backlink renderer and bounded evidence drawer.
   - Add outbound links to Cockpit State/Decision/Action pages only; prohibit all guarded write components and calls.
   - Verify claim separation, sealed/missing evidence, type mismatch, partial authorities, no-backlink state, keyboard focus and long Chinese/ID layout.

No database migration, materialized-page persistence, dependency invalidation, Wiki-first RAG routing, LLM narrative or generalized entity page belongs in Phase 42. Those are Phase 43/44 or later decisions.

## Concrete files for the future implementation

| Area | Existing path | Likely Phase 42 change after activation |
|---|---|---|
| Router | `apps/personal_decision_cockpit/src/app/router.tsx` | Register `/knowledge` and typed P0 topic routes; retain `/evidence`. |
| Navigation | `apps/personal_decision_cockpit/src/components/layout/AppShell.tsx`, `MobileNav.tsx` | Evolve Evidence label/surface to Knowledge & Evidence without increasing primary navigation beyond current responsive budget. |
| API schemas | `apps/personal_decision_cockpit/src/api/schemas.ts` | Add strict, operation-specific Wiki envelopes, page/section/evidence/backlink models. |
| Hooks | `apps/personal_decision_cockpit/src/api/hooks.ts` | Add query hooks keyed by type + opaque topic ID and bounded evidence reference. |
| Shared page pieces | `apps/personal_decision_cockpit/src/components/authority/*`, `components/feedback/*` | Reuse/extend freshness, authority, snapshot and typed recovery primitives. |
| New pages | `apps/personal_decision_cockpit/src/pages/knowledge/*` | Directory, typed Topic Page frame, Evidence Drawer; no workflow components. |
| Compatibility evidence page | `apps/personal_decision_cockpit/src/pages/evidence/EvidencePage.tsx` | Keep legacy diagnostic widgets distinct and link them from Knowledge & Evidence rather than make them page authority. |
| Server route registry | `src/personal_knowledge/services/api_server.py` | Register only Phase 41 read-only `/ui/knowledge/*`/equivalent routes, using the final contract chosen there. |
| Tests | `apps/personal_decision_cockpit/src/test/*`, `tests/*ui*` or existing UI-projection test locations | Add contract, component and route tests; no live personal data. |

## Confidence and open prerequisites

| Finding | Confidence | Basis / prerequisite |
|---|---:|---|
| Topic Pages should be deterministic, read-only projections rather than another fact store | High | v1.5 requirements/spec and current multi-authority architecture agree. |
| Project/Goal/Decision is the correct P0 identity set | High | v1.5 requirements explicitly defer skill/career/external topic aliases. |
| Existing React Router/Zod/TanStack/Testing Library stack is sufficient | High | Present in `package.json` and used by all Cockpit pages. |
| Existing EvidencePage cannot satisfy current-object Wiki evidence by itself | High | It embeds legacy MCP widgets and explicitly says authority wiring is future work. |
| Stable evidence drawer shape can be finalized now | Low | It depends on actual Phase 37 evidence resolver execution and Phase 41 exact contract; do not choose query parameters or raw data shape in Phase 42 planning. |
| Canonical route segment should be opaque `topic_id` | Medium-high | Avoids private title/value exposure and future alias drift; Phase 41 must prove a stable registry/lookup. |
| A distinct `/knowledge/:type/:id/backlinks` route is needed in P0 | Medium | The directory/page can render a bounded inline backlink section first; pagination/full graph routing should be decided from actual cohort size during Phase 42 activation. |

## Phase exit evidence

Phase 42 may only be marked complete after implementation, not after this document, when all are true:

1. `/knowledge` browses only published P0 topics, and Project/Goal/Decision route resolution is deterministic and opaque-ID based.
2. Each page exposes source snapshot/authority/freshness, current/history/conflict/limitations and evidence access without presenting sensitive sealed content as plain text.
3. Fact, Observation, Inference, Forecast and Recommendation are visibly distinct; external context never appears as a personal fact.
4. Backlinks are server-provided explicit joins; empty/partial/unavailable states are distinguishable.
5. Wiki pages contain no write, provider, external-action or promotion path; any workflow link hands off to the Cockpit's existing guarded surfaces.
6. Contract, component, navigation, 320px, keyboard and degraded-state tests pass against non-sensitive fixtures; real cohort UAT remains Phase 44.

