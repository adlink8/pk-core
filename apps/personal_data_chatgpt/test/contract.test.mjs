import assert from "node:assert/strict";
import { test } from "node:test";
import { callTool, createAppServer, handleRpc, toolDescriptors } from "../server.mjs";

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

function makeFakeFetch() {
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    const parsed = new URL(String(url));
    calls.push({ url: parsed, options });

    if (parsed.pathname === "/stats") {
      return jsonResponse({ ok: true, data: { events: 12, memory_items: 3, memory_links: 2 } });
    }

    if (parsed.pathname === "/knowledge" || parsed.pathname === "/knowledge/status") {
      return jsonResponse({
        ok: true,
        data: {
          available: true,
          active_collection: "knowledge_units_test",
          unit_count: 30774,
          db_unit_count: 30774,
          fallback_policy: "layered",
          ssot: {
            dialogue: "agentsview_canonical",
            knowledge: "canonical_knowledge_units",
            non_dialogue_raw: "personal_events"
          },
          pointer_path: "var/db/knowledge_index_active.txt",
          allow_legacy_pad: true
        }
      });
    }

    if (parsed.pathname === "/google/assertions") {
      return jsonResponse({
        ok: true,
        data: {
          kind: "google_light_assertion",
          not_knowledge_unit: true,
          event_id_prefix: "g|",
          total: 1,
          limit: Number(parsed.searchParams.get("limit") || 50),
          offset: Number(parsed.searchParams.get("offset") || 0),
          items: [
            {
              assertion_id: "ga_1",
              assertion_type: "interest_topic",
              subject: "Gemini Apps",
              not_knowledge_unit: true
            }
          ]
        }
      });
    }

    if (parsed.pathname === "/google/assertions/ga_1") {
      return jsonResponse({
        ok: true,
        data: {
          assertion_id: "ga_1",
          assertion_type: "interest_topic",
          subject: "Gemini Apps",
          not_knowledge_unit: true,
          evidence: ["g|evt_1"]
        }
      });
    }

    if (parsed.pathname === "/search/semantic") {
      const body = JSON.parse(options.body || "{}");
      return jsonResponse({
        ok: true,
        data: {
          route: "knowledge_first",
          fallback_policy: "layered",
          active_collection: "knowledge_units_test",
          results: [{ event_id: "evt_1", title: `Result for ${body.query}`, layer: "knowledge_unit" }]
        }
      });
    }

    if (parsed.pathname === "/data/events") {
      return jsonResponse({
        ok: true,
        scope: Object.fromEntries(parsed.searchParams.entries()),
        counts: { total: 8136, returned: 1, limit: Number(parsed.searchParams.get("limit") || 100), offset: Number(parsed.searchParams.get("offset") || 0) },
        items: [{ event_id: "evt_1", source: "GPT", event_time: "2026-07-01T00:00:00", title: "Example event" }],
        truncated: true
      });
    }

    if (parsed.pathname === "/data/memories") {
      return jsonResponse({
        ok: true,
        scope: Object.fromEntries(parsed.searchParams.entries()),
        counts: { total: 194, returned: 1 },
        items: [{ memory_id: "mem_1", subject: "Codex", memory_type: "tooling" }],
        truncated: true
      });
    }

    if (parsed.pathname === "/data/relations") {
      return jsonResponse({
        ok: true,
        scope: Object.fromEntries(parsed.searchParams.entries()),
        counts: { total: 29, returned: 1 },
        items: [{ from_memory_id: "mem_1", to_memory_id: "mem_2", relation: "uses_tool", edge_source: "rule" }],
        truncated: false
      });
    }

    if (parsed.pathname === "/data/aggregate") {
      return jsonResponse({
        ok: true,
        scope: Object.fromEntries(parsed.searchParams.entries()),
        counts: { returned: 2 },
        rows: [{ key: "GPT", count: 1796 }, { key: "Agent", count: 4324 }]
      });
    }

    if (parsed.pathname === "/data/timeline") {
      return jsonResponse({
        ok: true,
        scope: Object.fromEntries(parsed.searchParams.entries()),
        counts: { returned: 1 },
        rows: [{ bucket: "2026-07", count: 3 }]
      });
    }

    if (parsed.pathname === "/data/export") {
      return jsonResponse({
        ok: true,
        scope: Object.fromEntries(parsed.searchParams.entries()),
        counts: { total: 8136, returned: 1 },
        format: parsed.searchParams.get("format") || "jsonl",
        text: "{\"event_id\":\"evt_1\"}\n",
        truncated: true
      });
    }

    if (parsed.pathname === "/data/event/evt_1") {
      return jsonResponse({ ok: true, event_id: "evt_1", item: { event_id: "evt_1", title: "Example event" } });
    }

    if (parsed.pathname === "/data/memory/mem_1") {
      return jsonResponse({ ok: true, memory_id: "mem_1", item: { memory_id: "mem_1", subject: "Codex" } });
    }

    if (parsed.pathname === "/data/quality") {
      return jsonResponse({ ok: true, generated_at: "2026-07-03T00:00:00", checks: { duplicate_event_ids: 0 }, warnings: [] });
    }

    if (parsed.pathname === "/memory/graph") {
      return jsonResponse({
        ok: true,
        data: {
          ok: true,
          scope: Object.fromEntries(parsed.searchParams.entries()),
          nodes: [
            { id: "mem_1", subject: "Codex", memory_type: "tooling" },
            { id: "mem_2", subject: "Apps SDK", memory_type: "project" }
          ],
          edges: [
            { source: "mem_1", target: "mem_2", relation: "related_to", edge_source: "llm_judgment", confidence: 0.82, gate_status: "review" }
          ],
          truncated: false
        }
      });
    }

    if (parsed.pathname === "/memory/relation-review") {
      return jsonResponse({
        ok: true,
        data: {
          ok: true,
          count: 1,
          items: [
            { candidate_id: "cand_1", source_subject: "Codex", target_subject: "MCP", relation_type: "uses", confidence: 0.76, gate_status: "review" }
          ],
          truncated: false
        }
      });
    }

    if (parsed.pathname === "/event/evt_1") {
      return jsonResponse({ ok: true, data: { event_id: "evt_1", title: "Example event" } });
    }

    return jsonResponse({ ok: false, error: `Unhandled path ${parsed.pathname}` }, 404);
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}

test("tool descriptors expose the read-only Apps SDK metadata", () => {
  const names = toolDescriptors.map((tool) => tool.name).sort();
  assert.deepEqual(names, [
    "agent_session_action_complete",
    "agent_session_action_start",
    "agent_session_calibrate",
    "agent_session_confirm",
    "agent_session_decide",
    "agent_session_explain",
    "agent_session_generate",
    "agent_session_observe",
    "agent_session_prepare",
    "agent_session_preregister",
    "agent_session_preview",
    "agent_session_publish",
    "agent_session_resume",
    "data_aggregate",
    "data_export",
    "data_get_event_by_id",
    "data_get_memory_by_id",
    "data_list_events",
    "data_list_memories",
    "data_list_relations",
    "data_quality_report",
    "data_timeline",
    "decision_analysis_explain",
    "decision_analysis_get",
    "decision_analysis_list",
    "external_context_explain",
    "external_context_get",
    "external_context_list",
    "fetch",
    "get_google_assertion",
    "get_system_stats",
    "knowledge_status",
    "list_google_assertions",
    "project_pilot_explain",
    "project_pilot_get",
    "project_pilot_list",
    "recommendation_calibration_explain",
    "recommendation_calibration_get",
    "recommendation_calibration_list",
    "search",
    "show_data_browser",
    "show_memory_graph",
    "show_memory_subject",
    "show_relation_review_queue"
  ]);

  const exportTool = toolDescriptors.find((tool) => tool.name === "data_export");
  assert.equal(exportTool._meta.ui.visibility.includes("app"), true);
  // heavy export is app/widget-facing under core profile
  assert.deepEqual(exportTool._meta.ui.visibility, ["app"]);

  const guardedWrites = new Set([
    "agent_session_confirm", "agent_session_generate", "agent_session_publish",
    "agent_session_decide", "agent_session_preregister", "agent_session_action_start",
    "agent_session_action_complete", "agent_session_observe", "agent_session_calibrate"
  ]);
  for (const tool of toolDescriptors) {
    assert.equal(tool.annotations.readOnlyHint, !guardedWrites.has(tool.name), `${tool.name} read-only annotation mismatch`);
    assert.equal(tool.annotations.destructiveHint, false, `${tool.name} should not be destructive`);
    assert.equal(tool.outputSchema.type, "object", `${tool.name} should declare outputSchema`);
  }

  const dataTool = toolDescriptors.find((tool) => tool.name === "data_list_events");
  assert.deepEqual(dataTool._meta.ui.visibility, ["model", "app"]);
  assert.equal(dataTool._meta["openai/widgetAccessible"], true);

  const aggregate = toolDescriptors.find((tool) => tool.name === "data_aggregate");
  assert.equal(aggregate.inputSchema.properties.group_by.enum, undefined);
  assert.equal(aggregate.inputSchema.properties.group_by_fields.type, "array");
  assert.deepEqual(aggregate.inputSchema.properties.group_by_fields.items.enum, ["month", "source", "service", "category", "memory_type", "relation_type"]);

  const timeline = toolDescriptors.find((tool) => tool.name === "data_timeline");
  assert.deepEqual(timeline.inputSchema.properties.bucket.enum, ["day", "month", "year"]);
  assert.equal(timeline.inputSchema.properties.keyword.type, "string");

  const graph = toolDescriptors.find((tool) => tool.name === "show_memory_graph");
  assert.equal(graph._meta.ui.resourceUri, "ui://personal-data/memory-graph-widget.html");
  assert.equal(graph._meta["openai/outputTemplate"], graph._meta.ui.resourceUri);

  const review = toolDescriptors.find((tool) => tool.name === "show_relation_review_queue");
  assert.equal(review._meta.ui.resourceUri, "ui://personal-data/relation-review-widget.html");
  assert.equal(review._meta["openai/outputTemplate"], review._meta.ui.resourceUri);

  const browser = toolDescriptors.find((tool) => tool.name === "show_data_browser");
  assert.equal(browser._meta.ui.resourceUri, "ui://personal-data/data-browser-widget.html");
  assert.equal(browser._meta["openai/outputTemplate"], browser._meta.ui.resourceUri);
});

test("tools call the REST API and return structuredContent", async () => {
  const fetchImpl = makeFakeFetch();

  const stats = await callTool("get_system_stats", {}, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(stats.structuredContent.ok, true);
  assert.equal(stats.structuredContent.stats.memory_items, 3);

  const graph = await callTool("show_memory_graph", { include_llm: true, limit: 5 }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(graph.structuredContent.ok, true);
  assert.equal(graph.structuredContent.nodes.length, 2);
  assert.equal(graph.structuredContent.edges[0].edge_source, "llm_judgment");
  assert.equal(graph._meta.ui.resourceUri, "ui://personal-data/memory-graph-widget.html");

  const review = await callTool("show_relation_review_queue", { limit: 10 }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(review.structuredContent.ok, true);
  assert.equal(review.structuredContent.items[0].candidate_id, "cand_1");
  assert.equal(review._meta.ui.resourceUri, "ui://personal-data/relation-review-widget.html");

  const browser = await callTool("show_data_browser", { view: "events", query: "Codex" }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(browser.structuredContent.ok, true);
  assert.equal(browser.structuredContent.actions.includes("data_list_events"), true);
  assert.equal(browser._meta.ui.resourceUri, "ui://personal-data/data-browser-widget.html");
});

test("JSON-RPC lists and calls tools", async () => {
  const fetchImpl = makeFakeFetch();

  const init = await handleRpc({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} });
  assert.equal(init.result.serverInfo.name, "personal-data-chatgpt-app");

  const listed = await handleRpc({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
  assert.equal(listed.result.tools.length, 44);

  const called = await handleRpc({
    jsonrpc: "2.0",
    id: 3,
    method: "tools/call",
    params: { name: "get_system_stats", arguments: {} }
  }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(called.result.structuredContent.ok, true);
  assert.equal(called.result.structuredContent.stats.events, 12);
});

test("data tools call /data REST endpoints and return structuredContent", async () => {
  const fetchImpl = makeFakeFetch();

  const events = await callTool("data_list_events", { limit: 1, offset: 2, source: "GPT", category_v2: "编程", fields: "event_id,title", order: "asc" }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(events.structuredContent.ok, true);
  assert.equal(events.structuredContent.items[0].event_id, "evt_1");
  assert.equal(events.structuredContent.counts.returned, 1);
  const eventCall = fetchImpl.calls.find((call) => call.url.pathname === "/data/events");
  assert.equal(eventCall.url.searchParams.get("category"), "编程");
  assert.equal(eventCall.url.searchParams.get("order"), "asc");

  const memories = await callTool("data_list_memories", { memory_type: "tooling" }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(memories.structuredContent.counts.total, 194);

  const relations = await callTool("data_list_relations", { relation_type: "uses_tool", subject: "Codex", status: "review" }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(relations.structuredContent.items[0].relation, "uses_tool");
  const relationCall = fetchImpl.calls.find((call) => call.url.pathname === "/data/relations");
  assert.equal(relationCall.url.searchParams.get("subject"), "Codex");
  assert.equal(relationCall.url.searchParams.get("status"), "review");

  const aggregate = await callTool("data_aggregate", { group_by_fields: ["source", "service"], keyword: "Codex", limit: 5 }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(aggregate.structuredContent.rows[0].key, "GPT");
  assert.equal(aggregate.structuredContent.counts.returned, 2);
  const aggregateCall = fetchImpl.calls.find((call) => call.url.pathname === "/data/aggregate");
  assert.equal(aggregateCall.url.searchParams.get("group_by"), "source,service");
  assert.equal(aggregateCall.url.searchParams.get("keyword"), "Codex");
  assert.equal(aggregateCall.url.searchParams.get("limit"), "5");

  const timeline = await callTool("data_timeline", { keyword: "Codex", bucket: "day" }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(timeline.structuredContent.rows[0].bucket, "2026-07");
  const timelineCall = fetchImpl.calls.find((call) => call.url.pathname === "/data/timeline");
  assert.equal(timelineCall.url.searchParams.get("subject"), "Codex");
  assert.equal(timelineCall.url.searchParams.get("bucket"), "day");

  const exported = await callTool("data_export", { query: "Codex", format: "jsonl" }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(exported.structuredContent.format, "jsonl");
  assert.match(exported.structuredContent.text, /evt_1/);
  assert.match(exported.structuredContent.content, /evt_1/);

  const event = await callTool("data_get_event_by_id", { event_id: "evt_1" }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(event.structuredContent.item.title, "Example event");

  const memory = await callTool("data_get_memory_by_id", { memory_id: "mem_1" }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(memory.structuredContent.item.subject, "Codex");

  const quality = await callTool("data_quality_report", {}, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(quality.structuredContent.checks.duplicate_event_ids, 0);
});

test("HTTP server serves health and /mcp", async () => {
  const fetchImpl = makeFakeFetch();
  const server = createAppServer({ fetchImpl, restBaseUrl: "http://rest.test" });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  try {
    const health = await fetch(`http://127.0.0.1:${port}/health`);
    assert.equal(health.status, 200);
    assert.equal((await health.json()).status, "ok");

    const metadata = await fetch(`http://127.0.0.1:${port}/.well-known/oauth-protected-resource/mcp`);
    assert.equal(metadata.status, 200);
    assert.deepEqual((await metadata.json()).authorization_servers, []);

    const response = await fetch(`http://127.0.0.1:${port}/mcp`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list", params: {} })
    });
    const body = await response.json();
    assert.equal(response.status, 200);
    assert.equal(body.result.tools.length, 44);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test("knowledge and google tools call REST endpoints", async () => {
  const fetchImpl = makeFakeFetch();

  const ku = await callTool("knowledge_status", { probe_chroma: true }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(ku.structuredContent.ok, true);
  assert.equal(ku.structuredContent.active_collection, "knowledge_units_test");
  assert.equal(ku.structuredContent.fallback_policy, "layered");
  assert.equal(ku.structuredContent.unit_count, 30774);
  const kuCall = fetchImpl.calls.find((call) => call.url.pathname === "/knowledge");
  assert.ok(kuCall);

  const listed = await callTool("list_google_assertions", { type: "interest_topic", limit: 10 }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(listed.structuredContent.not_knowledge_unit, true);
  assert.equal(listed.structuredContent.items[0].assertion_id, "ga_1");
  const listCall = fetchImpl.calls.find((call) => call.url.pathname === "/google/assertions");
  assert.equal(listCall.url.searchParams.get("type"), "interest_topic");
  assert.equal(listCall.url.searchParams.get("limit"), "10");

  const one = await callTool("get_google_assertion", { assertion_id: "ga_1" }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(one.structuredContent.found, true);
  assert.equal(one.structuredContent.item.subject, "Gemini Apps");
  assert.equal(one.structuredContent.not_knowledge_unit, true);

  const search = await callTool("search", { query: "shell preference", top_k: 3 }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(search.structuredContent.count, 1);
  assert.equal(search.structuredContent.route, "knowledge_first");
  assert.equal(search.structuredContent.fallback_policy, "layered");
  assert.equal(search.structuredContent.results[0].layer, "knowledge_unit");
});
