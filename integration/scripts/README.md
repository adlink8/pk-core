# integration/scripts

**Status:** governance + historical layout only (post Phase 19–21).

## What lives here

| Path | Role |
|------|------|
| `governance/` | Repository preflight, shim budget, path/docs/planning checks, migration helpers |
| Other package dirs (`knowledge/`, `memory/`, …) | **Empty shells / historical** — product code is **not** here |

Product implementation lives under:

```text
src/personal_knowledge/
```

## Product CLI (use these)

```powershell
pip install -e .

# Dialogue SSOT
pk-sync conversations
pk-sync conversations --write

# Knowledge units (incremental)
pk-ku workflow
pk-ku inspect
pk-ku prepare --model … --provider … --endpoint … --auth-mode …
pk-ku extract --run ir_* --max-items N
pk-ku extract-gate --run ir_*
pk-ku canonical --run ir_* --write
pk-ku publish --run ir_* --write
pk-ku vector --write
pk-ku canary --candidate-override <coll> --report path.json
pk-ku promote --list
pk-ku watermark
pk-ku watermark --advance --from-canonical --write

# Search / services
rag-search …
rag-api
rag-mcp
rag-dashboard
```

Module form (same code):

```powershell
python -m personal_knowledge.application.sync conversations --write
python -m personal_knowledge.application.ku inspect
```

## Do **not** use for daily ops

| Path / command | Why |
|----------------|-----|
| `python integration/scripts/build_*.py` | Shims moved; bodies are in `src/` |
| `tools/compat/v1_1/*.py` | Compatibility only; not product docs |
| `rag-pipeline` | Retired unless `PK_ALLOW_LEGACY_PIPELINE=1` forensics |
| Full inventory + prod `--start` as daily KU | Forbidden — see `docs/runbooks/ku-incremental.md` |

## Compatibility shims

Temporary re-exports: `tools/compat/v1_1/` (budget-gated).  
Domain facades: `src/personal_knowledge/domains/*` remain as **optional re-export only**.  
**2026-07-16:** `application/**` → `domains` **real imports = 0** (SCHEMA_SQL lives in `application.knowledge.migrate_add_knowledge_unit_tables`).  
New code **must** import `personal_knowledge.application.*` / `evaluation.*` — never new `domains.*` from product code.

## Governance

```powershell
python integration/scripts/governance/preflight.py
python integration/scripts/governance/check_shim_budget.py --check
```

## Docs

- Agent manual: `docs/AGENTS.md`
- KU runbook: `docs/runbooks/ku-incremental.md`
- Zones: `docs/architecture/repository-zones.md`
- Cleanup map: `.planning/codebase/CONCERNS.md`
