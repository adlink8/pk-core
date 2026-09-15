# Tests
## Responsibility
Deterministic unit, contract, pipeline, lifecycle, retrieval and governance checks.
## Boundaries
Default discovery is limited to `tests/` (`pytest.ini`); no raw exports as fixtures.
Some governance path tests may read local private paths when present (machine-local).
## Entry points
```powershell
python -m pytest -q
python -m pytest -q tests/governance/
python -m pytest -q tests/contract/
```
Cache directory: `var/cache/pytest` (Phase 20).
## I/O and privacy
Prefer `tmp_path`, in-memory SQLite and synthetic fixtures; never commit personal evidence.
Live path resolution uses `personal_knowledge.core.project_paths` (`data/`, `var/`).
## Tests
Phase UAT / human live proof lives in `.planning/phases/*/…-UAT.md`.
## Ownership
Owner: quality. Status: supported. Last reviewed: Phase 20 (2026-07-13).

