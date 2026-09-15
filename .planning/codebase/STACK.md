---
mapped_at: 2026-07-16
last_mapped_commit: ce6dfde
focus: tech
---

# STACK — Languages, Runtimes, Dependencies, Data Stores

Evidence: `pyproject.toml`, `requirements*.txt`, `constraints.txt`, `governance/policies/dependencies.yaml`, `src/personal_knowledge/**`, `apps/personal_data_chatgpt/**`, `docs/AGENTS.md`, `docs/runbooks/*`.

## 1. Languages and runtimes

| Layer | Runtime | Contract / notes |
|-------|---------|------------------|
| Product Python | **Python ≥3.11** (`pyproject.toml`); policy supports **3.12 / 3.14** (`governance/policies/dependencies.yaml`) | Package install: editable `pip install -e .` with `src/` layout |
| ChatGPT MCP Apps | **Node.js ≥20** (`apps/personal_data_chatgpt/package.json` `engines`) | ES modules (`"type": "module"`); no npm runtime deps |
| Shell / ops | **Windows PowerShell** | Service launcher `apps/personal_data_chatgpt/scripts/start-services.ps1`; bat wrappers 启动/停止服务 |
| External CLI | **gcloud** (Google Cloud SDK) | Vertex token via `gcloud auth print-access-token` (`src/personal_knowledge/core/runtime_config.py`) |
| External binary | **tunnel-client.exe** | OpenAI Secure MCP Tunnel (outside repo; path configured in start script) |

OS notes (Windows):

- Primary host is Windows; paths use `%USERPROFILE%`, drive letters only as optional embedding-model discovery (`runtime_config.embedding_model_path`).
- Loopback services bind `127.0.0.1` by default (REST 8000, MCP Apps 8789, Tunnel health 8081, Chroma 8001).
- Local proxy often `http://127.0.0.1:7897` for tunnel → `api.openai.com`; `NO_PROXY=127.0.0.1,localhost` so loopback never goes through proxy.
- AgentsView live DB: `%USERPROFILE%\.agentsview\sessions.db` — **read-only**, never relocate (`project_paths.AGENTSVIEW_DB`).

## 2. Package managers and install contracts

### Python

| File | Role |
|------|------|
| `pyproject.toml` | Package name `personal-knowledge` 0.1.0; setuptools backend; console scripts |
| `requirements.txt` | **Core** runtime: pydantic, requests, PyYAML, mcp |
| `requirements-optional.txt` | UI/analysis + hosted LLM + local embed stack |
| `requirements-dev.txt` | `-c constraints.txt` + core + pytest / pytest-asyncio |
| `constraints.txt` | Audited pins for 3.12 and 3.14 |
| `governance/policies/dependencies.yaml` | Install contract SSOT |

```text
# Dev / CI-style install
python -m pip install -c constraints.txt -r requirements-dev.txt
# Optional features (dashboard, openai client, sentence-transformers)
python -m pip install -c constraints.txt -r requirements-optional.txt
# Package + entry points
pip install -e .
```

### Node

| File | Role |
|------|------|
| `apps/personal_data_chatgpt/package.json` | App manifest; scripts `start` / `test` |
| `apps/personal_data_chatgpt/package-lock.json` | lockfileVersion 3; **no third-party packages** (stdlib only) |

```text
npm ci --ignore-scripts   # policy contract; effectively no deps to install
npm start                 # node server.mjs
npm test                  # node --test
```

## 3. Declared Python dependencies

### Core (`requirements.txt` + `constraints.txt` pins)

| Package | Constraint pin | Used for |
|---------|----------------|----------|
| `pydantic` | 2.12.5 | Schemas / validation |
| `requests` | 2.32.5 | Chroma REST client (`core/chroma_client.py`); HTTP utilities |
| `PyYAML` | 6.0.3 | Config / manifests |
| `mcp` | 1.27.0 | Stdio MCP server (`services/mcp_server.py`) |

### Optional (`requirements-optional.txt`)

| Package | Pin | Used for |
|---------|-----|----------|
| `numpy` | 2.4.1 | Analysis / vector math |
| `pandas` | 3.0.0 | Dashboard tables |
| `matplotlib` | 3.10.8 | Charts |
| `streamlit` | 1.58.0 | Local dashboard (`services/dashboard.py`) |
| `plotly` | 6.8.0 | Interactive graphs in dashboard |
| `openai` | 2.35.1 | OpenAI-compatible chat client (`core/llm.py`) |
| `httpx` | 0.28.1 | Explicit proxy client for OpenAI SDK when `HTTPS_PROXY` set |
| `sentence-transformers` | 5.5.1 | Local embeddings (`core/local_embed.py`) |

### Dev (`requirements-dev.txt`)

| Package | Pin |
|---------|-----|
| `pytest` | 9.0.2 |
| `pytest-asyncio` | 1.4.0 |

### Used in code but **not** in requirements files

| Import | Where | Notes |
|--------|-------|-------|
| `duckdb` | `application/conversation/build_conversation_graph.py`, `query_conversation_graph.py`, `visualize_conversation_graph.py`; memory eval audit | Graph artifact `conversation_graph.duckdb`; **install separately** if using graph tooling |
| `torch` | Via `sentence-transformers` / `local_embed.verify_model` | Transitive; CUDA optional (`PERSONAL_DATA_EMBED_DEVICE`) |
| stdlib only | `services/api_server.py` (`http.server`), SQLite, `urllib` for Vertex | No FastAPI/Flask |

Do **not** invent undeclared packages. Prefer `constraints.txt` when adding pins.

## 4. Console entry points (`pyproject.toml` `[project.scripts]`)

| Command | Target | Product role |
|---------|--------|--------------|
| **`pk-sync`** | `personal_knowledge.cli:sync` | **Product** conversation sync → SSOT |
| **`pk-ku`** | `personal_knowledge.cli:ku` | **Product** KU incremental (inspect/prepare/extract/…/promote) |
| `rag-search` | `personal_knowledge.cli:search` | Hybrid retrieval CLI |
| `rag-api` | `personal_knowledge.cli:api` | REST API process |
| `rag-mcp` | `personal_knowledge.cli:mcp` | Stdio MCP server |
| `rag-dashboard` | `personal_knowledge.cli:dashboard` | Streamlit dashboard |
| `rag-pipeline` | `personal_knowledge.cli:pipeline` | **Retired** integrated batch; exit 2 unless `PK_ALLOW_LEGACY_PIPELINE=1` |

Implementation hub: `src/personal_knowledge/cli.py`.

Product subcommands (not only `rag-*`):

```text
pk-sync conversations [--write]
pk-sync help-legacy

pk-ku inspect | prepare | extract | status | extract-gate
pk-ku canonical | publish | vector | promote | workflow
```

See `src/personal_knowledge/application/sync.py`, `application/ku.py`, `docs/runbooks/product-sync.md`, `docs/runbooks/ku-incremental.md`.

## 5. Package layout (Python product)

Root: `src/personal_knowledge/`

| Package area | Path | Role |
|--------------|------|------|
| CLI | `cli.py` | Console scripts |
| Core | `core/` | paths, LLM, embed, chroma client, privacy, runtime_config |
| Adapters | `adapters/` | AgentsView RO adapter; Google sample adapter |
| Application | `application/` | Product flows: sync, ku, knowledge/conversation/memory/graph pipelines |
| Domains | `domains/` | Domain-oriented modules (conversation, knowledge, memory, graph) |
| Retrieval | `retrieval/` | unified_search, semantic_search, vector build/eval |
| Services | `services/` | api_server, mcp_server, dashboard |
| Evaluation | `evaluation/` | KU/conversation/memory/vector eval |
| Governance | `governance/` | migration / preflight helpers |

Path SSOT: `src/personal_knowledge/core/project_paths.py` (Phase 20 prefers `data/`, `var/`).

## 6. Models and LLM providers

### 6.1 Extraction / judging (outbound)

| Provider key | Auth | Endpoint pattern | Default model | Config |
|--------------|------|------------------|---------------|--------|
| **`vertex_google`** | gcloud access token | `https://aiplatform.googleapis.com` | `gemini-3.5-flash-lite` | `PERSONAL_DATA_GCP_PROJECT`, `PERSONAL_DATA_VERTEX_LOCATION` (default `global`), `PERSONAL_DATA_VERTEX_MODEL`, `PERSONAL_DATA_GCLOUD` |
| **`openai`** | API key | OpenAI-compatible / `api.openai.com` | gpt-*/o* models (allowlist) | `OPENAI_API_KEY` or `MEM0_API_KEY`, `OPENAI_BASE_URL` |
| **`google_free`** | API key | `generativelanguage.googleapis.com` | gemini* | Provider validation in `refresh_knowledge_units.py` |

Provider allowlists: `PROVIDER_MODEL_ALLOWLIST` / `PROVIDER_ENDPOINT_PATTERNS` in `src/personal_knowledge/application/knowledge/refresh_knowledge_units.py`.

Vertex call path (prod extract):

- Token: `gcloud_access_token()` in `core/runtime_config.py`
- HTTP: `urllib` POST `…/publishers/google/models/{model}:generateContent` in `application/knowledge/build_knowledge_units_prod.py` (and related build/test modules)
- Example product flags:  
  `pk-ku prepare --model gemini-3.5-flash-lite --provider vertex_google --endpoint https://aiplatform.googleapis.com --auth-mode gcloud`

OpenAI-compatible path:

- `core/llm.py` → `openai.OpenAI`, default base `https://token-plan-cn.xiaomimimo.com/v1` if `OPENAI_BASE_URL` unset
- Proxy: inject `httpx.Client(proxy=…)` when `HTTPS_PROXY` / `HTTP_PROXY` set
- UA spoof `curl/8.0` for some third-party gateways

### 6.2 Local embeddings (offline)

| Item | Value |
|------|--------|
| Model | **BAAI/bge-small-zh-v1.5** |
| Dim | **512** |
| Library | `sentence-transformers` (`core/local_embed.py`) |
| Offline | `TRANSFORMERS_OFFLINE=1`, `HF_HUB_OFFLINE=1` by default |
| Device | `PERSONAL_DATA_EMBED_DEVICE` (default `cuda`, fallback CPU) |
| Path | `PERSONAL_DATA_EMBED_MODEL_PATH` or discovery via ModelScope/HF cache / `{drive}:/models/…` (`runtime_config.embedding_model_path`) |

Vectors are **not** fact SSOT; knowledge SSOT is KU tables + active Chroma pointer.

### 6.3 Prompt assets

Versioned prompt trees under `assets/prompts/` (e.g. `knowledge_unit_extractor/`, `memory_*`, `graph_*`, `gate_repair_loop/`).

## 7. Databases and vector store

### SQLite (primary)

| Artifact | Path (Phase 20 preferred) | Role |
|----------|---------------------------|------|
| Dialogue SSOT | `data/canonical/agent/structured/db/agent_conversations.sqlite` | Canonical agent conversations |
| AgentsView normalized | `…/agentsview_normalized.sqlite` | Snapshot/normalized intermediate |
| Agent evidence | `data/canonical/agent/structured/db/agent_data.sqlite` | Agent structured DB |
| Google structured | `data/canonical/google/structured/db/google_data.sqlite` | Google activity DB |
| Integrated system | `var/db/personal_system.sqlite` (`UNIFIED_DB`) | unified_events, KU tables, memory experimental, personal_events |
| Active KU pointer | `var/db/knowledge_index_active.txt` | Points at promoted Chroma collection name |
| Eval registry | under `var/db/` / legacy `integration/db/` | Evaluation registry artifacts |
| AgentsView **live** | `%USERPROFILE%\.agentsview\sessions.db` | External WAL; RO only |

### DuckDB

| Artifact | Path | Role |
|----------|------|------|
| Conversation graph | `var/db/conversation_graph.duckdb` (`CONV_GRAPH_DB`) | Graph analysis / query / visualize |

### Chroma (vector)

| Item | Value |
|------|--------|
| API | REST **v2** (`/api/v2/tenants/.../databases/...`) |
| Client | Custom `requests`-based `core/chroma_client.py` (**not** official `chromadb` Python package) |
| Default host/port | `127.0.0.1:8001` (`_KU_PORT` in `retrieval/_constants.py`) |
| Space | cosine (`hnsw:space: cosine`) |
| Notable collections | Active KU generation (name in pointer file); `personal_events`; `conversation_turns`; `canonical_messages` |

Candidate build / promote: `pk-ku vector`, `pk-ku promote` → never mutate active mid-extract.

### Eval fixtures

- Public/synthetic: `assets/evals/knowledge_units/`
- Runtime private suites: under `var/runtime/` (not for Git)

## 8. Local service ports (product trio + Chroma)

| Service | Port | Process / entry | Health |
|---------|------|-----------------|--------|
| REST API | **8000** | `rag-api` / `personal_knowledge.services.api_server` | `GET /health` |
| GPT Apps MCP | **8789** | `node apps/personal_data_chatgpt/server.mjs` | `GET /health`, MCP `POST /mcp` |
| Tunnel health | **8081** | `tunnel-client.exe` | `GET /healthz` |
| Chroma | **8001** | External Chroma server process | `/api/v2/heartbeat` |

Launcher: `apps/personal_data_chatgpt/scripts/start-services.ps1` (order REST → MCP → Tunnel).

## 9. Node app stack

| Item | Detail |
|------|--------|
| App | `apps/personal_data_chatgpt` — read-only ChatGPT-facing HTTP MCP Apps adapter |
| Server | `server.mjs` (Node http only) |
| Widgets | `public/*-widget.html` (memory graph, relation review, data browser) |
| REST base | `PERSONAL_DATA_REST_URL` default `http://127.0.0.1:8000` |
| Profile | `PERSONAL_DATA_MCP_PROFILE` = `core` \| `full` |
| Privacy guard | Env-tunable seal of credentials/PII in outbound payloads |

## 10. Test tooling

| Tool | Config |
|------|--------|
| pytest | `pytest.ini`: `testpaths = tests`, `pythonpath = src`, cache `var/cache/pytest` |
| Node tests | `node --test` under `apps/personal_data_chatgpt/test/` |

## 11. Key environment variables (stack-facing)

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` / `MEM0_API_KEY` | OpenAI-compatible auth |
| `OPENAI_BASE_URL` | Override LLM endpoint (default MiMo token-plan CN) |
| `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` | Proxy for LLM / tunnel |
| `PERSONAL_DATA_GCLOUD` | gcloud executable path |
| `PERSONAL_DATA_GCP_PROJECT` | Vertex project id |
| `PERSONAL_DATA_VERTEX_LOCATION` | Vertex region |
| `PERSONAL_DATA_VERTEX_MODEL` | Default Gemini model id |
| `PERSONAL_DATA_EMBED_MODEL_PATH` | Local bge model directory |
| `PERSONAL_DATA_EMBED_DEVICE` | `cuda` / `cpu` |
| `PERSONAL_DATA_SEMANTIC_API` | Loopback semantic URL for MCP→REST |
| `PERSONAL_DATA_REST_URL` | Apps server → REST base |
| `PERSONAL_DATA_MCP_PROFILE` | `core` / `full` tool surface |
| `PERSONAL_DATA_FALLBACK_POLICY` | Retrieval hybrid: `layered` / `legacy` |
| `PK_ALLOW_LEGACY_PIPELINE` | Unblock retired `rag-pipeline` |

## 12. What is intentionally out of product stack

- **`rag-pipeline`** integrated steps 1–12 (personal_events + memory batch) — retired; use `pk-sync` + `pk-ku`.
- Memory experiment tables as knowledge SSOT — experimental only.
- Writing AgentsView live DB — forbidden.
- Official `chromadb` Python client — avoided (httpx/compat issues); use REST wrapper.
