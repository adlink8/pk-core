# var/ — Phase 20 runtime / DB / reports / logs

| Path | Content |
|------|---------|
| `db/` | SQLite/DuckDB + active KU pointer + structured CSV |
| `runtime/` | Private evals / governance runtime artifacts |
| `reports/analysis/` | stage1_profile, ai_context, evaluations |
| `logs/` | Process logs |
| `cache/pytest/` | pytest cache (`pytest.ini` → `cache_dir`) |
| `phase20-journals/` | Migration journals (must stay outside moved trees) |

## Key files

| File | Role |
|------|------|
| `db/personal_system.sqlite` | Unified knowledge + memory SSOT |
| `db/knowledge_index_active.txt` | Active Chroma collection name |
| `db/conversation_graph.duckdb` | Conversation graph |
| `db/evaluation_registry.sqlite` | Phase 17 eval run registry |
| `reports/analysis/evaluations/` | Knowledge eval HTML/JSON runs |

Legacy `integration/db`, `integration/runtime`, `integration/analysis`, `logs/` cut over **2026-07-13**.
