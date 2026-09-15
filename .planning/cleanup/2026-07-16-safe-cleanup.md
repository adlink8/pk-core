# Safe cleanup log — 2026-07-16

**Scope:** confirmed execution of low/medium cleanup steps from GSD map  
(`.planning/codebase/CONCERNS.md`).  
**Not in scope:** domains facade deletion, `tools/compat` retirement, archive physical delete.  
**CLI canary/watermark:** done 2026-07-16 (`pk-ku canary` / `pk-ku watermark`) — no longer deferred.

## Done

### 1. `integration/scripts` bytecode

- Removed **12** `__pycache__` directories under `integration/scripts/`
- Remaining `*.pyc` in that tree: **0**
- Kept live source: `integration/scripts/governance/*.py` only

### 2. Docs

- Rewrote `integration/scripts/README.md` to product CLI (`pk-sync` / `pk-ku` / `rag-*`)
- Explicit “do not use” old `integration/scripts/build_*.py` and full-inventory daily path

### 3. Phase 20 bak quarantine (move, not delete)

All of the following **moved** to:

`archive/quarantine/bak-phase20-20260716/`

| Former path | Quarantine name |
|-------------|-----------------|
| `_recycle.bak-phase20` | `_recycle.bak-phase20` (~4.2 GiB) |
| `.ai-bridge.bak-phase20` | `.ai-bridge.bak-phase20` |
| `.gsd.bak-phase20` | `.gsd.bak-phase20` |
| `Agent.bak-phase20` | `Agent.bak-phase20` |
| `Google.bak-phase20` | `Google.bak-phase20` |
| `imports.bak-phase20` | `imports.bak-phase20` |
| `logs.bak-phase20` | `logs.bak-phase20` |
| `integration/analysis.bak-phase20` | `integration_analysis.bak-phase20` |
| `integration/db.bak-phase20` | `integration_db.bak-phase20` |
| `integration/raw_index.bak-phase20` | `integration_raw_index.bak-phase20` |
| `integration/runtime.bak-phase20` | `integration_runtime.bak-phase20` |
| `integration/structured.bak-phase20` | `integration_structured.bak-phase20` |

Local MANIFEST: `archive/quarantine/bak-phase20-20260716/MANIFEST.md`  
(gitignored under `archive/**`; this file is the tracked summary).

**Repo root / `integration/` no longer have `*.bak-phase20` directories.**

## Explicitly NOT done (still deferred)

| Item | Window / gate |
|------|----------------|
| Delete `src/personal_knowledge/domains/*` facades | After **2026-08-13** + zero business imports |
| Delete `tools/compat/v1_1` | Shim budget + consumer=0 |
| Physical delete of `archive/` | Owner + retention journal only |

## Done later same day (was deferred)

| Item | Status |
|------|--------|
| `pk-ku canary` / `watermark` CLI | **Done 2026-07-16** — product subcommands on `pk-ku` (see `docs/runbooks/ku-incremental.md`) |

## Recovery

If a bak tree is needed:

```powershell
# example
Move-Item archive\quarantine\bak-phase20-20260716\integration_db.bak-phase20 integration\db.bak-phase20
```

## Verification

```powershell
# no bak at product roots
Get-ChildItem -Directory -Force | Where-Object Name -like '*bak-phase20*'
Get-ChildItem integration -Directory -Force | Where-Object Name -like '*bak-phase20*'

# scripts: governance py only + no pycache
Get-ChildItem integration\scripts -Recurse -Filter __pycache__
Get-ChildItem integration\scripts -Recurse -Filter *.py
```
