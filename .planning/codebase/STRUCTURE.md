---
mapped_at: 2026-07-16
last_mapped_commit: ce6dfde1f6d759368077e47288dcfc811f2960b9
focus: architecture
branch: codex/llm-memory-mcp-integration
phases: Phase 20 physical cutover + Phase 21 domains slimming + pk-ku packaging
---

# Repository Structure Map (Post Phase 20–21)

Physical and package layout after Phase 20 (`data/` / `var/` / `archive/`) and Phase 21 (`application/` + `evaluation/` as canonical build/eval homes; `domains/*` facades until **2026-08-13**).

Path SSOT: `src/personal_knowledge/core/project_paths.py`.  
Zone policy: `governance/policies/paths.yaml`, `docs/architecture/repository-zones.md`.

---

## 1. Top-level directories

| Path | Zone / class | Role |
|------|--------------|------|
| `src/` | src | Product Python package `personal_knowledge` |
| `apps/` | src | Deployable Node MCP Apps + service start scripts |
| `tools/` | src | Forensics, migrations, supported analytics, compat shims |
| `tests/` | tests | unit / contract / integration / e2e / governance |
| `assets/` | assets | Versioned prompts, public/synthetic evals, vendored JS/CSS |
| `docs/` | docs | Agent manual, architecture, runbooks |
| `governance/` | governance | Policies, manifests, schemas, baselines, reports |
| `data/` | data | Private personal data (raw / canonical / imports) — **gitignored content** |
| `var/` | var | DB, runtime, reports, logs, cache, phase20 journals |
| `archive/` | archive | Quarantine, old planning, vendor-reference |
| `integration/` | mixed residual | Compat scripts cache, evals, empty/legacy shells after Phase 20 |
| `.planning/` | planning | GSD roadmap, phases, codebase maps (this file) |
| `*.bak-phase20/` | recovery | Phase 20 cutover backups (not live SSOT) |
| Root config | control | `pyproject.toml`, `requirements*.txt`, `pytest.ini`, `AGENTS.md`, `README.md`, `LOCATION.md` |

### Product console scripts (`pyproject.toml`)

| Script | Entry |
|--------|-------|
| `pk-sync` | `personal_knowledge.cli:sync` |
| `pk-ku` | `personal_knowledge.cli:ku` |
| `rag-search` | `personal_knowledge.cli:search` |
| `rag-api` | `personal_knowledge.cli:api` |
| `rag-mcp` | `personal_knowledge.cli:mcp` |
| `rag-dashboard` | `personal_knowledge.cli:dashboard` |
| `rag-pipeline` | `personal_knowledge.cli:pipeline` (**retired**) |

---

## 2. `src/personal_knowledge/` layout

```text
src/personal_knowledge/
├── cli.py                    # console entry wiring (pk-sync, pk-ku, rag-*)
├── core/                     # foundation
│   ├── project_paths.py      # ROOT, DATA_*, VAR_*, AGENT_CONVERSATIONS_DB, KNOWLEDGE_ACTIVE_POINTER
│   ├── llm.py                # OpenAI-compatible client + retry (Phase 21)
│   ├── privacy_guard.py
│   ├── chroma_client.py
│   ├── local_embed.py
│   ├── conversation_repository.py
│   ├── common.py, rules.py, runtime_config.py, memory_governance.py
│   └── _verify.py
├── adapters/                 # infrastructure — source ports
│   ├── agentsview.py         # RO AgentsView
│   ├── google_activities.py
│   └── base.py
├── application/              # CANONICAL build / lifecycle / product CLIs
│   ├── sync.py               # pk-sync
│   ├── ku.py                 # pk-ku
│   ├── run_pipeline.py       # agentsview stage + retired integrated
│   ├── run_import_pipeline.py
│   ├── conversation/         # normalize, canonical, graph, summary, vectors
│   ├── knowledge/            # inventory, extract, canonical, publish, vector, promote
│   ├── graph/
│   ├── memory/               # experimental memory lifecycle
│   ├── build_google_*.py, google_structure_lifecycle.py
│   └── build_context_doc.py, build_deep_profiles.py, …
├── evaluation/               # CANONICAL eval / gates / reports
│   ├── conversation/
│   ├── knowledge/            # extract-gate, canary, RAG eval
│   ├── graph/
│   ├── memory/
│   ├── vector/               # evaluate/compare vector & sqlite generations
│   ├── gate_knowledge_candidate.py, run_knowledge_eval.py
│   └── eval_contracts.py, eval_registry.py, …
├── domains/                  # facades (+ SCHEMA_SQL) until 2026-08-13
│   ├── conversation/         # re-exports → application/evaluation
│   ├── knowledge/            # facades; migrate_add_knowledge_unit_tables keeps SCHEMA_SQL
│   ├── graph/
│   └── memory/
├── retrieval/                # unified search + vector I/O
│   ├── unified_search.py     # public facade + _cli
│   ├── semantic_search.py    # hybrid KU search + knowledge status
│   ├── search_vectors.py, build_vector_store.py
│   ├── events_query.py, google_assertions.py, memory.py, merge_cluster.py
│   ├── _constants.py, _db_utils.py
│   └── evaluate_* / compare_*  # facades → evaluation/vector
├── services/                 # delivery
│   ├── api_server.py         # REST :8000
│   ├── mcp_server.py         # MCP stdio
│   └── dashboard.py
└── governance/               # in-package control helpers
    ├── preflight.py
    ├── apply_*_migration.py, source_manifest.py, …
```

### Layer rule (import)

| Need | Import |
|------|--------|
| Paths / LLM / privacy | `personal_knowledge.core.*` |
| Build / lifecycle | `personal_knowledge.application.<sub>.*` |
| Eval / gate / compare | `personal_knowledge.evaluation.<sub>.*` |
| Search contracts | `personal_knowledge.retrieval.*` |
| Legacy domain path | `personal_knowledge.domains.*` (facade only; expire 2026-08-13) |
| KU schema DDL | `personal_knowledge.domains.knowledge.migrate_add_knowledge_unit_tables` |

---

## 3. `data/` — private personal data

```text
data/
├── README.md
├── raw/
│   └── google/Takeout/…          # immutable Takeout-style exports
├── staging/                      # reserved intermediate import staging
├── canonical/
│   ├── agent/
│   │   ├── README.md
│   │   └── structured/db/
│   │       ├── agent_conversations.sqlite     # DIALOGUE SSOT
│   │       ├── agentsview_normalized.sqlite
│   │       ├── agent_data.sqlite
│   │       └── backups/
│   └── google/
│       ├── structured/
│       │   ├── db/
│       │   │   ├── google_data.sqlite
│       │   │   ├── google_structure_active_run.txt
│       │   │   └── google_data_schema.sql
│       │   ├── by_service/, by_topic/, details_csv/, raw_index/
│       │   └── scripts/                     # historical Google helper scripts
│       └── _shell/…                         # residual shell / bak trees
└── imports/
    ├── README.md
    ├── batches/                             # dated intake batches
    ├── incoming/{agent,google,gpt}/
    └── duplicate_audit/                     # quarantine + manifests
```

**External protected (not under `data/`):**

- `%USERPROFILE%\.agentsview\sessions.db` — live WAL; RO only; never relocate

**Phase 20 legacy → current:**

| Legacy | Current |
|--------|---------|
| `Agent/structured/db/` | `data/canonical/agent/structured/db/` |
| `Google/raw/` | `data/raw/google/` |
| `Google/structured/` | `data/canonical/google/structured/` |
| `imports/` | `data/imports/` |

---

## 4. `var/` — runtime / DB / reports

```text
var/
├── README.md
├── db/
│   ├── personal_system.sqlite               # unified KU + PE + memory + versions
│   ├── knowledge_index_active.txt           # ACTIVE CHROMA COLLECTION POINTER
│   ├── knowledge_index_promote_log.jsonl
│   ├── conversation_graph.duckdb
│   ├── evaluation_registry.sqlite
│   ├── conversation_source.txt (+ rollback log)
│   ├── structured/                          # unified CSV exports
│   ├── raw_index/
│   └── backups/
├── runtime/
│   ├── governance/                          # preflight / inventory artifacts
│   └── private_evals/
├── reports/
│   ├── analysis/
│   │   ├── ai_context/                      # KU deltas, eval JSON, charts
│   │   ├── evaluations/                     # knowledge eval HTML/JSON runs
│   │   ├── stage1_profile/
│   │   └── refactoring/
│   └── classification_summary.json
├── logs/                                    # api, mcp, tunnel, workers
├── cache/pytest/
└── phase20-journals/                        # migration journals (stay outside moved trees)
```

**Phase 20 legacy → current:**

| Legacy | Current |
|--------|---------|
| `integration/db/` | `var/db/` |
| `integration/runtime/` | `var/runtime/` |
| `integration/analysis/` | `var/reports/analysis/` |
| `logs/` | `var/logs/` |

---

## 5. `apps/`

```text
apps/
├── README.md
└── personal_data_chatgpt/
    ├── package.json / package-lock.json
    ├── server.mjs                 # HTTP MCP Apps on :8789
    ├── public/                    # widgets (memory-graph, data-browser, relation-review)
    ├── scripts/
    │   ├── start-services.ps1     # REST + MCP + tunnel watchdog
    │   ├── 启动服务.bat
    │   └── 停止服务.bat
    ├── logs/
    ├── test/                      # contract + widget tests
    ├── README.md
    └── TUNNEL_RUNBOOK.md
```

Ports: REST **8000**, MCP Apps **8789**, tunnel **8081**.  
Depends on Python `rag-api` backend.

---

## 6. `tools/`

```text
tools/
├── compat/v1_1/          # flat-name shims (old integration/scripts module names)
├── forensics/            # one-off audits, phase15 evals, sqlite probes, examples/
├── migrations/           # path/shim fix scripts
└── supported/            # maintained ancillary analytics (gap analysis, L1/L2 compare)
```

Not product daily path. Prefer `pk-sync` / `pk-ku` / `python -m personal_knowledge.…`.

---

## 7. `integration/` (residual after Phase 20)

Live product code is under `src/`. `integration/` retains:

| Path | Role |
|------|------|
| `integration/README.md` | Cross-source integration narrative + consumer notes |
| `integration/evals/knowledge_units/` | Private frozen query suites (may be preferred by `KNOWLEDGE_EVAL_DIR`) |
| `integration/scripts/` | Mostly `__pycache__` + residual `governance/*.py`; historical domain packages empty of sources |
| `integration/prompts/`, `lib/` | Often empty shells; real assets moved to `assets/` |
| `integration/db/`, `runtime/`, `analysis/` | Prefer empty or thin; live data in `var/` |
| `*.bak-phase20` under integration | Cutover recovery copies |

CLI bootstrap in `cli.py` may still fall back to `integration/scripts` on ModuleNotFoundError for a few legacy names — product entries (`pk-sync`, `pk-ku`) use `src` modules only.

---

## 8. `archive/`

```text
archive/
├── README.md
├── quarantine/
│   ├── _recycle/                 # former _recycle (Agent/Google/GPT raw dumps, etc.)
│   └── desktop-strays-20260713/  # quarantined loose scripts
├── planning/                     # archived GSD / .gsd history
└── vendor-reference/             # e.g. former .ai-bridge clones
```

Read-only / retention-bound. Never import as product source. Never scan into tests by default.

---

## 9. `assets/`, `docs/`, `governance/`, `tests/`

### `assets/`

```text
assets/
├── prompts/                 # versioned prompt packs (KU extractor, memory judges, …)
├── evals/knowledge_units/   # public/synthetic eval contracts
└── vendor/                  # tom-select, vis, bindings
```

### `docs/`

```text
docs/
├── AGENTS.md                # full agent operating manual
├── architecture/
│   ├── retrieval-ssot.md
│   ├── domains-slimming.md
│   └── repository-zones.md
├── runbooks/
│   ├── product-sync.md      # pk-sync
│   ├── ku-incremental.md    # pk-ku
│   ├── dependency-governance.md
│   └── tooling/
└── testing/
```

### `governance/`

```text
governance/
├── policies/                # architecture.yaml, paths.yaml, privacy, retention, …
├── manifests/               # entrypoints, asset classification, migration
├── baselines/               # inventory / storage budgets
├── schema/
├── reports/
└── stable_modules.yaml
```

### `tests/`

```text
tests/
├── unit/
├── contract/
├── integration/
├── e2e/
├── governance/
└── README.md
```

---

## 10. `.planning/`

```text
.planning/
├── PROJECT.md, REQUIREMENTS.md, ROADMAP.md, STATE.md
├── config.json
├── codebase/                # ARCHITECTURE, STRUCTURE, STACK, TESTING, …
├── intel/                   # synthesis / constraints
├── phases/
│   ├── 01-… through 20-…
│   └── 21-architectural-alignment-domains-slimming/
└── MILESTONE-*.md, VERIFICATION-*.md
```

Authoritative process state for GSD. Architecture maps live in `codebase/`.

---

## 11. Cutover backups (not live tree)

Root-level recovery only (do not treat as SSOT):

- `Agent.bak-phase20/`, `Google.bak-phase20/`, `imports.bak-phase20/`
- `_recycle.bak-phase20/`, `logs.bak-phase20/`
- `integration/*.bak-phase20`, `integration/db.bak-phase20/`, …

---

## 12. Navigation cheatsheet

| Goal | Go here |
|------|---------|
| Dialogue SSOT file | `data/canonical/agent/structured/db/agent_conversations.sqlite` |
| Active KU pointer | `var/db/knowledge_index_active.txt` |
| Unified DB | `var/db/personal_system.sqlite` |
| Path constants | `src/personal_knowledge/core/project_paths.py` |
| Product sync CLI | `src/personal_knowledge/application/sync.py` |
| Product KU CLI | `src/personal_knowledge/application/ku.py` |
| Promote logic | `src/personal_knowledge/application/knowledge/promote_knowledge_index.py` |
| Hybrid search | `src/personal_knowledge/retrieval/semantic_search.py` |
| REST | `src/personal_knowledge/services/api_server.py` |
| ChatGPT adapter | `apps/personal_data_chatgpt/` |
| KU operator docs | `docs/runbooks/ku-incremental.md` |
| Architecture policy | `governance/policies/architecture.yaml` |

---

## 13. Related map

See `ARCHITECTURE.md` in this directory for SSOT layers, request flows, promote/active pointer, and domains facades vs application/evaluation.
