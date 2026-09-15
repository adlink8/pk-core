# Milestone v1.0 Verification Runbook

**Date:** 2026-07-12  
**Purpose:** Closeout verification after GSD planning alignment.  
**Result:** **PASS**

## Automated gates

| # | Command | Expect | Result |
|---|---------|--------|--------|
| V1 | `python -m pytest -q` | all green; no `_recycle` collection | **380 passed** |
| V2 | incremental + google + search contracts | all green | **51 passed** |
| V3 | `refresh_knowledge_units.py --prepare ...` | `no_op: true`, zero writes | **PASS** (`no_op: true`, checksums equal, 0 writes) |
| V4 | `refresh_knowledge_units.py --sandbox-ku08` | `ok: true`, live active untouched | **PASS** (after sandbox workdir wipe fix) |
| V5 | `phase15_02_holdout_eval.py --offline-smoke` | telemetry schema ok | **PASS** |
| V6 | `get_knowledge_status` / google structure | layered + counts | **PASS** (active 30012, google 1696/1696/48) |

## Live status probe

```text
fallback_policy: layered
allow_legacy_pad: True
ssot: dialogue=agentsview_canonical, knowledge=canonical_knowledge_units,
      non_dialogue_raw=personal_events, google_structure=normalized+assertions
active: knowledge_units_run_76c6259e_20260712062418
units: 30012
google: activities=1696, normalized_events=1696, light_assertions=48
privacy_policy_version: service_and_category_v1
```

## Planning gates

| Artifact | Result |
|----------|--------|
| ROADMAP 01–16 complete/cancelled | **PASS** |
| REQUIREMENTS KU-08 + SSOT-01..06 + GL-01..05 | **PASS** |
| 14/15/16 SUMMARY + VERIFICATION | **PASS** |
| MILESTONE-v1.0.md | **PASS** |
| STATE.md milestone_complete | **PASS** |

## Fixes during verification

- `run_sandbox_ku08_e2e`: wipe prior sandbox sqlite files before recreate (idempotent re-run).

## Sign-off

- [x] Automated suite green (380)
- [x] KU-08 prepare no_op + sandbox E2E
- [x] Holdout telemetry smoke
- [x] Live SSOT surfaces consistent with docs
- [x] GSD planning aligned to v1.0
