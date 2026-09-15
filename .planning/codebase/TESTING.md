---
mapped_at: 2026-07-16
last_mapped_commit: ce6dfde1f6d759368077e47288dcfc811f2960b9
focus: quality
branch: codex/llm-memory-mcp-integration
---

# Testing Map (quality)

Authoritative sources: `pytest.ini`, `tests/README.md`, `.github/workflows/ci.yml`,
`src/personal_knowledge/governance/preflight.py`, `docs/runbooks/dependency-governance.md`,
`assets/evals/knowledge_units/eval_policy_v1.yaml`, phase verification notes
(`.planning/phases/21-…/21-VERIFICATION.md`).

---

## 1. Tooling and config

| Item | Location / value |
|------|------------------|
| Runner | `pytest` via `python -m pytest` |
| Config | `pytest.ini` |
| `testpaths` | `tests` only (bare pytest does **not** walk `_recycle/`, `integration/scripts/test_*.py`, `.planning`, private data) |
| Patterns | `python_files=test_*.py`, `python_classes=Test*`, `python_functions=test_*` |
| `pythonpath` | `src` |
| Cache | `var/cache/pytest` (Phase 20; not repo root) |
| Default addopts | `-q` |
| Dev deps | `requirements-dev.txt` + `constraints.txt` |
| Supported Python | **3.12 and 3.14** (CI matrix); package `requires-python >=3.11` |
| Node tests | `apps/personal_data_chatgpt/` — Node 20, `npm test` |

```powershell
# From project root
.venv\Scripts\python -m pip install -c constraints.txt -r requirements-dev.txt
.venv\Scripts\python -m pip install -e .
python -m pytest --collect-only -q
python -m pytest -q
```

---

## 2. Layout under `tests/`

```text
tests/
  README.md                 # short responsibility / entry points
  unit/                     # pure unit + synthetic fixtures
  contract/                 # schema, CLI, API, pipeline contracts
  integration/              # multi-module lifecycle, evals, adapters
  governance/               # preflight contracts, path policy, migration layout
  e2e/                      # thin smoke (e.g. dashboard import)
```

### 2.1 `tests/unit/` (representative)

| File | Focus |
|------|--------|
| `test_pk_ku_cli.py` | `pk-ku` parser / workflow / non-incremental run_id reject |
| `test_canonical_knowledge_units.py` | KU canonicalization |
| `test_conversation_repository.py`, `test_conversation_summary_parse.py` | conversation helpers |
| `test_knowledge_unit_*.py` | extraction, gate, pilot, vector store, retry cache, RAG eval, backfill |
| `test_knowledge_l2_session_extract.py`, `test_knowledge_evidence_backfill.py` | L2 / evidence |
| `test_graph_relation_*.py` | graph candidates / judgments |
| `test_memory_*.py` | memory candidates, promotion, lifecycle, experiments, decomplexity plan |
| `test_vector_*.py` | collection/retrieval eval, store filter |
| `test_privacy_guard.py`, `test_rag_feedback_privacy.py` | privacy |

Approx. **33** unit modules (see directory listing).

### 2.2 `tests/contract/`

| File | Focus |
|------|--------|
| `test_knowledge_unit_contracts.py` | KU public contracts |
| `test_knowledge_search_contracts.py` | search surface |
| `test_knowledge_distribution_contracts.py` | distribution |
| `test_data_access_contracts.py` | data access APIs |
| `test_run_pipeline_contracts.py` | step select / retired pipeline contracts |
| `test_mcp_server_contracts.py` | MCP tool surface |
| `test_apps_sdk_data_contracts.py` | Apps SDK payloads |
| `test_agentsview_downstream_contracts.py` | AgentsView → downstream |

**8** contract modules.

### 2.3 `tests/integration/`

| Area | Examples |
|------|----------|
| Import / AgentsView | `test_import_pipeline.py`, `test_agentsview_*.py` |
| KU lifecycle | `test_knowledge_incremental_*.py`, `test_knowledge_index_promotion.py`, `test_knowledge_index_reconcile.py`, `test_knowledge_checkpoint_rollback.py`, `test_knowledge_unit_checkpoint.py` |
| Knowledge eval | `test_knowledge_eval_{dataset,extraction,retrieval,answers,gate,report}.py` |
| Google light | `test_google_light_structure.py` |
| Conversation rollback | `test_agent_conversation_rollback.py` |

**17** integration modules.

### 2.4 `tests/governance/`

| File | Focus |
|------|--------|
| `test_governance_preflight.py` | baseline cannot exempt P0 |
| `test_governance_architecture.py` | architecture policy |
| `test_governance_paths.py` | path baseline contract |
| `test_governance_artifacts.py` | storage budgets |
| `test_governance_inventory.py` | inventory check |
| `test_governance_privacy.py` | privacy audit |
| `test_governance_planning.py` | planning consistency |
| `test_governance_shims.py` | shim registry |
| `test_governance_migration.py`, `test_data_migration_*.py` | data migration cohorts |
| `test_physical_data_layout.py`, `test_physical_source_layout.py`, `test_phase19_default_paths.py` | Phase 19/20 layout |
| `test_governance_report.py` | report render contracts |

**17** governance modules.

### 2.5 `tests/e2e/`

| File | Focus |
|------|--------|
| `test_dashboard_smoke.py` | dashboard import / path constants smoke |

### 2.6 Node (outside pytest)

| Path | Command |
|------|---------|
| `apps/personal_data_chatgpt/test/contract.test.mjs` | `npm test --prefix apps/personal_data_chatgpt` |
| `apps/personal_data_chatgpt/test/widget-render.test.mjs` | same |

CI job `node` on `windows-latest` with Node 20.

### 2.7 UAT / human gates

Live proofs live in `.planning/phases/*/*-UAT.md` and `*-VERIFICATION.md` — not
in default CI.

---

## 3. How to run

### 3.1 Full offline suite

```powershell
python -m pytest -q
python -m pytest -q --tb=short
```

### 3.2 Subsets

```powershell
python -m pytest -q tests/unit/
python -m pytest -q tests/contract/
python -m pytest -q tests/integration/
python -m pytest -q tests/governance/
python -m pytest -q tests/e2e/
python -m pytest -q tests/unit/test_pk_ku_cli.py
python -m pytest -q -k knowledge
python -m pytest -q tests/unit/test_vector_collection_eval.py
# evaluation README also references legacy flat paths; prefer tests/unit + tests/integration
```

### 3.3 Collect-only (CI step 1)

```powershell
python -m pytest --collect-only -q
```

### 3.4 Governance preflight (CI step before pytest)

```powershell
# Canonical package entry
python -m personal_knowledge.governance.preflight --ci
# Compatibility wrapper used by CI workflow today:
python integration/scripts/governance/preflight.py --ci
```

Optional HTML (aggregate only; under `var/runtime/` after Phase 20):

```powershell
python integration/scripts/governance/render_governance_report.py `
  --preflight var/runtime/governance/preflight.json `
  --history var/runtime/governance/governance_history.sqlite `
  --output var/runtime/governance/governance_report.html
```

Note: package `preflight.py` default `--json-output` still mentions
`integration/runtime/governance/preflight.json` (legacy path); prefer writing
under `var/runtime/governance/` when documenting ops.

### 3.5 Node

```powershell
npm ci --ignore-scripts --prefix apps/personal_data_chatgpt
npm test --prefix apps/personal_data_chatgpt
```

### 3.6 CI matrix (`.github/workflows/ci.yml`)

| Job | Runner | Steps |
|-----|--------|-------|
| `python` | `windows-latest` | matrix Python **3.12**, **3.14**; `pip install -c constraints.txt -r requirements-dev.txt`; preflight `--ci`; collect-only + full pytest |
| `node` | `windows-latest` | Node **20**; `npm ci`; `npm test` in `apps/personal_data_chatgpt` |

---

## 4. Triple / multi-level gates

Project language uses **preflight gates**, **pytest**, and **promotion/eval gates**.
Documented quality ladder (local → PR → candidate → production):

### G0 — Local fast gate (every change)

```powershell
python -m pytest --collect-only -q
python -m pytest -q --tb=short
# targeted:
python -m pytest -q tests/unit/test_pk_ku_cli.py
```

Plus smoke imports / health when touching services:

```powershell
curl.exe --noproxy "*" http://127.0.0.1:8000/health
curl.exe --noproxy "*" http://127.0.0.1:8789/health
```

### G1 — PR / CI gate (blocking)

From preflight `evaluate()` (`src/personal_knowledge/governance/preflight.py`):

| Gate id | Check |
|---------|--------|
| `inventory-check` | `build_project_inventory.py --check` |
| `privacy-check` | `audit_artifacts.py --check --no-content` |
| `path-policy` | `check_path_policy.py --check` vs `governance/baselines/path_hits.yaml` |
| `shim-budget` | `check_shim_budget.py --check` (baseline-only-down) |
| `docs-coverage` | `check_docs_coverage.py --check` |
| `planning-consistency` | `check_planning_consistency.py --check` |
| `dependency-lock` | dependency findings from `check_dependencies` |
| `architecture-boundary` | AST import scan vs `governance/policies/architecture.yaml` |
| `secret-scan` | P0 credential patterns on safe roots |
| `artifact-lineage` | tied to privacy-check metadata |
| `storage-retention` | budgets; no disposition side effects |
| `test-matrix` | CI declares Python 3.12/3.14 + Node 20 + preflight |

Plus full offline pytest (both Python versions) and Node `npm test`.

Baseline file: `governance/baselines/preflight.json`

- `policy_id`: `preflight-baseline-v1`
- **P0 findings never grandfathered**
- Only exact IDs in `allowed_non_p0_findings` may be non-blocking (currently **empty**)

### G2 — Candidate / knowledge promotion gate

Not CI-default; product path after KU vector candidate:

| Surface | Location |
|---------|----------|
| Policy | `assets/evals/knowledge_units/eval_policy_v1.yaml` |
| Runner | `personal_knowledge.evaluation.run_knowledge_eval`, `gate_knowledge_candidate` |
| CLI promote | `pk-ku promote --require-eval-pass …` |
| Hard gates | `secret_privacy_hit: 0`, citation precision, no-answer FP, reconcile missing/orphan |
| Quality gates | KU vs raw Recall@5 delta, MRR non-inferior, frozen regression, cross-turn L2, p95 latency, grounded L2 precision |
| Modes | `raw`, `l1`, `l1_l2`, `hybrid` |
| Rule | Candidate must not mutate active; journal + checksum; fail → active unchanged |

Integration coverage: `tests/integration/test_knowledge_eval_*.py`,
`tests/unit/test_knowledge_unit_gate.py`.

### G3 — Production / periodic governance

- Canary labels + post-promote reconcile + rollback drills
- Incremental regression when source checksum changes
- Monthly lineage / orphan / backup recoverability (ops, not bare pytest)
- Optional: `preflight` with history HTML under `var/runtime/governance/`

Phase 21 health residual checks: REST `:8000/health` and MCP `:8789/health` → 200.

---

## 5. Known governance baselines and residuals

### 5.1 Baseline artifacts under `governance/baselines/`

| File | Role |
|------|------|
| `preflight.json` | allowed non-P0 finding IDs (empty); P0 never exempt |
| `path_hits.yaml` | path-policy categories / forbidden_new patterns |
| `storage_budgets.yaml` | artifact size/retention budgets |
| `inventory_summary.json` | sanitized inventory summary |
| `phase19_active_data_snapshot.json`, `phase19_before_after_tree.json` | Phase 19 evidence |

### 5.2 Shim / tool budgets

| Registry | Config | Check |
|----------|--------|-------|
| Compatibility shims | `governance/manifests/entrypoints.yaml` → `shim_registry.expected_count` (**86**) | `check_shim_budget.py --check` |
| Tools cohort | `governance/manifests/source/tools.json` + `tool_registry.expected_count` | same script; count must match manifest |

Budget is **baseline-only-down**: increases fail closed.

### 5.3 Documented pytest residuals (Phase 21)

From `.planning/phases/21-architectural-alignment-domains-slimming/21-VERIFICATION.md`
(re-run 2026-07-15):

| Class | Count / note |
|-------|----------------|
| Known fail baseline | **13** total: **8 governance + 5 memory_decomplexity** |
| Architecture goal | **PASS** if no *new* fails beyond that baseline |
| `architecture-boundary` preflight | PASS (phase goal) |
| Full `preflight --ci` | may still fail inventory/shim/docs/secret/lineage/retention (pre-existing governance debt) |

`tests/unit/test_memory_decomplexity_plan.py` depends on generated plan files under
`integration/analysis/ai_context/memory_decomplexity_plan.{json,md}` (private/generated
tree) — absence or drift produces the memory_decomplexity failures.

Treat “13-fail baseline” as **historical residual**, not a license to add new
reds. Prefer fixing or marking intentional skips with rationale when touching
those areas.

### 5.4 Test rules (must keep)

- Unit/contract tests: **no** live AgentsView DB, Google Takeout, paid LLM, or
  network; use `tmp_path`, in-memory SQLite, synthetic fixtures.
- Live-data validation: explicit operator commands + VERIFICATION/UAT only.
- Candidate/promote tests: gate fail ⇒ active pointer unchanged; journal
  traceable; rollback restores pointer/version.
- Eval: fixed dataset/config/checksum; reports include absolute metrics, paired
  delta, CI, privacy hits, latency; no selective hiding of failures.
- Fixed KU eval slices: no-answer, secret/privacy, conflict, stale knowledge,
  paraphrase, cross-turn.
- Privacy: never commit personal evidence in fixtures; public fixtures under
  `assets/evals/` only.

### 5.5 Path resolution in tests

Live path helpers: `personal_knowledge.core.project_paths` (`data/`, `var/`).
Some governance layout tests `pytest.skip` when phase inventories or artifacts
are not present on the machine (`test_physical_data_layout.py`).

---

## 6. Ownership

| Surface | Owner (docs) |
|---------|----------------|
| `tests/` | quality (`governance/policies/paths.yaml` rule `tests`) |
| Evaluation package | evaluation (`src/personal_knowledge/evaluation/README.md`) |
| Preflight / policies | engineering-governance / platform-governance |
| KU promotion policy | evaluation + knowledge product (`eval_policy_v1.yaml`) |

Status: supported quality map after Phase 20 layout + Phase 21 domains slimming
+ 2026-07-16 product CLI (`pk-sync` / `pk-ku`).
