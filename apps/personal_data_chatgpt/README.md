# Personal Data ChatGPT MCP App

HTTP MCP Apps adapter for the local personal decision-intelligence REST API. Reads are checksum-verifying; the bounded orchestration tools require explicit confirmation and remain local, append-only and idempotent.

## Run

Prerequisites:

- Node.js 20 or newer.
- Python REST API running at `http://127.0.0.1:8000`.

```powershell
cd apps\personal_data_chatgpt
npm install
npm start
```

Defaults:

- Apps MCP server: `http://127.0.0.1:8789`
- MCP endpoint: `http://127.0.0.1:8789/mcp`
- Health check: `http://127.0.0.1:8789/health`
- REST backend: `http://127.0.0.1:8000`

Override with environment variables:

```powershell
$env:PORT = "8789"
$env:HOST = "127.0.0.1"
$env:PERSONAL_DATA_REST_URL = "http://127.0.0.1:8000"
npm start
```

## Tools

- `search`: semantic search wrapper over `POST /search/semantic` (knowledge-first + layered fallback; returns `route` / `fallback_policy` when present).
- `fetch`: fetch one event id or memory subject.
- `knowledge_status`: active KU collection / unit_count / `fallback_policy` / SSOT (`GET /knowledge`).
- `list_google_assertions`: privacy-filtered Google light assertions (`GET /google/assertions`; not knowledge units).
- `get_google_assertion`: one Google light assertion by id (`GET /google/assertions/<id>`).
- `show_memory_graph`: returns bounded graph `structuredContent` and the memory graph widget.
- `show_memory_subject`: returns one subject plus neighbors as graph `structuredContent` and the memory graph widget.
- `show_relation_review_queue`: returns relation review queue `structuredContent` and the review widget.
- `show_data_browser`: renders the Data browser widget. The widget calls the `data_*` tools through the MCP Apps bridge.
- `get_system_stats`: returns current `/stats` data.
- `data_list_events` (`Data.list_events`): pages event records with `limit`, `offset`, source/service/category/time filters, and optional field selection.
- `data_list_memories` (`Data.list_memories`): pages long-term memory records by type or subject substring.
- `data_list_relations` (`Data.list_relations`): pages rule and LLM memory relation records with `relation_type`, `subject`, memory id, and `status` filters.
- `data_aggregate` (`Data.aggregate`): counts records grouped by month, source, service, category, memory type, or relation type. Prefer `group_by_fields: ["source", "service"]` for multi-field grouping; `group_by` remains for single-field compatibility.
- `data_timeline` (`Data.timeline`): returns day/month/year counts, optionally filtered by subject.
- `data_export` (`Data.export`): exports a bounded event slice (optional query/keyword; merged former export_all + export_query).
- `data_get_event_by_id` (`Data.get_event_by_id`): fetches one exact event id.
- `data_get_memory_by_id` (`Data.get_memory_by_id`): fetches one exact memory id.
- `data_quality_report` (`Data.data_quality_report`): summarizes duplicate, missing-field, orphan-link, and judgment-status checks.

Read tools set `annotations.readOnlyHint = true`. Guarded orchestration mutations truthfully set `readOnlyHint = false`, `destructiveHint = false`, `idempotentHint = true`, and `openWorldHint = false`.

The Agent-facing decision tools include focused list/get/explain surfaces for External, Analysis, Pilot and Calibration authorities, plus prepare/confirm/preview/execute/resume/explain orchestration tools. Default responses use the compact `agent_compact_envelope_v1`; large evidence requires explicit drill-down.

### Tool surface (KU-first)

| Surface | Tools |
|---------|--------|
| **Model-facing (core)** | `search`, `fetch`, `knowledge_status`, Google assertions, memory show_*, `show_data_browser`, list/get data tools |
| **App/widget-only (core)** | `data_aggregate`, `data_timeline`, `data_export`, `data_quality_report` |

Set `$env:PERSONAL_DATA_MCP_PROFILE = "full"` to expose heavy data tools to the model as well.

Widget-backed tools use the MCP Apps standard `_meta.ui.resourceUri` and the ChatGPT compatibility alias `_meta["openai/outputTemplate"]`.

Data access tools use snake_case MCP names for compatibility. Their titles preserve the `Data.*` names shown above.

Event list/export tools default to compact fields and do not include full `content` or `content_rich` unless requested through `fields`.

## Widgets

- `public/memory-graph-widget.html`: renders nodes and edges from `structuredContent`, distinguishes `rule` and `llm_judgment` edges, and supports type, edge-source, and subject filters.
- `public/relation-review-widget.html`: renders the relation review queue as a read-only table.
- `public/data-browser-widget.html`: renders a compact browser for `/data/*` tools and calls `data_*` tools through `window.openai.callTool` or standard `tools/call` postMessage.
- `public/widget-harness.html`: local fixture harness for testing without ChatGPT.

The widgets listen for MCP Apps bridge messages:

```json
{
  "jsonrpc": "2.0",
  "method": "ui/notifications/tool-result",
  "params": {
    "structuredContent": {}
  }
}
```

## Test

```powershell
cd apps\personal_data_chatgpt
npm install
npm test
```

The tests use mocked REST responses, so they do not require the Python REST server or ChatGPT.
