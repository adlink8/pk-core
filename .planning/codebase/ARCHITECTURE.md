---
mapped_at: 2026-07-16
last_mapped_commit: ce6dfde1f6d759368077e47288dcfc811f2960b9
focus: architecture
branch: codex/llm-memory-mcp-integration
phases: Phase 20 physical cutover + Phase 21 domains slimming + pk-ku packaging
---

# Architecture Map (Post Phase 20–21 / pk-ku)

Current production shape for the personal knowledge system after:

- **Phase 20** — physical roots `data/` · `var/` · `archive/`; path SSOT in `project_paths`
- **Phase 21** — build/eval moved out of `domains/` into `application/` + `evaluation/`
- **pk-ku packaging** — product CLI surface for incremental knowledge units

Authoritative operating docs: `docs/AGENTS.md`, `docs/architecture/retrieval-ssot.md`, `docs/architecture/domains-slimming.md`, `docs/architecture/repository-zones.md`, `docs/runbooks/product-sync.md`, `docs/runbooks/ku-incremental.md`.  
Machine policies: `governance/policies/architecture.yaml`, `governance/policies/paths.yaml`.

---

## 1. System overview

```text
AgentsView live (RO)          Google raw / structured          imports/
        │                              │                          │
        │ pk-sync conversations        │ Google lifecycle         │ intake
        ▼                              ▼                          ▼
agent_conversations.sqlite      google_data.sqlite         data/imports/
  (dialogue SSOT)               (non-dialogue raw +
                                 light assertions)
        │
        │  pk-ku inspect → prepare(delta) → extract(ir_*)
        │  → extract-gate → canonical → publish
        │  → vector(candidate) → eval → promote
        ▼
canonical_knowledge_units  +  Chroma active collection
  (in var/db/personal_system.sqlite)   (pointer: knowledge_index_active.txt)
        │
        ▼
unified_search (layered hybrid: KU → dialogue → Google PE → optional legacy pad)
        │
        ├── rag-search (CLI)
        ├── rag-api  :8000  (REST)
        ├── rag-mcp  stdio  / apps MCP :8789
        └── tunnel   :8081 → ChatGPT
```

**Core value:** personal history → evidence-backed, queryable knowledge with promote/rollback.  
**Not knowledge SSOT:** `memory_*` experiment tables, raw `personal_events` as dialogue substitute, AgentsView insights.

---

## 2. SSOT layers

| Layer | Authority name | Physical / logical surface | Notes |
|-------|----------------|----------------------------|-------|
| **Dialogue evidence** | Canonical conversation | `data/canonical/agent/structured/db/agent_conversations.sqlite` | Message-level evidence; built by `pk-sync conversations` |
| **Dialogue upstream (live)** | AgentsView | `%USERPROFILE%\.agentsview\sessions.db` | **protected-external**; open RO only; never relocate / never write |
| **Dialogue normalized snapshot** | AgentsView adapter output | `data/canonical/agent/structured/db/agentsview_normalized.sqlite` | Intermediate publish target of sync |
| **Knowledge** | KU + active index | SQLite `canonical_knowledge_units` in `var/db/personal_system.sqlite` + active Chroma collection named in `var/db/knowledge_index_active.txt` | Product knowledge SSOT |
| **Non-dialogue raw (PE / transition)** | personal_events / unified_events | `var/db/personal_system.sqlite` | Google-preferred PE for hybrid; **not** dialogue gold |
| **Google light structure** | activities + light assertions | `data/canonical/google/structured/db/google_data.sqlite`; active run pointer `google_structure_active_run.txt` | Not knowledge units; `not_knowledge_unit: true` |
| **Memory** | Experimental only | memory tables / graph in unified DB | Phase 08 cancelled as product SSOT; still queryable via MCP/API |
| **Vector indexes** | Retrieval features | Chroma (KU port typically 8001) | Not fact SSOT; active via pointer file |
| **Path resolution** | `project_paths` | `src/personal_knowledge/core/project_paths.py` | Prefer Phase 20 paths; optional legacy fallback via `_prefer` |

Status API (`get_knowledge_status()` in `retrieval/semantic_search.py`) exposes:

```json
{
  "ssot": {
    "dialogue": "agentsview_canonical",
    "knowledge": "canonical_knowledge_units",
    "non_dialogue_raw": "personal_events"
  },
  "fallback_policy": "layered",
  "allow_legacy_pad": true
}
```

### Hybrid retrieval order (default `layered`)

Implemented by `search_knowledge_units` / shared backend used by CLI · REST · MCP:

1. **Active KU collection** (knowledge-first)
2. **Dialogue** — canonical messages (lexical/snippet) + `conversation_turns` vectors
3. **Google personal_events** (`source=Google`)
4. **Optional legacy_pad** — non-Google PE fill (`allow_legacy_pad`; env `PERSONAL_DATA_ALLOW_LEGACY_PAD`)

`fallback_policy=legacy` restores full PE pad after KU (forensics / A-B only).  
Env: `PERSONAL_DATA_FALLBACK_POLICY`.

---

## 3. Package layers (dependency direction)

Policy: `governance/policies/architecture.yaml`. Enforced direction:

```text
delivery (services, apps)
    → application (orchestration, product CLIs)
        → domains (rules/constants only; facades until 2026-08-13)
            → foundation (core)
    → infrastructure (adapters, retrieval)
evaluation → public domain/retrieval/application contracts
             (must not silently promote active pointer)
```

| Layer | Path | Role |
|-------|------|------|
| Foundation | `src/personal_knowledge/core/` | Paths, privacy_guard, llm client+retry, chroma_client, embed, rules, runtime_config, conversation_repository |
| Infrastructure | `src/personal_knowledge/adapters/` | AgentsView RO adapter, Google activities adapter, base ports |
| Infrastructure | `src/personal_knowledge/retrieval/` | Unified search facade + semantic/events/memory/google modules; vector build/search |
| Domain (target) | `src/personal_knowledge/domains/{conversation,graph,knowledge,memory}/` | Rules/models/constants; **build/eval are re-export facades** until **2026-08-13** |
| Application | `src/personal_knowledge/application/` | Canonical build/lifecycle; product entries `sync.py`, `ku.py` |
| Evaluation | `src/personal_knowledge/evaluation/` | Gates, canary, RAG eval, vector compare, reports; does not write active pointer |
| Delivery | `src/personal_knowledge/services/` | REST (`api_server`), MCP stdio (`mcp_server`), dashboard |
| Delivery | `apps/personal_data_chatgpt/` | HTTP MCP Apps + widgets + tunnel start scripts |
| Control | `src/personal_knowledge/governance/` + `governance/` | Preflight, manifests, path/architecture policies |

**Forbidden:** domain → services; core → domain; evaluation silently mutating `knowledge_index_active.txt`; product imports of quarantine/archive as source.

---

## 4. Domains facades vs application / evaluation

**Note (hard):** `domains/*` build/eval modules are **facades** until **2026-08-13**. New code must import `application.*` / `evaluation.*` / `core.llm`, not `domains.*` facades.

### Pattern

Each former domain orchestration module is typically:

```python
# domains/<sub>/build_*.py  (or evaluate_*.py)
_canonical = _import_module("personal_knowledge.application.<sub>.…")
_sys.modules[__name__] = _canonical
```

Same for evaluation targets under `evaluation/`. Retrieval eval shims:

- `retrieval/evaluate_vector_*.py`, `compare_*_generations.py` → `evaluation/vector/*`

### Canonical homes after Phase 21

| Concern | Application (build/lifecycle) | Evaluation |
|---------|-------------------------------|------------|
| Conversation | `application/conversation/` (normalized, canonical, graph, summary, segments, vector store) | `evaluation/conversation/` |
| Knowledge | `application/knowledge/` (refresh, prod extract, canonical, publish, vector, promote, rollback) | `evaluation/knowledge/` (extract gate, canary, RAG) |
| Graph | `application/graph/` | `evaluation/graph/` |
| Memory | `application/memory/` (experimental lifecycle) | `evaluation/memory/` |
| Vector generations | (build lives under knowledge/retrieval) | `evaluation/vector/` |
| Cross-cutting product | `application/sync.py`, `application/ku.py`, Google light builders | `evaluation/gate_knowledge_candidate.py`, `run_knowledge_eval.py`, … |

### Sole non-facade domain logic retained

- `domains/knowledge/migrate_add_knowledge_unit_tables.py` — `SCHEMA_SQL` DDL constant

### Product vs retired entries

| Entry | Status | Module |
|-------|--------|--------|
| **`pk-sync`** | **Product** | `cli:sync` → `application/sync.py` |
| **`pk-ku`** | **Product** | `cli:ku` → `application/ku.py` |
| `rag-search` / `rag-api` / `rag-mcp` / `rag-dashboard` | Product serve/search | `cli` → retrieval / services |
| `rag-pipeline` | **Retired** (exit 2) | Needs `PK_ALLOW_LEGACY_PIPELINE=1` + `--legacy-integrated` |

Console scripts: `pyproject.toml` `[project.scripts]` / `src/personal_knowledge.egg-info/entry_points.txt`.

---

## 5. Request flows

### 5.1 `pk-sync` — conversation SSOT update

**Entry:** `pk-sync` → `personal_knowledge.cli:sync` → `application/sync.py`

| Command | Behavior |
|---------|----------|
| `pk-sync conversations` | Dry-run AgentsView → normalized → canonical |
| `pk-sync conversations --write` | Publish `agentsview_normalized.sqlite` + `agent_conversations.sqlite` |
| `pk-sync help-legacy` | Print emergency legacy pipeline notes |

**Implementation path:** `sync._cmd_conversations` → `application.run_pipeline.run_agentsview_stage(write=…)`.

**Does not:** run integrated personal_events/memory batch; does not extract KU.

**Runbook:** `docs/runbooks/product-sync.md`.

```text
~/.agentsview/sessions.db (RO adapter)
        → agentsview_normalized.sqlite
        → agent_conversations.sqlite   ◄── dialogue SSOT
```

### 5.2 `pk-ku` — incremental knowledge units

**Entry:** `pk-ku` → `personal_knowledge.cli:ku` → `application/ku.py`

| Subcommand | Delegates to | Side effects |
|------------|--------------|--------------|
| `workflow` | prints daily flow | none |
| `inspect` | `application.knowledge.refresh_knowledge_units --inspect` | free delta report |
| `prepare` | `refresh_knowledge_units --prepare` + policy flags | freezes delta inventory / `ir_*` run; no LLM |
| `extract --run ir_*` | `build_knowledge_units_prod --resume` | paid LLM; rejects non-`ir_*` unless `PK_KU_ALLOW_NON_INCREMENTAL_RUN=1` |
| `status --run` | `build_knowledge_units_prod --status` | ledger stats |
| `extract-gate` | `evaluation.knowledge.evaluate_knowledge_unit_extraction` | gate row |
| `canonical --run [--write]` | `build_canonical_knowledge_units` | staging/canonical units |
| `publish --run [--write]` | `publish_incremental_run` | additive staging → current (**no** demote of other runs) |
| `vector [--write]` | `build_knowledge_unit_vector_store` | **candidate** collection only |
| `promote` | `promote_knowledge_index` | active pointer (last; prefer `--require-eval-pass`) |

**Daily order (hard):**

```text
1) pk-sync conversations [--write]     # if dialogue grew
2) pk-ku inspect
3) pk-ku prepare --model … --provider … --endpoint … --auth-mode …
4) pk-ku extract --run <fresh_run_id>  # only if extract_item_count > 0
5) pk-ku extract-gate --run … [--min-yield 0.7]
6) pk-ku canonical --run … --write
7) pk-ku publish --run … --write
8) pk-ku vector --write
9) canary/eval → pk-ku promote --collection … --require-eval-pass …
```

**Hard rules:**

- Daily extract is **delta only** (prepare queue / watermark); not full inventory.
- If `inspect` shows delta but `prepare` is `no_op` → **STOP** (prepare defect); do not invent full inventory path.
- Forbidden daily: `build_knowledge_inventory --write` + `build_knowledge_units_prod --start` on full ledger.
- Policy via CLI flags only; do not edit application code for daily ops.
- Full inventory remains under underlying modules for planned backfill only (not exposed on `pk-ku`).

**Runbook:** `docs/runbooks/ku-incremental.md`.  
**Artifact default:** `var/reports/analysis/ai_context/knowledge_incremental_delta.json`.

### 5.3 `rag-search` — unified retrieval CLI

**Entry:** `rag-search` → `cli:search` → `retrieval.unified_search._cli`

Public API re-exported from facade modules:

| Module | Responsibility |
|--------|----------------|
| `retrieval/semantic_search.py` | `search_semantic`, `search_knowledge_units`, `get_knowledge_status` |
| `retrieval/google_assertions.py` | Google light assertion list/get |
| `retrieval/events_query.py` | Structured event query + data-access contracts |
| `retrieval/memory.py` | Memory list/graph/review contracts |
| `retrieval/merge_cluster.py` | Merge layer / cluster |
| `retrieval/search_vectors.py` | Low-level Chroma search |

Typical subcommands: `semantic`, `knowledge`, `query`, `detail`, `stats`, `memory`, `cluster`, …

### 5.4 REST + MCP + tunnel

| Surface | Port / mode | Process | Backend |
|---------|-------------|---------|---------|
| REST API | `127.0.0.1:8000` | `rag-api` / `services/api_server.py` | `import retrieval.unified_search as backend` |
| MCP Apps (ChatGPT) | `127.0.0.1:8789` · `/mcp` | Node `apps/personal_data_chatgpt/server.mjs` | HTTP to REST |
| MCP stdio | stdio | `rag-mcp` / `services/mcp_server.py` | contracts + loopback REST for semantic |
| Tunnel | `127.0.0.1:8081` | `tunnel-client.exe` (start scripts) | bridges ChatGPT control plane → MCP |

**Start (keep PowerShell window open):**

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File "apps\personal_data_chatgpt\scripts\start-services.ps1"
```

**Health:**

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8789/health
http://127.0.0.1:8081/healthz
```

REST routes (representative): `/health`, `/stats`, `/knowledge`, `/search/semantic`, `/search/query`, `/google/assertions`, `/memory`, `/event/<id>`, `/profile`.  
Privacy: outbound JSON via `core.privacy_guard`.  
MCP profile: `PERSONAL_DATA_MCP_PROFILE=core|full` (default core).  
Tunnel proxy typically `http://127.0.0.1:7897`; REST/MCP localhost-only; ensure `NO_PROXY` includes localhost.

Promote/rollback **never** go through REST/MCP — only via `pk-ku promote` / `promote_knowledge_index` / `rollback_knowledge_checkpoint`.

---

## 6. Promote and active pointer

### Active pointer

| Item | Path |
|------|------|
| Active collection name | `var/db/knowledge_index_active.txt` (`project_paths.KNOWLEDGE_ACTIVE_POINTER` / `DB_DIR`) |
| Promote journal | `var/db/knowledge_index_promote_log.jsonl` |
| Index version table | `knowledge_index_versions` in `var/db/personal_system.sqlite` |
| Implementation | `application/knowledge/promote_knowledge_index.py` |
| Rollback | `application/knowledge/rollback_knowledge_checkpoint.py` |

### Promote algorithm (summary)

1. Read previous active from pointer file.
2. Optionally enforce eval gate (`--require-eval-pass` + summary/gate paths).
3. Compute Chroma collection ID-set checksum.
4. Update SQLite version statuses (old active → rolled_back; candidate → active).
5. Atomic write: `*.tmp` → `knowledge_index_active.txt`.
6. Append JSONL promote log.

### Lifecycle separation

| Stage | Touches active? |
|-------|-----------------|
| `pk-ku vector` | **No** — candidate only |
| `pk-ku publish` | SQLite staging→current units only; **not** Chroma active |
| `pk-ku promote` | **Yes** — flips active pointer last |
| Mid-run promote | **Forbidden** |

Retrieval always resolves active via `_read_knowledge_active_collection()` in `semantic_search.py`.

### Google structure pointer (separate)

- `data/canonical/google/structured/db/google_structure_active_run.txt`
- Lifecycle: `application/build_google_normalized_events`, `build_google_light_assertions`, `google_structure_lifecycle`

---

## 7. Data plane (Phase 20)

| Zone | Primary paths | Mutability |
|------|---------------|------------|
| Private data | `data/raw/`, `data/staging/`, `data/canonical/`, `data/imports/` | private; adapters only |
| Runtime DB / reports | `var/db/`, `var/runtime/`, `var/reports/`, `var/logs/`, `var/cache/`, `var/phase20-journals/` | generated / gitignored |
| Archive | `archive/quarantine/`, `archive/planning/`, `archive/vendor-reference/` | read-only / retention |
| Cutover backups | `*.bak-phase20` at root (`Agent.bak-phase20`, `Google.bak-phase20`, …) | recovery window only |

Key DBs:

| DB | Role |
|----|------|
| `data/canonical/agent/structured/db/agent_conversations.sqlite` | Dialogue SSOT |
| `data/canonical/agent/structured/db/agentsview_normalized.sqlite` | Sync intermediate |
| `data/canonical/agent/structured/db/agent_data.sqlite` | Agent structured |
| `data/canonical/google/structured/db/google_data.sqlite` | Google activities / light |
| `var/db/personal_system.sqlite` | Unified KU + PE + memory + index versions |
| `var/db/conversation_graph.duckdb` | Conversation graph |
| `var/db/evaluation_registry.sqlite` | Eval registry |

Path resolution always through `personal_knowledge.core.project_paths` (never hard-coded `parents[N]` in new code).

---

## 8. Evaluation plane

- Canonical suites under `src/personal_knowledge/evaluation/`
- Public/synthetic assets: `assets/evals/knowledge_units/`, prompts under `assets/prompts/`
- Private frozen suites may still resolve via `project_paths.KNOWLEDGE_EVAL_DIR` (prefer content under `integration/evals` or `assets/evals` when present)
- Reports: `var/reports/analysis/evaluations/`, `var/reports/analysis/ai_context/`
- Gate principle: evaluation **reads candidates**; promotion is an explicit application command after gate pass

---

## 9. Compatibility residue

| Residue | Role | Status |
|---------|------|--------|
| `domains/*/build_*.py`, `evaluate_*.py` | re-export facades | remove after **2026-08-13** |
| `retrieval/evaluate_*`, `compare_*` | facades → `evaluation/vector` | same window |
| `integration/scripts/` | mostly `__pycache__` + residual governance scripts | not product path; prefer `src/` |
| `tools/compat/v1_1/` | flat module-name compatibility shims | forensics / old import paths |
| `rag-pipeline` / `run_pipeline` integrated steps | retired product path | flag-gated forensics only |
| `*.bak-phase20` | Phase 20 recovery trees | do not treat as live SSOT |

---

## 10. Quick reference — product commands

| Intent | Command |
|--------|---------|
| Sync dialogue (dry) | `pk-sync conversations` |
| Sync dialogue (write) | `pk-sync conversations --write` |
| KU workflow help | `pk-ku workflow` |
| KU delta inspect | `pk-ku inspect` |
| KU prepare (no LLM) | `pk-ku prepare --model … --provider … --endpoint … --auth-mode …` |
| KU extract | `pk-ku extract --run ir_…` |
| KU promote | `pk-ku promote --collection … --require-eval-pass …` |
| Search | `rag-search semantic "…" --top-k 5` |
| Knowledge status | `rag-search knowledge --json` |
| REST | `rag-api` (or start-services.ps1) |
| Serve stack | `apps\personal_data_chatgpt\scripts\start-services.ps1` |

---

## 11. Related maps

| Doc | Content |
|-----|---------|
| `STRUCTURE.md` (this directory) | Top-level and package tree |
| `docs/architecture/retrieval-ssot.md` | Three-layer SSOT + hybrid telemetry |
| `docs/architecture/domains-slimming.md` | Phase 21 layout |
| `docs/architecture/repository-zones.md` | Physical zones |
| `.planning/phases/20-physical-data-runtime-relocation/` | Phase 20 cutover |
| `.planning/phases/21-architectural-alignment-domains-slimming/` | Phase 21 |
| `docs/runbooks/ku-incremental.md` | Operator KU procedure |
| `docs/runbooks/product-sync.md` | Operator sync procedure |
