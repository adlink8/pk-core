# Automated Test Report — 2026-07-16

## Environment

| Item | Value |
|------|--------|
| OS | Windows (PowerShell) |
| Workspace | `<repo-root>` |
| Python | **3.14.2** (`$HOME\AppData\Local\Programs\Python\Python314\python.exe`) |
| `PYTHONPATH` | `<repo-root>\src` |
| Runner | `python -m pytest` |
| Report date | 2026-07-16 |
| Product source changes | **None** (read-only test run) |

### Constraints observed

- No promote / active index write
- No watermark `--write` / `--advance`
- No full inventory start
- No paid LLM extract / Vertex
- No data delete / git push

---

## Executive summary

| Scope | Result |
|-------|--------|
| **Product-critical (pk-ku CLI, KU incremental, knowledge/sync/conversation)** | **All green** |
| **Full suite** | **486 passed, 16 failed, 2 skipped** (exit 1) |
| **Smoke CLI** | **All exit 0** |

Failures cluster in **governance / physical layout / retired pipeline contracts / missing plan artifact** — not in the daily KU / conversation product path.

**One-line:** `486 passed, 16 failed, 0 errors, 2 skipped` (full suite); product path subsets all passed.

---

## 1. Unit: pk-ku CLI

```text
python -m pytest -q tests/unit/test_pk_ku_cli.py --tb=short
```

| Metric | Value |
|--------|--------|
| Exit code | **0** |
| Passed | **8** |
| Failed | 0 |
| Skipped | 0 |
| Duration | **~8.6 s** |
| Raw log | `.planning/cleanup/_test1_pk_ku_cli.txt` |

---

## 2. Integration: knowledge incremental

```text
python -m pytest -q tests/integration/test_knowledge_incremental_pipeline.py tests/integration/test_knowledge_incremental_refresh.py --tb=line
```

| Metric | Value |
|--------|--------|
| Exit code | **0** |
| Passed | **29** |
| Failed | 0 |
| Skipped | 0 |
| Duration | **~4.8 s** |
| Raw log | `.planning/cleanup/_test2_knowledge_incremental.txt` |

---

## 3. Broader: knowledge / ku / sync / conversation

```text
python -m pytest -q tests -k "knowledge or ku or sync or conversation" --tb=line
```

| Metric | Value |
|--------|--------|
| Exit code | **0** |
| Passed | **282** |
| Failed | 0 |
| Skipped | 0 |
| Duration | **~57.7 s** |
| Truncation | No — completed fully |
| Raw log | `.planning/cleanup/_test3_knowledge_ku_sync.txt` |

---

## 4. Full pytest suite

```text
python -m pytest -q tests --tb=line
```

| Metric | Value |
|--------|--------|
| Exit code | **1** |
| Passed | **486** |
| Failed | **16** |
| Skipped | **2** |
| Errors | **0** |
| Total collected/run (status chars) | **504** |
| Duration | **~63.4 s** (under 15–20 min cap) |
| Hang | No |
| Raw log | `.planning/cleanup/_test4_full_pytest.txt` |

Progress pattern (first progress block): many `.`, then `F` early (contract/governance), later memory plan `F`s, **2** `s` skips.

### Failed tests (16) — name + short reason

| # | Test | Short reason |
|---|------|----------------|
| 1 | `tests/contract/test_run_pipeline_contracts.py::test_dry_run_main_prints_without_executing` | `AttributeError`: `Namespace` has no `agentsview_only` (suggests `agentsview_write`). Contract vs retired `run_pipeline` CLI drift. |
| 2 | `tests/governance/test_governance_artifacts.py::test_preview_has_approval_owner_reason_size_privacy_and_rollback` | `KeyError: 'recycle-quarantine'` — preview map missing expected action key. |
| 3 | `tests/governance/test_governance_inventory.py::test_schema_accepts_complete_inventory` | `jsonschema.ValidationError`: node status `'rolled-back-legacy'` not in allowed enum (`active`, `candidate`, `deprecated`, `quarantined`, `archived`, `orphaned`, `excluded`). |
| 4 | `tests/governance/test_governance_inventory.py::test_ordered_policy_is_unique_for_all_fixture_types` | Assertion: expected type label `private-runtime`, got `private-runtime-legacy`. |
| 5 | `tests/governance/test_governance_shims.py::test_all_legacy_shims_resolve_to_existing_static_targets` | Shim list length **85 == 86** expected — one legacy shim target missing or inventory count drift. |
| 6 | `tests/governance/test_physical_source_layout.py::test_manifest_records_consumers_dirty_overlap_and_excludes_private_data` | `assert any(...)` false — consumer/dirty/private-data expectation not met by current manifest. |
| 7 | `tests/governance/test_physical_source_layout.py::test_manifest_declares_five_stable_console_scripts` | Console scripts set has **extra** `pk-ku`, `pk-sync` vs expected fixed five (`rag-api`, …, `rag-search`). Product entrypoints evolved; test still frozen. |
| 8 | `tests/governance/test_physical_source_layout.py::test_canonical_preview_is_exact_and_records_phase17_conflicts` | `assert all(...)` false — phase17 conflict / exact preview mismatch. |
| 9 | `tests/governance/test_physical_source_layout.py::test_canonical_preview_records_exact_consumer_prestate_and_windows_checks` | `assert any(...)` false — consumer prestate / Windows check record missing. |
| 10 | `tests/governance/test_physical_source_layout.py::test_legacy_executor_entry_runs_from_uninstalled_checkout` | stderr missing expected phrases (`source missing or non-file` / `dirty source requires a newly approved manifest`); executor error shape changed. |
| 11 | `tests/governance/test_physical_source_layout.py::test_root_shim_and_tool_cohorts_are_fully_applied` | Cohort set mismatch: right side still expects `'documentation'`; actual has `forensics`, … without `documentation`. |
| 12 | `tests/unit/test_memory_decomplexity_plan.py::…::test_active_reader_objects_are_not_direct_remove_candidates` | `FileNotFoundError`: `integration/analysis/ai_context/memory_decomplexity_plan.json` |
| 13 | `…::test_counts_match_classification_rows` | Same missing plan JSON |
| 14 | `…::test_deprecated_archive_and_remove_candidates_have_required_fields` | Same missing plan JSON |
| 15 | `…::test_plan_is_plan_only_and_protects_active_surfaces` | Same missing plan JSON |
| 16 | `…::test_plan_states_mechanism_consolidation_not_result_winner` | Same missing plan JSON |

### Failure themes (not product KU path)

1. **Governance inventory / schema / shims / physical layout** (10 tests) — fixtures and manifests lag product renames (`pk-ku`/`pk-sync`, `rolled-back-legacy`, `private-runtime-legacy`, cohort renames).
2. **Retired pipeline contract** (1 test) — `agentsview_only` arg removed/renamed.
3. **Missing analysis artifact** (5 tests) — `memory_decomplexity_plan.json` not present under `integration/analysis/ai_context/`.

---

## 5. Smoke CLI (no paid LLM, no promote write)

| Command | Exit | Notes |
|---------|------|--------|
| `python -m personal_knowledge.application.ku workflow` | **0** | Prints daily incremental workflow + forbidden paths |
| `python -m personal_knowledge.application.ku watermark` | **0** | Read-only JSON: committed watermark present; `source_matches_watermark: false` (informational; no write) |
| `python -m personal_knowledge.application.sync --help` | **0** | `pk-sync` help; subcommands `conversations`, `help-legacy` |
| `pk-ku --help` | **0** | Installed console script works; full subcommand list |

Duration smoke total: **~2.1 s**  
Raw log: `.planning/cleanup/_test5_smoke_cli.txt`

Watermark sample (read-only, redacted to fields only):

```json
{
  "committed": "<checksum>",
  "committed_updated_at": "2026-07-11T09:00:00Z",
  "current_source_checksum": "<checksum>",
  "source_matches_watermark": false,
  "write": false
}
```

---

## Aggregate table

| Step | Command focus | Exit | Pass | Fail | Skip | ~Duration |
|------|---------------|------|------|------|------|-----------|
| 1 | `tests/unit/test_pk_ku_cli.py` | 0 | 8 | 0 | 0 | 8.6 s |
| 2 | knowledge incremental integration (2 files) | 0 | 29 | 0 | 0 | 4.8 s |
| 3 | `-k "knowledge or ku or sync or conversation"` | 0 | 282 | 0 | 0 | 57.7 s |
| 4 | `tests` full | 1 | 486 | 16 | 2 | 63.4 s |
| 5 | ku workflow / watermark / sync --help / pk-ku --help | 0 | — | — | — | 2.1 s |

Note: steps 1–3 are **subsets** of step 4; do not sum pass counts across steps.

---

## Interpretation

- **Daily product path is healthy:** `pk-ku` unit CLI, incremental pipeline/refresh integration, broader knowledge/sync/conversation filter, and smoke CLIs all succeeded.
- **Full suite debt** is concentrated in governance layout contracts and a missing decomplexity plan file — consistent with ongoing source-layout / legacy-shim evolution and artifact not checked in (or moved).
- No test hung; wall time for full suite was well under the 15–20 minute budget.

## Suggested follow-ups (not done in this run)

1. Refresh governance fixtures/schema for `rolled-back-legacy`, `private-runtime-legacy`, console scripts including `pk-ku`/`pk-sync`, and cohort labels.
2. Align `test_run_pipeline_contracts` with current argparse (`agentsview_write` vs `agentsview_only`) or mark retired pipeline tests accordingly.
3. Restore or relocate `integration/analysis/ai_context/memory_decomplexity_plan.json`, or skip those unit tests when artifact absent.
4. Fix shim inventory count (85 vs 86).

---

## Artifacts

| Path | Role |
|------|------|
| `.planning/cleanup/2026-07-16-auto-test-report.md` | This report |
| `.planning/cleanup/_test1_pk_ku_cli.txt` | Step 1 raw |
| `.planning/cleanup/_test2_knowledge_incremental.txt` | Step 2 raw |
| `.planning/cleanup/_test3_knowledge_ku_sync.txt` | Step 3 raw |
| `.planning/cleanup/_test4_full_pytest.txt` | Step 4 raw |
| `.planning/cleanup/_test5_smoke_cli.txt` | Step 5 raw |
