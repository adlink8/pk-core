# Full Gap Audit — 2026-07-16

**Scope:** product CLI, KU incremental E2E, legacy residual, docs drift, security/SSOT, tests  
**Method:** local code / docs / artifacts only (read-only). No promote, no watermark `--write`, no delete, no push.  
**Workspace:** `<repo-root>`  
**Related:** `.planning/cleanup/2026-07-16-safe-cleanup.md`, `.planning/codebase/CONCERNS.md`, `docs/runbooks/ku-incremental.md`

---

## 1. Executive summary (top 10 gaps)

| Rank | Sev | Gap | Evidence | Impact |
|------|-----|-----|----------|--------|
| 1 | **P0** | Latest incremental cycle **cannot promote**: canary all labels empty (`pending_labels`); active still old collection | `var/reports/.../ku_canary_ir_4cd8af4ad_20260716.json` gate=`pending_labels`; `var/db/knowledge_index_active.txt` = `knowledge_units_205bff9560b9_20260712142938` | Candidate index + 1425 new current units exist but **retrieval SSOT active pointer lag**; product cycle incomplete |
| 2 | **P0** | `pk-ku promote` **does not require eval by default** — omit `--require-eval-pass` → active rewrite allowed | `promote_knowledge_index.py` `_check_eval_gate`: `require=False` returns ok; only opt-in | Operator/agent can promote mid-canary (contradicts Gate E in runbook) |
| 3 | **P0** | Full inventory + `prod --start` still **easy mistake path** (module CLI, no hard product ban) | `build_knowledge_inventory` / `build_knowledge_units_prod` still importable; Incident 2026-07-16 in runbook; only `pk-ku extract` rejects non-`ir_*` | Paid re-extract of ~20k+ items; already caused one incident |
| 4 | **P1** | Extract-gate **api_completion failed** on latest run (52 non-schema terminal) | Runbook §2026-07-16 cycle: yield 0.775; privacy fixed for `di_*`; **failed only on api_completion** | Gate status `failed` even if yield OK; no formal waiver path on `pk-ku` |
| 5 | **P1** | **Application still imports `domains.*` facades** (circular sys.modules hop) | ≥16 hits under `application/` (e.g. `build_knowledge_units_prod`, `backfill_loop`, `extract_knowledge_units_l2_session`, `knowledge_unit_pipeline` docstring still teaches domains import) | **2026-08-13 facade delete will break product path** unless retargeted first |
| 6 | **P1** | **Test holes on new product surfaces**: no `pk-sync` tests; no `publish_incremental_run` tests; canary/promote require-eval only partial | `tests/unit/test_pk_ku_cli.py` thin wrapper only; grep `publish_incremental` in tests = 0; `pk-sync` in tests = 0 | Regressions on additive publish / sync entry go undetected |
| 7 | **P1** | **Docs drift / stale maps** teach wrong or obsolete commands | `product-sync.md` still “start with `refresh_knowledge_units --inspect`”; `docs/AGENTS.md` cheat sheet still `build_knowledge_units_prod --status`; `CONCERNS.md` §7 says canary/watermark **not** on `pk-ku` (they are); cleanup log “CLI canary/watermark optional later” outdated | Agents follow module paths or think packaging incomplete |
| 8 | **P1** | Merge gate may report **error / FAIL without blocking write** | `evaluate_merge_gate` needs `merge_positive_pairs.private.jsonl` + hard negatives; pairs use `cm|…` refs; `run()` prints gate after write but **exit 0 anyway** | False confidence on canonical quality; gate not wired into `pk-ku` exit code |
| 9 | **P2** | Governance residual: `entrypoints.yaml` root=`integration/scripts` expected 86; live shims = `tools/compat/v1_1` | `governance/manifests/entrypoints.yaml`; CONCERNS §1 | Preflight/shim budget false fails or false security |
| 10 | **P2** | Domains/tests/compat double surface + empty `integration/scripts/{knowledge,memory,…}` shells | ~87 compat shims; domains facades until 2026-08-13; bak-phase20 **moved** to quarantine but CONCERNS still describes old bak paths | Import noise, wrong generation risk, onboarding confusion |

---

## 2. CLI matrix

### 2.1 `pk-sync` (`application/sync.py` → `cli:sync`)

| Command | Status | Gap |
|---------|--------|-----|
| `conversations` | **OK** (dry-run default; `--write` publishes) | No `--max` / progress flags; no count summary beyond stage logs |
| `conversations --dry-run` | **OK** (explicit, same as default) | Redundant with default; fine |
| `help-legacy` | **OK** | Text only; correct |
| `google` / light structure | **Missing** | Still `python -m personal_knowledge.application.build_google_*` / lifecycle — tribal |
| conversation summary / turn vectors / source rollback | **Missing on CLI** | Documented as module-only in `product-sync.md` |
| **Tests** | **Missing** | No `test_pk_sync*` |

### 2.2 `pk-ku` (`application/ku.py` → `cli:ku`)

| Command | Status | Gap |
|---------|--------|-----|
| `workflow` | **OK** | — |
| `inspect` | **OK** | Thin wrap of `refresh --inspect` |
| `prepare` | **OK** + policy flags | Still requires operator to pass model/provider/endpoint/auth every time (no project defaults file on CLI); historical inspect≠prepare defect **partially fixed** in `prepare_production_delta` (inventory baseline vs same-path self-diff) but agent social rule still required |
| `extract` | **OK** + `ir_*` guard | Guard is prefix-based, not DB `run_type` check; env escape hatch `PK_KU_ALLOW_NON_INCREMENTAL_RUN=1` |
| `status` | **OK** | — |
| `extract-gate` | **OK** | No flag for “waive api_completion with human note”; exit code depends on eval module |
| `canonical` | **OK** | Merge gate not fail-closed on CLI exit |
| `publish` | **OK** (additive) | **No unit/integration test**; only path that avoids StagingPublisher demote |
| `vector` | **OK** | Rebuilds full candidate from **all current** units (not “delta only index”) — costly/side-effect-ish but documented |
| `canary` | **OK** (packaged) | Labels are manual JSON edit; no worksheet helper on CLI; first query can show multi-second cold latency |
| `promote --list` | **OK** | — |
| `promote --collection` | **Risky default** | **Eval optional** unless flags set |
| `watermark` / `--advance --from-canonical [--write]` | **OK** | No precondition check that promote already happened (social Gate F only) |
| Full inventory / prod `--start` | **Intentionally absent** | Good — still reachable via modules |

### 2.3 Other console scripts (`pyproject.toml`)

| Command | Status | Gap |
|---------|--------|-----|
| `rag-search` | Product OK | Legacy fallback to `integration/scripts` if package missing (`cli.py`) |
| `rag-api` / `rag-mcp` / `rag-dashboard` | Product OK | Same fallback |
| `rag-pipeline` | **Retired** exit 2 | Correct; forensics via `PK_ALLOW_LEGACY_PIPELINE=1` |

### 2.4 Forbidden paths still discoverable as easy mistakes

| Path | Discoverable how? | Guard strength |
|------|-------------------|----------------|
| `python -m …build_knowledge_inventory --write` | Module `__main__`, shims, old phase docs | **Docs only** |
| `…build_knowledge_units_prod --start` | Same | **Docs only** |
| Resume full run `6f3da1e…` style | DB ledger + forensics scripts | `pk-ku extract` rejects non-`ir_*`; **module resume still free** |
| `StagingPublisher.promote` (demotes others) | `knowledge_unit_pipeline` | Docstring warning; not blocked |
| `tools/compat/v1_1/*.py` | README in compat; entrypoints yaml | “Not product” docs only |
| `rag-pipeline` without env | Entry point | **Hard** exit 2 |

---

## 3. Pipeline gaps (KU incremental E2E)

### 3.1 Flow vs latest run evidence

Reference cycle (runbook + artifacts): **`ir_4cd8af4ad31ccdc2`** / delta **`di_9e002cdac7af1460`**.

| Step | Expected | Evidence | Status / impact |
|------|----------|----------|-----------------|
| inspect | Free delta | Procedure documented; hard rule inspect≠prepare | **Process OK**; code path OK |
| prepare | Delta inventory + `fresh_run_id` | Artifact `knowledge_incremental_delta.json`: `extract_item_count=756`, `new_count=18259` filtered by watermark/roles/skip | **OK this cycle**; large raw delta vs small queue needs operator literacy |
| extract | Paid resume `ir_*` | 756 → 586 succeeded / 110 abstained / 60 terminal_failed (runbook) | **Partial quality** |
| extract-gate | Critical pass | privacy `di_*` fixed; **api_completion failed** (52 non-schema terminal) | **P1** — gate failed; no product waive UX |
| canonical | staging units | 1456 draft → 1425 staging (runbook) | **OK** write path |
| merge gate | quality signal | Private pairs exist under `integration/evals/knowledge_units/`; gate not CLI-blocking | **P1** soft gate |
| publish | additive staging→current | +1425 current; total current **32184**; active untouched | **OK** |
| vector | candidate only | `knowledge_units_ir_4cd8af4ad_20260716020508` (32184) | **OK** |
| canary | labels + strict | Report: 30 queries, all `"label": ""`, `gate.status=pending_labels`, p95≈152ms | **P0 block** |
| promote | after PASS | Not done; active still `knowledge_units_205bff9560b9_20260712142938` | **Blocked / incomplete** |
| watermark | after promote | Not advanced (by design until promote) | **Pending** |

### 3.2 Known defect tracker

| Issue | Evidence | Current state |
|-------|----------|---------------|
| prepare vs inspect history (same-path self-diff) | Runbook Gate B; `prepare_production_delta` docstring: never same live DB as before/after; uses watermark inventory baseline | **Mitigated in code**; residual risk if baseline inventory missing/wrong |
| `di_*` privacy gate | `evaluate_knowledge_unit_extraction.py` uses `knowledge_delta_items` when inventory_id starts with `di_` | **Fixed** (this cycle privacy OK) |
| api_completion failures | Gate 2 counts terminal_failed with `last_error_class != schema_invalid` | **Open** on latest run |
| merge gate missing / weak eval pairs | pairs present but refs are `cm|…`; unit test `test_merge_gate_no_eval_files` covers missing only | **Open quality** |
| canary pending_labels | report JSON | **Open ops** — human labeling not done |
| Mistaken full inventory run in DB | Incident + `tmp_ku_verify_run.py` probes `6f3da1eec10c4fee6fb1509c83cfb85b` | **Data residue** — do not resume |

### 3.3 Operational readiness snapshot

| Item | Value |
|------|-------|
| Active pointer | `knowledge_units_205bff9560b9_20260712142938` |
| Candidate (pending promote) | `knowledge_units_ir_4cd8af4ad_20260716020508` |
| Watermark vs source | Artifact before=`90c631…` after=`87e24e…` — watermark **not** equal to post-cycle source until advance |
| Knowledge SSOT claim | SQLite current units include new run; **vector active index does not** |

---

## 4. Legacy residual table

| Residual | Location | Class | Status 2026-07-16 | Action due |
|----------|----------|-------|-------------------|------------|
| Domains re-export facades | `src/personal_knowledge/domains/{conversation,graph,knowledge,memory}/*.py` | keep-facade | Window **→ 2026-08-13**; SCHEMA_SQL stays | Migrate imports first |
| Business imports of domains from application | e.g. prod/l2/pipeline/rollback | **debt** | Active | Rewrite to application/evaluation/core |
| evaluation → domains | memory/graph eval modules | debt | Active | Same |
| retrieval lazy domains | `retrieval/memory.py` ×3 | debt | Phase 21 deferred | Point to application.graph |
| tools/compat/v1_1 | ~87 shims → domains | keep-facade | Double hop | Retarget before facade delete |
| entrypoints.yaml | claims `integration/scripts` | **stale** | Wrong root | Fix root + count |
| integration/scripts shells | knowledge/, memory/, … empty + governance live | residual | pyc purged (safe-cleanup); shells remain | Optional remove empty pkgs after smoke |
| integration/scripts README | product CLI | **OK** after rewrite | Matches pk-ku | Keep |
| bak-phase20 | `archive/quarantine/bak-phase20-20260716/` | quarantine | **Moved** (safe-cleanup) | Hold; physical delete later only |
| CONCERNS bak paths | still describes `integration/*.bak-phase20` at root | **docs stale** | Quarantine already done | Refresh CONCERNS |
| archive/quarantine/_recycle | ~9k private files | quarantine | Hold | Owner retention |
| var/tmp_ku_*.py diagnostics | `var/` | junk | Local forensics | Optional cleanup (not product) |
| test_coverage_gaps.md | 2026-07-12, integration/scripts layout | stale report | Misleading | Regenerate vs `src/` |
| Memory experimental path | application/memory + rag-pipeline steps | retired product | exit 2 | Keep stub |
| StagingPublisher full promote | knowledge_unit_pipeline | capability | Must not use for incremental | Prefer `publish_incremental_run` only |

---

## 5. Docs drift list

| Doc | Drift | Severity |
|-----|-------|----------|
| `docs/runbooks/product-sync.md` L47 | “start with `refresh_knowledge_units --inspect` only” instead of **`pk-ku inspect`** | **P1** |
| `docs/AGENTS.md` cheat sheet L253 | `build_knowledge_units_prod --status` instead of **`pk-ku status`** | **P1** |
| `.planning/cleanup/2026-07-16-safe-cleanup.md` | “CLI canary/watermark optional later” / “NOT done” | **P1** (false; implemented in `ku.py`) |
| `.planning/codebase/CONCERNS.md` §7 | Claims canary & watermark advance **not** on `pk-ku` | **P1** (stale map) |
| `.planning/codebase/CONCERNS.md` §3 | bak-phase20 still under `integration/` / root | **P2** (already quarantined) |
| `governance/manifests/entrypoints.yaml` | shim root `integration/scripts` | **P2** governance |
| `var/reports/.../test_coverage_gaps.md` | Pre-Phase-19 script tree | **P2** |
| Root `AGENTS.md` + `docs/AGENTS.md` + ku-incremental | Generally **aligned** on pk-ku / ban full inventory | Good |
| `integration/scripts/README.md` | Product CLI matrix present | Good (after cleanup) |
| `docs/architecture/domains-slimming.md` | Still accurate on facade window; product table missing full pk-ku surface | **P2** minor |
| ku-incremental §5 command matrix | Still module-centric (`refresh --inspect`) mixed with pk-ku §0 | **P2** dual vocabulary |

**Contradictions vs current CLI (fact):**

- Code: `pk-ku` has **canary**, **watermark**, **publish**, **extract-gate**, **vector**, etc.  
- Stale docs: CONCERNS + safe-cleanup still describe packaging gap.  
- product-sync / AGENTS cheat sheet still point operators at raw modules for steps already on CLI.

---

## 6. Security / SSOT

| Control | Status | Evidence | Gap |
|---------|--------|----------|-----|
| Active pointer last / mid-run promote ban | **Process + optional flag** | Runbook Gate E; promote can skip eval | **P0**: require-eval not default |
| AgentsView RO | **OK** | `adapters/agentsview.py` URI `mode=ro`; docs hard ban write | Keep |
| Secrets not in git | **OK pattern** | `.gitignore` ignores `data/**`, `var/**`, `*.sqlite`, `*.json`, archive bodies | Ensure no future force-add; no shell secret scan in this audit |
| Full inventory daily ban | **Docs + pk-ku non-exposure** | AGENTS, ku-incremental, extract `ir_*` | **Not hard-enforced** on inventory/prod modules |
| Knowledge SSOT = KU + active | Documented | docs/AGENTS.md SSOT map | Active lag after incremental (current cycle) |
| Memory not SSOT | Documented | Phase 08 cancelled messaging | Residual memory scripts still present |
| Watermark advance fail-closed | **OK** | needs `--advance` + source + `--write` | No promote precondition |
| Privacy extract-gate di_* | **Fixed** | evaluation gate code | — |

---

## 7. Test coverage gaps (product-relevant)

| Surface | Tests today | Gap |
|---------|-------------|-----|
| `pk-ku` parser / workflow / ir_* reject / watermark dry | `tests/unit/test_pk_ku_cli.py` | No mock of prepare/extract wiring; no canary argv build; no publish dry-run |
| `publish_incremental_run` | **None** | Critical incremental safety (no demote) untested |
| `pk-sync` | **None** | Conversations stage entry untested at CLI |
| Canary label completeness / strict | contract tests via domains facade | Not via `pk-ku canary` |
| Promote `--require-eval-pass` | integration promote tests exist (domains) | Default-unsafe path not asserted as product policy |
| Incremental pipeline sandbox | `test_knowledge_incremental_pipeline.py` strong | Does not cover full `pk-ku` orchestration order |
| Integration suite | 17 modules (TESTING.md) | Healthy for KU lifecycle internals |
| Governance | path/shim/privacy | entrypoints drift not auto-fixed |
| Stale coverage report | test_coverage_gaps 2026-07-12 | Wrong tree |

---

## 8. Recommended next actions (max 10, ordered)

1. **Label canary report** `var/reports/analysis/ai_context/ku_canary_ir_4cd8af4ad_20260716.json` → `pk-ku canary --report … --check-label-completeness` → `--strict`; only then consider promote. *(ops, unblocks cycle)*  
2. **Fail-closed promote:** make `--require-eval-pass` default for `pk-ku promote --collection` (or refuse promote without gate artifact). *(P0 product)*  
3. **Hard-ban daily full inventory:** env or dual-confirm on `build_knowledge_inventory --write` / `prod --start` against production UNIFIED_DB (keep forensics flag). *(P0 accident class)*  
4. **Retarget application/evaluation imports off `domains.*`** (except SCHEMA_SQL) before 2026-08-13; fix `retrieval/memory.py` and compat shims next. *(P1 deadline)*  
5. **Add tests:** `publish_incremental_run` (dry/write, refuse non-ir, no demote); `pk-sync conversations` dry; promote without eval → fail when product policy on. *(P1)*  
6. **Docs pass:** fix `product-sync.md`, `docs/AGENTS.md` cheat sheet, refresh `CONCERNS.md` §3/§7, update safe-cleanup “NOT done” for canary/watermark. *(P1, cheap)*  
7. **extract-gate api_completion:** classify/retry 52 terminal failures or document human waiver + non-zero exit policy for `pk-ku extract-gate`. *(P1 quality)*  
8. **Fix `entrypoints.yaml`** shim root → `tools/compat/v1_1` + recount. *(P2 governance)*  
9. **Merge gate:** verify private pair unit refs still resolve post-incremental; either refresh pairs or skip gate with explicit `skipped` (not silent FAIL print + exit 0). *(P1 quality)*  
10. **Abandon mistaken full run** in ops notes (`6f3da1e…` pending); never resume; optional orphan candidate collection inventory. *(data hygiene)*  

---

## 9. Evidence index (paths)

| Artifact | Path |
|----------|------|
| Product CLI | `src/personal_knowledge/cli.py`, `application/ku.py`, `application/sync.py` |
| Entry points | `pyproject.toml`, `src/personal_knowledge.egg-info/entry_points.txt` |
| Incremental prepare | `application/knowledge/refresh_knowledge_units.py` (`prepare_production_delta`) |
| Publish additive | `application/knowledge/publish_incremental_run.py` |
| Extract gate | `evaluation/knowledge/evaluate_knowledge_unit_extraction.py` |
| Canary | `evaluation/knowledge/evaluate_knowledge_canary.py` |
| Promote | `application/knowledge/promote_knowledge_index.py` |
| Delta artifact | `var/reports/analysis/ai_context/knowledge_incremental_delta.json` |
| Canary report | `var/reports/analysis/ai_context/ku_canary_ir_4cd8af4ad_20260716.json` |
| Active pointer | `var/db/knowledge_index_active.txt` |
| Runbook | `docs/runbooks/ku-incremental.md` |
| Prior cleanup | `.planning/cleanup/2026-07-16-safe-cleanup.md` |
| Concerns map | `.planning/codebase/CONCERNS.md` |

---

## 10. Audit limits

- No live `pytest` run or SQLite ad-hoc query executed in this pass (no shell tool in session); run stats rely on runbook + JSON artifacts + code paths.  
- No secret scanning beyond `.gitignore` review.  
- No measurement of archive disk size beyond prior CONCERNS estimates.  
- Does not authorize any cleanup delete or promote.

---

## Addendum — docs drift items fixed 2026-07-16

P1 docs pass from §4 / §8 item 6 applied (wording only; no product code change):

| File | Fix |
|------|-----|
| `docs/runbooks/product-sync.md` | `refresh_knowledge_units --inspect` → `pk-ku inspect` + full chain pointer to `ku-incremental.md` |
| `docs/AGENTS.md` cheat sheet | `build_knowledge_units_prod --status` → `pk-ku status --run` |
| `.planning/codebase/CONCERNS.md` §7 | canary/watermark marked **resolved 2026-07-16** (`pk-ku canary` / `pk-ku watermark`); Google sync still open |
| `.planning/cleanup/2026-07-16-safe-cleanup.md` | canary/watermark moved from “NOT done / optional later” to **done** |

---

*End of audit — 2026-07-16*
