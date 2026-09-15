---
mapped_at: 2026-07-16
last_mapped_commit: ce6dfde1f6d759368077e47288dcfc811f2960b9
focus: quality
branch: codex/llm-memory-mcp-integration
---

# Code Conventions (quality map)

Authoritative sources: `docs/AGENTS.md`, `docs/architecture/domains-slimming.md`,
`docs/architecture/repository-zones.md`, `governance/policies/architecture.yaml`,
`governance/policies/paths.yaml`, `docs/runbooks/ku-incremental.md`,
`docs/runbooks/product-sync.md`.

---

## 1. Package layout and import rules

Product code lives under `src/personal_knowledge/` (editable install via
`pyproject.toml` → package-dir `src`).

### 1.1 Layer map (Phase 21)

| Layer | Path | Role |
|-------|------|------|
| foundation | `src/personal_knowledge/core/` | `project_paths`, `privacy_guard`, **`llm`**, chroma/common helpers |
| infrastructure | `src/personal_knowledge/adapters/`, `src/personal_knowledge/retrieval/` | source adapters (RO); vector/search I/O |
| domain | `src/personal_knowledge/domains/{conversation,graph,knowledge,memory}/` | rules, models, constants only (+ temporary facades) |
| application | `src/personal_knowledge/application/{conversation,graph,knowledge,memory}/` | **canonical** build / lifecycle orchestration |
| evaluation | `src/personal_knowledge/evaluation/{conversation,graph,knowledge,memory,vector}/` | **canonical** eval / compare / audit / gates |
| delivery | `src/personal_knowledge/services/` | REST (`api_server`), MCP stdio, dashboard |
| control | `src/personal_knowledge/governance/`, `governance/` | preflight, manifests, sanitized baselines |

Machine-readable import matrix: `governance/policies/architecture.yaml`
(`modules.*.may_import`, `forbidden`).

### 1.2 Where to import (new code)

| Need | Import from |
|------|-------------|
| Paths / privacy / LLM client | `personal_knowledge.core.project_paths`, `…privacy_guard`, **`personal_knowledge.core.llm`** |
| Build / lifecycle / product CLI | `personal_knowledge.application.<subdomain>.…` or `application.sync` / `application.ku` |
| Eval / compare / gate / report | `personal_knowledge.evaluation.<subdomain\|vector>.…` |
| Search / vector I/O (non-eval) | `personal_knowledge.retrieval.…` |
| REST / MCP | `personal_knowledge.services.…` |
| Source adapters | `personal_knowledge.adapters.…` |
| KU schema DDL constant | `personal_knowledge.domains.knowledge.migrate_add_knowledge_unit_tables` (`SCHEMA_SQL`) |
| Legacy facade only | `personal_knowledge.domains.<subdomain>.…` or `retrieval.evaluate_*` — **do not use in new code** |

### 1.3 Facades (cleanup window through **2026-08-13**)

Former orchestration modules under `domains/*/` and retrieval eval scripts are
re-export facades:

```text
# pattern in e.g. domains/knowledge/build_knowledge_units.py
# and retrieval/evaluate_vector_collections.py
_canonical = _import_module("personal_knowledge.application…|evaluation…")
sys.modules[__name__] = _canonical
```

Rules:

- **Do not add logic** to facade files.
- **New imports** go to `application.*` / `evaluation.*` / `core.llm`.
- Sole non-facade domain logic retained:
  `domains/knowledge/migrate_add_knowledge_unit_tables.py`.
- `domains` must **not** import `application` as a peer dependency for new
  domain rules (facades only re-export). Evaluation must **not** silently promote
  active indexes.

### 1.4 Forbidden import directions

From `governance/policies/architecture.yaml` and
`docs/architecture/repository-zones.md`:

- delivery → application → domain → foundation (allowed direction).
- `core` must not import conversation/knowledge/memory/graph/vector/pipeline/services/evaluation.
- domain packages must not import `pipeline` (application root), `services`, or `evaluation`.
- evaluation may read public domain/retrieval contracts; **cannot silently promote**.
- product source must not import `_tools`, raw private data trees, runtime outputs,
  quarantine (`archive/`), or archived planning as import sources.
- Prefer `personal_knowledge.core.project_paths` over ad-hoc `Path(__file__).parents[N]`
  or hardcoded user paths.

### 1.5 Module run form

```powershell
python -m personal_knowledge.application.conversation.summary --dry-run
python -m personal_knowledge.application.knowledge.refresh_knowledge_units --help
python -m personal_knowledge.evaluation.vector.evaluate_vector_collections
python -m personal_knowledge.evaluation.knowledge.evaluate_knowledge_canary
python -m personal_knowledge.governance.preflight --ci
```

Console scripts (`pyproject.toml` `[project.scripts]`): `pk-sync`, `pk-ku`,
`rag-search`, `rag-api`, `rag-mcp`, `rag-dashboard`; `rag-pipeline` is **retired**.

---

## 2. CLI-first ops (no code edits for daily policy)

### 2.1 Product CLIs

| Intent | Command | Notes |
|--------|---------|-------|
| Conversation SSOT | `pk-sync conversations` / `--write` | dry-run default; never writes AgentsView live DB |
| Knowledge units | **`pk-ku`** | `inspect` → `prepare` → `extract` → `extract-gate` → `canonical` → `publish` → `vector` → `promote` |
| Flow help | `pk-ku workflow` | prints allowed vs forbidden path |
| Search | `rag-search …` | local retrieval CLI |
| Legacy integrated batch | blocked | needs `PK_ALLOW_LEGACY_PIPELINE=1` + `--legacy-integrated` |

Runbooks:

- `docs/runbooks/product-sync.md`
- `docs/runbooks/ku-incremental.md` (hard rules)

### 2.2 Policy via flags, not code

Daily / after chat growth:

1. Adjust **flags** on `pk-ku prepare` / `extract` (`--since`, `--roles`,
   `--max-extract-items`, `--model`, …) — see `pk-ku prepare -h`.
2. **Do not** edit `application/knowledge/*` or eval policy YAML for routine
   batching, filters, or one-off scope.
3. Eval policy thresholds (`assets/evals/knowledge_units/eval_policy_v1.yaml`)
   require a **new policy version** if changed; never rewrite old run verdicts.

### 2.3 KU hard rules (operators / agents)

| Rule | Detail |
|------|--------|
| Delta only | Extract new/modified evidence; not full eligible set |
| inspect ≠ prepare | If inspect has delta but prepare is `no_op` → **STOP**; report prepare defect; do not invent full-inventory workaround |
| Forbidden daily | `build_knowledge_inventory --write` + `build_knowledge_units_prod --start` on full ledger |
| No auto paid LLM | `refresh --write` prints approval commands; does not auto-call Vertex |
| Active last | Never promote mid-run; prefer `--require-eval-pass` |
| No `rag-pipeline` for KU | Wrong layer; product path is `pk-ku` |

### 2.4 Write-path lifecycle pattern

Production mutations follow:

```text
stage → validate / gate → promote → journal → rollback
```

- Default **dry-run**; `--write` / promote is explicit.
- Candidate vector indexes never touch active pointer
  (`var/db/knowledge_index_active.txt`) until promote.
- Evaluation gates read candidates; promote is a separate command.

---

## 3. Naming

| Kind | Convention | Examples |
|------|------------|----------|
| Modules / functions | `snake_case` | `build_canonical_knowledge_units`, `refresh_knowledge_units` |
| Classes | `PascalCase` | test classes `TestMemoryDecomplexityPlan` |
| Constants | `UPPER_SNAKE_CASE` | `SCHEMA_SQL`, path constants in `project_paths` |
| Build orchestration modules | `build_*`, `import_*`, `promote_*`, `rollback_*`, `refresh_*` | under `application/` |
| Eval modules | `evaluate_*`, `compare_*`, `gate_*`, `run_*_eval` | under `evaluation/` |
| Tests | `test_<area>_<topic>.py` under `tests/{unit,contract,integration,governance,e2e}/` | `tests/unit/test_pk_ku_cli.py` |
| Incremental run ids | `ir_*` prefix | `pk-ku extract --run ir_…` |
| Policy / gate IDs | string ids in baselines | `architecture-boundary-v1`, `preflight-baseline-v1` |
| Console product names | short `pk-*` for product; legacy `rag-*` for search/API | `pk-sync`, `pk-ku` |

Path constants: define once in `src/personal_knowledge/core/project_paths.py`
(`ROOT`, `DATA_DIR`, `VAR_DIR`, `DB_DIR`, …). Prefer Phase 20 trees
(`data/`, `var/`, `archive/`) with legacy fallback only when new path absent.

---

## 4. Windows PowerShell notes

Default environment: **Windows + PowerShell** (CI also `windows-latest` in
`.github/workflows/ci.yml`).

### 4.1 Install and PYTHONPATH

```powershell
cd <project-root>
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -c constraints.txt -r requirements-dev.txt
.venv\Scripts\python -m pip install -e .
# if not using editable install:
$env:PYTHONPATH = "<project-root>\src"
```

### 4.2 Common ops

```powershell
pk-sync conversations
pk-sync conversations --write
pk-ku workflow
pk-ku inspect

# Vertex / gcloud when not on PATH
$env:PERSONAL_DATA_GCLOUD = "<path-to-gcloud.bat>"

# Services (REST 8000 + MCP 8789 + tunnel 8081) — keep window open
pwsh -NoProfile -ExecutionPolicy Bypass -File "apps\personal_data_chatgpt\scripts\start-services.ps1"
# or: apps\personal_data_chatgpt\scripts\启动服务.bat

# Health (bypass proxy for localhost)
curl.exe --noproxy "*" http://127.0.0.1:8000/health
curl.exe --noproxy "*" http://127.0.0.1:8789/health
curl.exe --noproxy "*" http://127.0.0.1:8081/healthz

# Governance + tests
python -m personal_knowledge.governance.preflight --ci
python -m pytest -q
```

### 4.3 Environment / proxy

| Item | Value / rule |
|------|----------------|
| Tunnel proxy | often `http://127.0.0.1:7897` for OpenAI control plane |
| REST / MCP | localhost only; `NO_PROXY` must include localhost for tunnel → MCP |
| AgentsView live | `%USERPROFILE%\.agentsview\sessions.db` — **read-only**, never relocate |
| Closing watchdog PS window | **stops all child services** |
| Paths | Use backticks or quotes for spaces; prefer `\` in native PS; avoid assuming `cwd` |

### 4.4 Privacy on Windows

- Do not commit `data/**`, `var/**` private DBs, secrets, tokens.
- Do not log full `gcloud auth print-access-token` output (length-only checks OK).
- No hardcoded `C:\Users\<name>\…` in production source; use env discovery
  (`PERSONAL_DATA_GCLOUD`, `project_paths`). Path-policy baseline:
  `governance/baselines/path_hits.yaml`.

---

## 5. Zones and git policy (short)

| Zone | Primary paths | Git |
|------|---------------|-----|
| source | `src/`, `apps/`, `tools/` | track |
| tests | `tests/` | track |
| assets | `assets/` (prompts, public evals) | track; no private bodies |
| docs / planning | `docs/`, `README.md`, `.planning/` | track |
| governance | `governance/` | track (sanitized baselines only) |
| data | `data/{raw,staging,canonical,imports}/` | private / ignore content |
| var | `var/{db,runtime,reports,logs,cache}/` | generated / ignore |
| archive | `archive/` | quarantine / retention |

Detail: `docs/architecture/repository-zones.md`, `governance/policies/paths.yaml`.

---

## 6. Compatibility shims

- Root/legacy shims under `tools/compat/v1_1/` and residual
  `integration/scripts/*.py` forward only — no business logic.
- Shim budget is baseline-only-down (`governance/manifests/entrypoints.yaml`,
  check: `integration/scripts/governance/check_shim_budget.py --check`).
- New product entry points: add console scripts / `python -m personal_knowledge…`,
  not new root shims, unless an approved compatibility requirement exists.

---

## 7. Docs and planning priority

Fact order when sources conflict:

1. Runtime / DB / tests  
2. VERIFICATION / UAT  
3. SUMMARY  
4. STATE / ROADMAP  
5. README / historical design  

Agent ops manual: `docs/AGENTS.md`. Workspace short form: `AGENTS.md`.
