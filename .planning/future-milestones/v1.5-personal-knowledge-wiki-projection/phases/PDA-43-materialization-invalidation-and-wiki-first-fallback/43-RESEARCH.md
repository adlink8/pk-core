---
phase: 43
name: Materialization, Invalidation and Wiki-first Fallback
status: preplanned_not_active
research_type: implementation
requirements: [WIKI-03]
depends_on: [41, 42]
researched: 2026-07-22
---

# Phase 43 Research — Materialization, Invalidation and Wiki-first Fallback

> **Lifecycle notice:** This is research for a future, inactive v1.5 package. No
> `topic.*` authority, Wiki database, cache, route or invalidation worker is
> currently shipped. Re-check all paths, contracts and v1.4 security results when
> v1.5 is actually activated.

## Research question

How can P0 Project, Goal and Decision topic pages become fast, persistent,
rebuildable materialized projections without becoming a new personal-fact SSOT,
feeding their own summaries into Active KU/Chroma, or hiding upstream changes as
fresh data?

## Current codebase facts

- The composite serving authority already exposes an immutable active snapshot
  through `src/personal_knowledge/retrieval/serving.py::ServingSnapshotResolver`.
  Its snapshot id, manifest hash, members and pointer drift are the canonical
  binding for Active KU retrieval.
- `src/personal_knowledge/retrieval/semantic_search.py::search_knowledge_units`
  is the sole knowledge-first retrieval path. It resolves the active collection
  from the serving snapshot, rejects non-current lifecycle rows, and then uses
  its documented layered raw fallback. A Wiki projection must never be supplied
  as a collection or as a fallback layer to this function.
- Personal State is a snapshot-bound, metadata-only read authority in
  `src/personal_knowledge/intelligence/service.py`; its output includes run and
  snapshot checksums and validates each evidence version before projection.
- External data has a separate immutable snapshot authority in
  `src/personal_knowledge/external_context/snapshots.py`. It verifies member
  checksums, lifecycle heads and watermarks when reads occur; it must remain
  separate from personal facts.
- Decision/analysis/pilot/calibration reads are bounded by
  `src/personal_knowledge/services/decision_intelligence_reads.py`; its `_ro`
  helper uses `mode=ro`, `PRAGMA query_only=ON` and foreign-key enforcement.
- `src/personal_knowledge/retrieval/evidence.py::EvidenceResolver` already gives
  typed, privacy-aware metadata-first evidence drill-down. It is the final
  fallback/detail layer, not a source for a second copied evidence table.
- The in-progress Cockpit projection in
  `src/personal_knowledge/services/ui_projection.py` shows the right envelope
  pattern: `schema_version`, operation, snapshot bindings, freshness,
  authorities, `partial`, limitations and data. It is currently v1.4 work, so
  Phase 43 must consume the **validated v1.4 contract**, not its current WIP
  internals.

## Standard Stack

Use repository primitives; this phase needs no new framework, cache server,
ORM, vector database or background worker.

| Need | Standard component / pattern | Why |
|---|---|---|
| Authoritative KU binding | `ServingSnapshotResolver`, `get_knowledge_status`, `search_knowledge_units` | Keeps Active KU and Chroma selection tied to the composite serving snapshot. |
| Personal/External/Decision reads | Existing `IntelligenceService`, `DecisionIntelligenceReadService`, `DecisionFeedbackService` | Preserves existing privacy, read-only and typed-error contracts. |
| Evidence fallback | `EvidenceResolver.resolve(ref, artifact_type=...)` | Supports typed, metadata-first, privacy-aware drill-down instead of copied source bodies. |
| Deterministic IDs/checksums | Existing canonical JSON/checksum helpers in `application/serving/snapshots.py` and `external_context/schema.py` | Stable ordering and hashing are already established repository conventions. |
| Derived projection persistence | A small SQLite **derived-store** with `connect_rw` for writes and `mode=ro` + `query_only` for reads | Fits the local product and allows atomic version/dependency insertion without creating a serving authority. |
| Browser cache | TanStack Query invalidation only after API returns a changed projection version | Browser cache is presentation-only; it must not decide freshness. |
| Tests | pytest contract/integration tests plus Cockpit Vitest/Playwright tests | Existing project convention for authority contracts and visible stale/partial behavior. |

### Required derived-store boundary

If a persistent store is needed, make it an explicitly non-authoritative runtime
store such as `var/db/personal_wiki_projection.sqlite`, not a table grafted onto
`canonical_knowledge_units`, the serving snapshot tables, External Context, or
the Decision authority. Its records may contain only:

```text
topic_id / topic_type / projection_format_version
projection_version / projection_checksum / generated_at
captured authority snapshot bindings and dependency fingerprints
freshness state / reason codes / invalidation event metadata
explicit dependency references (authority + stable ref + expected version/checksum)
```

It must not store copied personal fact values, external fact bodies, raw message
bodies, provider responses, semantic embeddings, or a replacement evidence
payload. A page can always be deterministically rebuilt from its bindings and
dependencies after this derived store is deleted.

## Architecture Patterns

### 1. Immutable projection version + explicit dependency manifest

Represent a topic version as a derived manifest, not as an editable Wiki
document:

```text
topic key
  -> immutable projection version
       -> source binding manifest
       -> sorted explicit dependency rows
       -> projection checksum
       -> generated/freshness/invalidation metadata
```

An implementation should insert the version row and all dependency rows in one
SQLite transaction. Versions remain immutable. A latest-version pointer, if
needed, is only a derived navigation projection; it cannot rewrite a previous
version or change upstream facts.

For P0, dependencies are explicit stable joins produced by Phase 41/42:

- Project: Personal State key/scope plus linked Project Decision/Outcome ids.
- Goal: Personal State assertion key and selected evidence/version refs.
- Decision: recommendation id, its decision-event sequence/checksum and linked
  Personal/External snapshot ids.

Do **not** infer dependencies from embedding similarity, generated prose or a
generic graph traversal. Those can be later Candidate capabilities, but cannot
be P0 invalidation evidence.

### 2. Read-time binding validation is the invalidation authority

The project contains multiple independent SQLite authorities. Cross-database
triggers and a best-effort background listener would be unreliable and could
incorrectly declare pages fresh. Therefore the primary invalidation mechanism is
deterministic **read-time validation**:

```text
topic request
  -> load latest derived manifest
  -> read current binding/fingerprint from each declared authority
  -> compare exact ids, hashes, lifecycle heads and event sequence/checksum
  -> fresh only if every required dependency still matches
  -> otherwise stale or partial with reason codes
```

Known local write paths may additionally append a derived invalidation event for
better navigation latency, but that event is only an optimization. It must never
replace read-time validation. A missing notifier therefore yields a page that is
conservatively revalidated, not a falsely fresh page.

Use reason codes that explain the failure boundary, for example:

```text
serving_snapshot_changed
personal_snapshot_changed
external_snapshot_changed
decision_sequence_changed
dependency_missing
dependency_lifecycle_changed
dependency_checksum_mismatch
authority_unavailable
projection_record_missing
```

Only a mismatch affecting an explicit dependency invalidates a topic. An
unrelated project/goal/decision must remain fresh; this is both a correctness and
cost requirement.

### 3. Stale, partial and unavailable are different states

Use a conservative state machine:

```text
fresh       all required authority bindings and dependency fingerprints match
stale       required authority is available but one or more captured bindings differ
partial     a non-essential section is unavailable; visible data is explicitly scoped
unavailable required authority cannot be validated or required material is absent
missing     no derived projection version exists for the requested topic
```

`partial` is only valid when the page contract explicitly identifies a non-critical
section and leaves it blank with a limitation. If the current summary depends on
an unavailable Personal State or Decision authority, return `unavailable` for the
summary rather than serving an old version as current.

### 4. Deterministic rebuild; no provider in the rebuild path

A rebuild reads current structured authorities using the same P0 topic resolver
as Phase 41, sorts dependency rows canonically, computes a projection manifest
and checksum, then creates a new immutable derived version. It must:

1. use `mode=ro` / `query_only` reads for upstream authorities;
2. not call a Provider, network source, external action, promotion or lifecycle
   mutation;
3. preserve separate Personal and External sections;
4. return `partial`/`unavailable` rather than manufacturing prose for missing
   information;
5. make the old version inspectable as derived history, never as current fact.

Deleting all derived-store rows and rebuilding from a fixed fixture must produce
the same dependency manifest and checksum. Wall-clock `generated_at` is not part
of the content checksum; otherwise equivalent rebuilds cannot be compared.

### 5. Wiki-first is a constrained read router, not a general RAG replacement

“Wiki-first” applies only after a request resolves to a P0 stable topic key. Its
selection order is:

```text
fresh, fully validated Wiki projection
  -> current structured authority for that topic
  -> Active KU/search through search_knowledge_units
  -> typed raw evidence through EvidenceResolver
```

If the Wiki version is stale, partial for a required answer, unavailable or
missing, the router must disclose that condition and skip to the next authority.
It must not pass the cached page text to `search_knowledge_units`, add it to a
Chroma collection, or use it as independent evidence. The fallback result retains
its own source/snapshot metadata; it is not relabelled as a Wiki result.

Long-tail questions which cannot resolve to a P0 topic bypass Wiki entirely and
use the existing structured/KU/raw path. This prevents a topic page directory
from becoming a misleading second search engine.

## Suggested implementation slices for the later plan

1. **Derived manifest contract and validator** — schema/migration, canonical
   dependency model, snapshot fingerprint readers and pure stale classifier.
2. **Immutable materialize/rebuild service** — source read adapters, atomic
   derived version write, deletion/rebuild support and safe read-only service
   envelope.
3. **Wiki-first router and no-feedback guards** — select fresh projection only,
   structured/KU/evidence fallback telemetry, and tests proving no Wiki text can
   reach KU/Chroma/evidence-authority writes.

Keep a manual rebuild endpoint/CLI explicitly privileged and local if one is
needed. P0 must not introduce a hidden background daemon merely to claim that a
cache is always fresh.

## Don't Hand-Roll

| Problem | Use instead | Reason |
|---|---|---|
| Active vector collection selection | `ServingSnapshotResolver` and `search_knowledge_units` | A second pointer bypasses snapshot integrity and drift checks. |
| Raw message/body retrieval | `EvidenceResolver` | It already enforces type, privacy and metadata-first behavior. |
| Cross-authority fact reconstruction | Existing Personal/External/Decision read services | They own lifecycle, snapshot and error validation. |
| Hashing / stable canonical serialization | Existing canonical JSON/checksum helpers | Custom unordered JSON hashing produces spurious stale state. |
| Browser-side freshness verdict | Server projection validator | Browser state cannot validate SQLite snapshots or lifecycle heads. |
| Semantic backlinks / dependency discovery | Explicit P0 key joins | Similarity and LLM guesses are not evidence-grade relationships. |
| Background automatic regeneration | Explicit deterministic rebuild plus read-time validation | Avoids hidden provider/network work and race-driven false freshness. |
| New vector collection for pages | No vector write at all | Prevents Wiki → KU/Chroma self-retrieval feedback loops. |

## Common Pitfalls

1. **Treating a saved page as evidence.** A cached summary may cite evidence but
   cannot become a new evidence artifact or enter KU extraction.
2. **Using only `generated_at`/TTL.** Time expiry does not detect a corrected
   goal, a changed decision event sequence, an External lifecycle update or a
   serving-snapshot switch. Bind exact fingerprints instead.
3. **Cross-database trigger fantasy.** Personal, External and Decision authorities
   are distinct stores. Do not depend on a trigger in one database to invalidate
   another; validate bindings when serving the page.
4. **Global cache flush after every change.** This hides dependency modelling
   errors, creates needless rebuild cost and violates W-43-02's affected-topic
   boundary.
5. **Returning stale content with a fresh-looking header.** Current summary,
   freshness label and fallback route must agree. A stale version may be shown as
   historical diagnostic context only, never as current state.
6. **Turning `partial` into silent empty success.** Preserve limitations and
   authority status; essential missing inputs make the relevant conclusion
   unavailable.
7. **Hashing timestamps or unordered dictionaries.** Canonicalize sorted
   dependencies and exclude volatile rendering time from the content checksum.
8. **Cache key missing subject scope.** `project:{scope}` / goal predicates and
   decision identifiers must be validated by the Phase 41 parser; never concatenate
   arbitrary user strings into SQL or file paths.
9. **Mixing Personal and External in fallback text.** Route them as separately
   labelled sections even if both contribute to the same topic.
10. **Testing only that a cache reads quickly.** The critical tests are stale
    detection, rebuild equivalence, downstream write fingerprints and degraded
    fallback provenance.

## Concrete repository paths for future implementation

| Responsibility | Existing path to reuse/verify | Likely new, derived-only path |
|---|---|---|
| Topic read envelope | `src/personal_knowledge/services/ui_projection.py`, `src/personal_knowledge/services/api_server.py` | `src/personal_knowledge/services/wiki_projection.py` |
| Topic dependency/rebuild logic | `src/personal_knowledge/retrieval/serving.py`, `src/personal_knowledge/intelligence/service.py`, `src/personal_knowledge/services/decision_intelligence_reads.py` | `src/personal_knowledge/wiki/materialization.py` and `.../invalidation.py` |
| Derived SQLite schema | `src/personal_knowledge/application/knowledge/migrate_add_knowledge_unit_tables.py` as migration convention only | a dedicated Wiki derived-store migration/module; not canonical KU schema |
| Active KU fallback | `src/personal_knowledge/retrieval/semantic_search.py` | no new collection or writer |
| Evidence fallback | `src/personal_knowledge/retrieval/evidence.py` | no copied evidence-body table |
| Frontend cache/render | `apps/personal_decision_cockpit/src/api/client.ts`, TanStack Query page hooks | Wiki route hook using server-provided freshness/version only |
| Contract tests | `tests/contract/test_ui_projection*.py`, `tests/contract/test_evidence_resolver.py` | `tests/contract/test_wiki_materialization.py` |
| Integration tests | `tests/integration/test_serving_snapshot_*.py`, `tests/e2e/test_external_context_authority.py` | `tests/integration/test_wiki_invalidation_and_fallback.py` |

Names above are planning targets, not a claim that any of them exist now.

## Verification plan

### Unit / contract

- P0 dependency parser accepts only canonical Project/Goal/Decision references;
  malformed or cross-topic keys fail closed.
- Equivalent sorted input produces the same projection checksum; changing only
  `generated_at` does not.
- A serving/personal/external/decision fingerprint mismatch yields the correct
  stale reason code; missing required dependency yields unavailable, not fresh.
- Changing one explicit dependency invalidates its topic but not unrelated topics.
- A `partial` result identifies the unavailable section and never returns that
  section as current factual text.
- Source-fingerprint, evidence-ref and projection-checksum tampering is rejected.

### Integration

- Create a disposable authority fixture, materialize a P0 page, change one
  bound lifecycle head or decision sequence, then verify read-time validation
  marks only the affected projection stale.
- Delete the projection database/rows, rebuild from unchanged fixture inputs and
  compare dependency manifest plus checksum. Verify all upstream database
  fingerprints and row counts remain unchanged.
- Verify `fresh Wiki -> structured -> KU -> evidence` route telemetry. For
  stale/partial/missing Wiki, ensure the returned answer names the selected
  fallback authority and preserves its snapshot/evidence binding.
- Monkeypatch or fingerprint all KU/Chroma/evidence-authority writers; materialize,
  invalidate and rebuild must perform zero writes and zero provider/network calls
  outside the dedicated derived store.

### Browser / UAT (after Phase 42 exists)

- Page visibly changes from fresh to stale after a fixture upstream change and
  offers the correct read-only refresh/recovery action.
- A stale page never displays its old current-summary section as current; a
  fallback result is labelled with its actual source.
- Long Chinese text, long ids and a no-result topic remain readable. Refreshing a
  browser query cache never changes the authority freshness verdict.

## Planning conclusion

Phase 43 is feasible without a new data platform. The correct P0 design is a
**small, immutable, derived dependency registry plus server-side read-time
validation**, not an LLM-generated knowledge cache and not another retrieval
index. Its value is stable high-frequency topic context; its safety comes from
never allowing that context to become upstream truth or retrieval evidence.

**Confidence:** high for reuse of existing snapshot/evidence/retrieval patterns;
medium for exact schema and API names because Phase 41/42 and v1.4 are not yet
active or verified and must be rechecked at activation.
