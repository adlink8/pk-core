---
mapped_at: 2026-07-16
last_mapped_commit: ce6dfde
focus: tech
---

# INTEGRATIONS — External Systems and Service Boundaries

Evidence: `src/personal_knowledge/**`, `apps/personal_data_chatgpt/**`, `docs/AGENTS.md`, `docs/architecture/retrieval-ssot.md`, `docs/runbooks/*`, `AGENTS.md` (workspace root).

## 1. Integration map (summary)

```text
AgentsView live SQLite (RO)
        │  pk-sync conversations
        ▼
canonical dialogue SSOT  ──►  Vertex Gemini / OpenAI-compat (pk-ku extract)
        │                              │
        │                              ▼
        │                     KU tables in personal_system.sqlite
        │                              │
        │                     local bge embed ──► Chroma :8001
        │                              │
        └──────── unified_search ◄─────┘
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
     rag-search    REST :8000   stdio MCP (rag-mcp)
                       │
                       ▼
              Node MCP Apps :8789
                       │
                       ▼
         OpenAI Secure Tunnel :8081 ──► ChatGPT connector
```

Google Takeout / structured Google feeds non-dialogue events and assertions into the same retrieval surface (layered hybrid).

## 2. AgentsView (inbound dialogue source)

| Field | Detail |
|-------|--------|
| System | AgentsView daemon sessions store |
| Locator | `%USERPROFILE%\.agentsview\sessions.db` → `project_paths.AGENTSVIEW_DB` |
| Code | `src/personal_knowledge/adapters/agentsview.py` |
| Direction | **Inbound only**, read-only |
| Product entry | **`pk-sync conversations`** → `application/sync.py` → `run_pipeline.run_agentsview_stage` |
| Outputs | `agentsview_normalized.sqlite`, **`agent_conversations.sqlite`** under `data/canonical/agent/structured/db/` |

Hard constraints (from adapter docstring + AGENTS):

- Connect with SQLite URI **`mode=ro`** + **`PRAGMA query_only=ON`**.
- Snapshot via **SQLite backup API** (not raw copy of `.db`+WAL+SHM).
- Required tables: `sessions`, `messages`, `tool_calls`, `tool_result_events`, `usage_events`, `secret_findings`, `excluded_sessions`.
- Pre-flight abort on missing schema / failed integrity — no silent empty success.
- **Never** relocate, migrate, index, VACUUM, or write the live DB.
- Insights from AgentsView must **not** override knowledge SSOT (KU).

SSOT after sync: dialogue evidence lives in canonical project SQLite, not the live path.

## 3. Google activity / Takeout (inbound non-dialogue)

| Field | Detail |
|-------|--------|
| Raw | `data/raw/google/` (Phase 20); legacy Takeout under archives |
| Structured DB | `data/canonical/google/structured/db/google_data.sqlite` (`GOOGLE_DB`) |
| Adapter sample | `src/personal_knowledge/adapters/google_activities.py` (contract sample; not sole pipeline) |
| Application | `application/build_google_*`, `google_structure_lifecycle.py`, light assertions |
| Retrieval | Google assertions endpoints; hybrid layer prefers Google `personal_events` for non-dialogue pad |

Privacy: raw activity is personal (R4); light assertions are derived and must remain traceable.

## 4. GPT export (legacy / archived inbound)

| Field | Detail |
|-------|--------|
| Status | Soft-archived under recycle cleanup paths (`project_paths.GPT_DB`) |
| Role | Historical ChatGPT export compatibility for import pipeline; **not** live product source |
| Product path | Prefer AgentsView → canonical; do not treat GPT archive as active dialogue SSOT |

## 5. gcloud / Vertex AI Gemini (outbound LLM)

| Field | Detail |
|-------|--------|
| Purpose | Knowledge-unit extraction, repair/judge paths that call Gemini on Vertex |
| Auth | `gcloud auth print-access-token` via `runtime_config.gcloud_access_token()` |
| Config | `PERSONAL_DATA_GCLOUD`, `PERSONAL_DATA_GCP_PROJECT`, `PERSONAL_DATA_VERTEX_LOCATION`, `PERSONAL_DATA_VERTEX_MODEL` |
| Defaults | location `global`; model `gemini-3.5-flash-lite` |
| Endpoint | `https://aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent` |
| Primary callers | `application/knowledge/build_knowledge_units_prod.py`, `build_knowledge_units.py`, `test_knowledge_unit_llm.py`, prepare validation in `refresh_knowledge_units.py` |
| Product entry | **`pk-ku prepare` / `pk-ku extract`** (not auto-paid on bare inspect) |

Provider registry keys in refresh pipeline:

| Provider | Auth mode | Endpoint pattern |
|----------|-----------|------------------|
| `vertex_google` | `gcloud` | `aiplatform.googleapis.com` |
| `openai` | `api_key` | `api.openai.com` (or compatible base) |
| `google_free` | `api_key` | `generativelanguage.googleapis.com` |

Operator notes (`docs/runbooks/ku-incremental.md`):

- Verify token length without printing secrets.
- Daily path is **incremental only**; full inventory + prod `--start` is not a `pk-ku` subcommand.

Data boundary: user evidence text may leave the machine to Google when extract runs — treat as sensitive outbound; no keys in logs.

## 6. OpenAI-compatible LLM (outbound)

| Field | Detail |
|-------|--------|
| Client | `core/llm.py` → `openai.OpenAI` |
| Env | `OPENAI_API_KEY` or `MEM0_API_KEY`; `OPENAI_BASE_URL` (default MiMo `https://token-plan-cn.xiaomimimo.com/v1`) |
| Proxy | `HTTPS_PROXY` / `HTTP_PROXY` → `httpx.Client` injection |
| Use cases | Conversation summary / memory experiment / evaluation paths that share `make_llm_client`; provider mode `openai` in KU prepare when configured |

Never log API keys or full Authorization headers.

## 7. Local embedding model (internal compute)

| Field | Detail |
|-------|--------|
| Module | `src/personal_knowledge/core/local_embed.py` |
| Model | `bge-small-zh-v1.5` (BAAI), 512-d |
| Network | Offline-first (`TRANSFORMERS_OFFLINE` / `HF_HUB_OFFLINE`) |
| Consumers | Vector build (`retrieval/build_vector_store.py`, KU vector), semantic search via REST-loaded model |

No remote embedding API in the product path for index build.

## 8. Chroma vector store (internal service)

| Field | Detail |
|-------|--------|
| Protocol | HTTP REST API v2 |
| Client | `src/personal_knowledge/core/chroma_client.py` (`requests`, `trust_env=False`) |
| Bind | Default `127.0.0.1:8001` |
| Heartbeat | `GET /api/v2/heartbeat` |
| Active pointer | `var/db/knowledge_index_active.txt` |
| Product | `pk-ku vector` builds **candidate**; `pk-ku promote` flips active after eval |

Collections used by hybrid retrieval (`retrieval/_constants.py`, `semantic_search.py`):

- Active **KU** collection (name from pointer)
- `conversation_turns`
- `canonical_messages`
- Legacy / raw `personal_events` (fallback layers)

Chroma holds embeddings + document snippets — **retrieval feature store**, not knowledge fact SSOT.

## 9. REST API (outbound local surface)

| Field | Detail |
|-------|--------|
| Entry | `rag-api` → `src/personal_knowledge/services/api_server.py` |
| Bind | Default `127.0.0.1:8000` (stdlib `ThreadingHTTPServer`) |
| Backend | `retrieval.unified_search` (same as CLI / MCP) |
| Privacy | Responses pass `privacy_guard` |

Key routes (from module docstring):

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Health + knowledge active collection |
| GET | `/stats` | DB + vector + knowledge stats |
| GET | `/knowledge`, `/knowledge/status` | KU index status |
| POST | `/search/semantic` | Knowledge-first hybrid semantic search |
| POST | `/search/query` | Structured SQLite filters |
| GET | `/memory`, `/memory/<subject>` | Memory overview (experimental layer) |
| GET | `/google/assertions` | Light Google assertions |
| GET | `/profile` | Long-context profile doc for RAG inject |
| GET | `/event/<event_id>` | Single event |

No built-in auth — external bind requires reverse proxy + auth (policy, not implemented in server).

## 10. Stdio MCP server (local AI clients)

| Field | Detail |
|-------|--------|
| Entry | `rag-mcp` → `src/personal_knowledge/services/mcp_server.py` |
| Transport | MCP **stdio** (`mcp` Python SDK) |
| Profile | `PERSONAL_DATA_MCP_PROFILE=core|full` |
| Semantic path | Prefers loopback REST (`PERSONAL_DATA_SEMANTIC_API`) so embed model stays in API process |
| Proxy fix | Forces `NO_PROXY` for `127.0.0.1,localhost` |

Core tools (approx.): `search_semantic`, `knowledge_status`, `stats`, Google assertion tools, memory profile tools, `data_*` browser tools. Full profile adds legacy aliases (`query_events`, `list_categories`, …).

Workspace MCP label in root `AGENTS.md`: `personal-data` → often the **HTTP** Apps MCP at `http://127.0.0.1:8789/mcp` (not stdio). Stdio MCP is for Claude Desktop / Cursor-style clients.

## 11. ChatGPT MCP Apps + Secure Tunnel (outbound bridge)

### 11.1 Node HTTP MCP Apps adapter

| Field | Detail |
|-------|--------|
| Path | `apps/personal_data_chatgpt/server.mjs` |
| Port | **8789** (`HOST`/`PORT` env) |
| Protocol | MCP over HTTP (`PROTOCOL_VERSION` 2025-06-18 in server) |
| Upstream | Proxies to REST `PERSONAL_DATA_REST_URL` default `http://127.0.0.1:8000` |
| Widgets | Memory graph, relation review, data browser HTML under `public/` |
| Docs | `apps/personal_data_chatgpt/README.md` |

Tools are read-only; annotations set `readOnlyHint: true`, `destructiveHint: false`. Widget tools use `_meta.ui.resourceUri` + ChatGPT `openai/outputTemplate` alias.

### 11.2 OpenAI Secure MCP Tunnel

| Field | Detail |
|-------|--------|
| Binary | External `tunnel-client.exe` (launcher default dir machine-local) |
| Profile | `personal-data-app` YAML under `%APPDATA%\tunnel-client\` (synced to `~\.config\tunnel-client\`) |
| Health | `127.0.0.1:8081` (`/healthz`, `/ui`) |
| MCP target | `http://127.0.0.1:8789/mcp` |
| Control plane | Reaches OpenAI (`api.openai.com`); needs proxy when China network requires it |
| Runbook | `apps/personal_data_chatgpt/TUNNEL_RUNBOOK.md` |
| Launcher | `scripts/start-services.ps1` — REST → MCP → Tunnel; tunnel env `HTTPS_PROXY=http://127.0.0.1:7897`, `NO_PROXY=127.0.0.1,localhost` |

Credentials: `CONTROL_PLANE_API_KEY` / tunnel id — environment or secure store only; **never** commit.

ChatGPT side: Platform tunnel connector → selected tunnel id → tools appear in ChatGPT.

## 12. Streamlit dashboard (local UI)

| Field | Detail |
|-------|--------|
| Entry | `rag-dashboard` → `services/dashboard.py` |
| Deps | streamlit, pandas, plotly (optional requirements) |
| Data | Reads integrated SQLite / probes Chroma |
| Bind | Local Streamlit process only; not part of ChatGPT tunnel trio |

## 13. Import drop zone (filesystem inbound)

| Field | Detail |
|-------|--------|
| Tree | `data/imports/{incoming,batches,duplicate_audit}/` |
| Pipeline | `application/run_import_pipeline.py` and related |
| Rules | Atomic intake, hash/duplicate audit, quarantine; never execute imported code |

## 14. Shared retrieval contract (cross-surface)

All human/AI-facing search surfaces must share **`personal_knowledge.retrieval.unified_search`** / `semantic_search`:

| Surface | Entry |
|---------|--------|
| CLI | `rag-search` → `retrieval.unified_search._cli` |
| REST | `services/api_server.py` |
| Stdio MCP | `services/mcp_server.py` |
| Apps MCP | Node → REST → same backend |

SSOT layers (`docs/architecture/retrieval-ssot.md`):

| Layer | Authority |
|-------|-----------|
| Dialogue | AgentsView RO → `agent_conversations.sqlite` |
| Knowledge | `canonical_knowledge_units` + active Chroma collection |
| Non-dialogue raw | `personal_events` / unified events (transition) |

Default hybrid: **layered** = KU → dialogue (`canonical_messages` / `conversation_turns`) → Google PE → optional legacy pad. Env: `PERSONAL_DATA_FALLBACK_POLICY`.

## 15. Product CLI integrations (orchestration, not external SaaS)

| CLI | Integrates |
|-----|------------|
| **`pk-sync`** | AgentsView → normalized → canonical dialogue |
| **`pk-ku`** | Dialogue SSOT → Vertex/LLM extract → KU SQLite → Chroma candidate → promote |
| **`rag-search` / `rag-api` / `rag-mcp`** | Read path over SSOT + Chroma |
| **`rag-pipeline`** | **Retired** integrated batch; blocked by default |

Daily order (docs/AGENTS + runbooks):

```text
pk-sync conversations [--write]
pk-ku inspect → prepare → extract → … → promote
```

## 16. Credential and network policy (integration checklist)

| Rule | Why |
|------|-----|
| Loopback bind by default | REST / Apps / Tunnel health / Chroma |
| Credentials only in env / credential store | gcloud user login, OpenAI keys, tunnel control-plane key |
| No write to AgentsView live DB | External ownership + WAL |
| Tunnel proxy ≠ REST/MCP proxy | Loopback must use `NO_PROXY` |
| Outbound LLM is opt-in paid path | `pk-ku extract` after prepare; inspect is free |
| Privacy guard on REST/MCP/Apps egress | Seal credentials/PII patterns |
| Do not commit `data/` / `var/` private DBs | Root AGENTS + privacy policies |

## 17. Health check commands (Windows)

```powershell
curl.exe --noproxy "*" http://127.0.0.1:8000/health
curl.exe --noproxy "*" http://127.0.0.1:8789/health
curl.exe --noproxy "*" http://127.0.0.1:8081/healthz
# Chroma (if running)
curl.exe --noproxy "*" http://127.0.0.1:8001/api/v2/heartbeat
```

Start trio:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File "apps\personal_data_chatgpt\scripts\start-services.ps1"
```

Logs: `apps/personal_data_chatgpt/logs/{rest-api,mcp-app,tunnel,watchdog}.log`.

## 18. File path index (integration code)

| Integration | Primary files |
|-------------|----------------|
| AgentsView adapter | `src/personal_knowledge/adapters/agentsview.py` |
| Sync product CLI | `src/personal_knowledge/application/sync.py` |
| KU product CLI | `src/personal_knowledge/application/ku.py` |
| KU refresh / provider validation | `src/personal_knowledge/application/knowledge/refresh_knowledge_units.py` |
| Vertex extract workers | `src/personal_knowledge/application/knowledge/build_knowledge_units_prod.py` |
| Runtime Vertex/embed config | `src/personal_knowledge/core/runtime_config.py` |
| OpenAI-compat client | `src/personal_knowledge/core/llm.py` |
| Chroma client | `src/personal_knowledge/core/chroma_client.py` |
| Paths / DBs | `src/personal_knowledge/core/project_paths.py` |
| Unified search | `src/personal_knowledge/retrieval/unified_search.py`, `semantic_search.py` |
| REST | `src/personal_knowledge/services/api_server.py` |
| Stdio MCP | `src/personal_knowledge/services/mcp_server.py` |
| Apps MCP | `apps/personal_data_chatgpt/server.mjs` |
| Tunnel ops | `apps/personal_data_chatgpt/TUNNEL_RUNBOOK.md`, `scripts/start-services.ps1` |
