---
phase: 41
name: Topic Authority and Deterministic Read Projection
status: preplanned_not_active
research_type: implementation
requirements: [WIKI-01]
depends_on: [v1.4 Phase 36, v1.4 Phase 37]
milestone: v1.5 Personal Knowledge Wiki Projection
activation_note: "Revalidate after v1.4 completion; this document authorizes no execution."
---

# Phase 41 Research — Topic Authority and Deterministic Read Projection

## Scope and current-state boundary

Phase 41 is a future **read-only contract** for Project, Goal and Decision topics.
It must resolve existing authorities into deterministic `topic.list`, `topic.get`
and `topic.backlinks` responses. It is not a Wiki UI, an entity graph, an LLM
feature, a page store, or a new fact authority.

```text
existing authority reads
  -> exact TopicKey validation
  -> deterministic type-specific joins
  -> snapshot-bound read envelope + checksum
```

Materialized pages, dependency persistence and invalidation are Phase 43 work.
Topic pages and evidence drawer UI are Phase 42 work. The v1.5 package is inactive:
it must not change root `STATE.md`, root `ROADMAP.md`, code, database, provider,
index or runtime before v1.4 has completed and been revalidated.

## Current implementation facts

| Existing component | Evidence | Phase 41 use | Boundary |
|---|---|---|---|
| Cockpit Projection | `src/personal_knowledge/services/ui_projection.py` | Reuse envelope conventions: operation, generated time, snapshot bindings, freshness, authorities, partial, limitations, data. | Use a separate Wiki schema version. |
| Cockpit REST | `src/personal_knowledge/services/api_server.py` | Reuse the thin adapter pattern only after v1.4 transport/security work is accepted. | No browser/database direct access. |
| Personal state | `ui_projection.py`; `tests/contract/test_ui_projection_state_external.py` | Exact Project/Goal keys, lifecycle and provenance. | Current Cockpit projection is metadata-only; Wiki cannot relax privacy. |
| Decision feedback | `ui_projection.py`; `services/decision_intelligence_reads.py` | Resolve exact `decision:{recommendation_id}` and its chain references. | Outcome is never causal proof. |
| Analysis evidence gate | `intelligence/analysis/evidence.py` | Enforce authority ID, record ID, snapshot and checksum binding. | Return references, not unbounded content. |
| Serving evidence resolver | `retrieval/evidence.py` | Reuse read-only, privacy-aware evidence presentation later. | Do not create a parallel raw-evidence reader. |

`docs/wiki/` is the static developer/operator Wiki (for example
`docs/wiki/08-data-governance.md`), not a personal-Wiki source or migration target.

## Standard Stack

Use existing repository patterns. No dependency or service should be added.

| Concern | Standard choice | Reason |
|---|---|---|
| Service boundary | A narrowly named `TopicProjectionService` adjacent to `services/ui_projection.py`. | Keeps all authority joins server-side and testable. |
| Read safety | SQLite URI `mode=ro` and `PRAGMA query_only=ON`. | Existing evidence modules use it; it excludes accidental writes. |
| Envelope | `personal_wiki_projection_v1`. | Stops Cockpit-only vocabulary becoming an accidental public API. |
| Inputs | Existing Personal State, Decision, External, Serving and evidence read services. | Preserves current SSOT, lineage and privacy policy. |
| Transport | Existing `api_server.py` JSON-contract adapter. | Avoids parallel HTTP servers and browser-side joins. |
| Verification | Pytest deterministic fixtures and contract tests. | Requires no provider, Chroma daemon or production database. |

Do **not** introduce an ORM, graph DB, vector DB, cache daemon, LLM SDK, topic-search
engine or browser-side authority composition in this phase.

## Architecture Patterns

### Explainable identity before retrieval

P0 identity is parsed and looked up exactly; semantic similarity is forbidden.

```text
topic string -> strict grammar parser -> type-specific authority lookup -> envelope
```

The only accepted P0 grammar is:

```text
project:{scope}
goal:{domain}:{scope}:{predicate}
decision:{recommendation_id}
```

Implement one typed `TopicKey` parser rather than string-splitting in loaders. It
must reject empty components, unknown types, ambiguous extra segments and invalid
encoding. Decode a URL path once at the transport boundary; return the canonical key
from the service and never silently map one key to another.

If a real authority allows separator characters inside `scope` or `predicate`, the
implementation must use a reversible canonical encoding or exclude that P0 type.
It must not replace deterministic identity with fuzzy matching.

### On-demand deterministic projection, not a cache

Phase 41 should assemble the response on each read and calculate a
`projection_checksum` over canonical JSON of published data, dependency references
and bindings. It must not persist the page body or a page table.

```text
read-only authority inputs
  -> deterministic filtering and joins
  -> typed current/history/conflict sections
  -> evidence and dependency references
  -> canonical serialization + checksum
  -> personal_wiki_projection_v1
```

The checksum proves which projection was observed; it does not turn the projection
into an authority. Phase 43 alone may introduce version/dependency metadata and
stale/rebuild behavior.

### Authority-bound envelope and partial truth

Use an independently versioned envelope:

```json
{
  "schema_version": "personal_wiki_projection_v1",
  "operation": "topic.list|topic.get|topic.backlinks",
  "ok": true,
  "generated_at": "RFC3339 UTC",
  "topic": {"topic_id": "...", "topic_type": "project|goal|decision"},
  "snapshot_bindings": {"serving": null, "personal": null, "external": null, "decision": null},
  "freshness": {"status": "fresh|partial|unavailable", "reason_codes": []},
  "authorities": {},
  "partial": false,
  "limitations": [{"code": "...", "authority": "..."}],
  "data": {},
  "projection_checksum": "..."
}
```

`stale` cannot mean a successful cached page in Phase 41 because no durable page
exists yet. It becomes valid only when Phase 43 has recorded dependency changes. If
a required authority is unavailable, return `partial` or `unavailable`, never a
previous response presented as fresh. A Decision topic cannot be guessed when its
decision authority is unavailable.

### Three exact resolvers, not a general entity graph

| Topic | Deterministic source joins | P0 output |
|---|---|---|
| Project | Personal State assertions with `domain=project` and exact `scope`; explicitly matching decision links. | Current assertions, lifecycle/history refs, related decisions. |
| Goal | Personal State `assertion_kind=goal` plus exact `domain`, `scope`, `predicate`; exact decision links only. | Current goal, historical/conflict refs, related decisions. |
| Decision | Exact recommendation ID from Decision Feedback/Analysis/Pilot reads and their existing chain IDs. | Recommendation, confirmation/action/outcome refs, non-causal limitation. |

`topic.list` only lists P0 keys observed from those explicit fields, in a bounded
stable sort. `topic.backlinks` uses a small fixed relation vocabulary such as
`assertion_matches_topic`, `recommendation_targets_topic`,
`decision_feedback_for_recommendation`. Every backlink needs relation type, source
authority, record ID and snapshot ID (plus checksum when available). Vector
similarity, co-occurrence and LLM inference never become P0 links.

### Evidence references stay references

Topic data should contain bounded typed references:

```text
{authority_id, record_type, record_id, snapshot_id, checksum?, evidence_type?}
```

`intelligence/analysis/evidence.py` already validates exact snapshot/checksum
compatibility. `retrieval/evidence.py` already uses read-only access and privacy /
eligibility checks. Phase 41 defines the reference schema and allowlist; Phase 42
connects the references to an Evidence Drawer. A topic resolver must not retrieve a
full raw transcript merely to build an overview.

### Truth classification is structural

Keep upstream content categories in separately typed sections:

```text
Fact / Observation       -> only authority-labelled current material
Inference / Forecast     -> visibly separate and non-factual
Recommendation           -> link to Decision Workspace, never personal state
Historical / Superseded  -> history, excluded from current conclusion
Conflict                 -> conflict, excluded from current conclusion
External                 -> separately labelled; never a personal fact
```

Metadata-only/privacy-sealed source material remains sealed. The Topic projection
can state evidence availability but cannot expose content just to make a page richer.

### Typed recovery, not exception disclosure

Current Cockpit `_collect` includes `str(exc)` in limitations. A future browser Wiki
endpoint must not expose the same diagnostic detail. Use safe reason codes:

```text
invalid_topic_key
unsupported_topic_type
topic_not_found
authority_unavailable
authority_binding_missing
snapshot_mismatch
evidence_unavailable
privacy_sealed
projection_partial
```

Detailed diagnostics belong only in server logging under existing privacy/retention
policy. The response contains a code, affected authority and retryability—not local
paths, SQL errors or exception text.

## Don't Hand-Roll

| Avoid | Use | Why |
|---|---|---|
| General entity graph/ontology | Three strict P0 key parsers and exact joins | Prevents identity drift and scope explosion. |
| LLM titles, summaries or relations | Deterministic templates | P0 excludes providers and has no Wiki Candidate→Eval→Publish contract. |
| Second fact/external table | Existing authorities | Duplicate facts would violate lifecycle and SSOT. |
| Custom raw evidence reader | Existing two evidence modules | They already enforce read-only, privacy and snapshot/checksum contracts. |
| Semantic backlinks | Explicit relation vocabulary | Similarity is not factual provenance. |
| Browser-side snapshot assembly | Server projection service | Browser joins bypass authority, privacy and partial-failure rules. |
| Durable cache in Phase 41 | On-demand projection/checksum | Materialization/invalidation is separately verified in Phase 43. |

## Common Pitfalls

1. Treating a display title as identity instead of the canonical key.
2. Selecting historical/conflict material as the current conclusion.
3. Reusing old output while calling it fresh when current authority is unavailable.
4. Mixing External Context into a personal fact summary.
5. Generating links from embeddings, co-occurrence or model guesses.
6. Leaking exception strings, paths or SQL details through limitations.
7. Returning unbounded directories, backlinks or evidence lists.
8. Joining records across incompatible snapshots/checksums.
9. Writing projection text to Active KU/Chroma and creating a self-retrieval loop.
10. Treating untracked v1.4 implementation as a stable API rather than revalidating
    after its milestone audit.

## Concrete paths for future implementation

| Concern | Expected path | Responsibility |
|---|---|---|
| Topic service | `src/personal_knowledge/services/topic_projection.py` or an isolated module beside `ui_projection.py` | Key parsing, deterministic reads, envelope/checksum, typed errors; no writes. |
| Stable projection patterns | `src/personal_knowledge/services/ui_projection.py` | Reuse accepted helpers only; do not merge Cockpit/Wiki schema versions. |
| Transport | `src/personal_knowledge/services/api_server.py` | Read route adapter consistent with final v1.4 origin/transport policy. |
| Evidence | `src/personal_knowledge/intelligence/analysis/evidence.py`; `src/personal_knowledge/retrieval/evidence.py` | Exact reference validation and bounded privacy-aware presentation. |
| Unit tests | `tests/unit/test_topic_projection_keys.py` | Grammar/normalization/rejection and relation allowlist. |
| Contract tests | `tests/contract/test_topic_projection.py` | Envelope, snapshots, classification, degradation and side-effect proof. |

Route paths, module placement and frontend schemas must be reconfirmed after v1.4.
This inactive research document does not authorize any of them.

## Verification strategy

Use fixture authorities and read-only connections; no provider, Chroma daemon or
production database is necessary.

### Unit and contract coverage

- Parse every valid Project/Goal/Decision key; reject empty segments, unknown types,
  invalid encoding, extra segments and non-canonical forms.
- For each P0 fixture, assert exact key, authority records, snapshot bindings,
  evidence references and deterministic checksum.
- Assert the schema version is `personal_wiki_projection_v1`, never the Cockpit
  schema by accident.
- Assert current/history/conflict/recommendation/external material is separated;
  historical or conflict data cannot form a current conclusion.
- Assert each backlink is allowlisted and has authority, record ID and snapshot
  binding; assert a semantic-only near match is absent.
- Simulate one authority failure: unaffected sections survive, `partial=true`, and
  the response contains no exception text or local path.
- Simulate privacy sealing and snapshot/checksum mismatch: return safe recovery, no
  raw content and no fabricated page.
- Compare database/index/provider/external-action state before and after every read
  operation. No table/row/watermark/pointer/Chroma/provider/action mutation is valid.

### Transport checks after v1.4 completion

- REST response matches direct service output except generated timestamp.
- Invalid route/key returns typed 4xx JSON, never stack trace or HTML.
- Read routes remain within the accepted v1.4 CORS/origin policy and do not reopen
  cross-origin mutation exposure.

### Required negative proof

Search the final Phase 41 diff and call paths for absent writes:

```text
provider invocation
promotion or active-pointer mutation
knowledge-unit insert/update
Chroma add/upsert/delete
external action
SQLite write transaction
```

## Planning handoff

Future implementation should split into independently verified plans:

1. Canonical TopicKey parser plus typed envelope/error vocabulary.
2. Deterministic Project/Goal/Decision resolvers with bindings, checksums,
   evidence refs and authority-isolated partial behavior.
3. Bounded directory/backlinks plus thin read-only REST adapter.
4. Contract tests, side-effect proof and a mandatory revalidation checkpoint against
   audited v1.4 interfaces.

Do not plan a migration, durable materialization, React route, Evidence Drawer UI,
user notes, vector search, LLM narrative or automatic rebuild in Phase 41.

## Confidence and activation prerequisite

Confidence is **medium**. The repository already has suitable read, snapshot,
evidence and projection patterns, but v1.4 is not complete. Before execution,
re-run this research and contract verification against the audited v1.4 code and its
security result. If real P0 `scope`/`predicate` identities are unstable, exclude the
affected topic type rather than introducing semantic matching.
