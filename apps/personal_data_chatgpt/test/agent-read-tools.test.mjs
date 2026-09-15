import assert from "node:assert/strict";
import { test } from "node:test";
import { callTool, toolDescriptors } from "../server.mjs";

const names = [
  "external_context_list", "external_context_get", "external_context_explain",
  "decision_analysis_list", "decision_analysis_get", "decision_analysis_explain",
  "project_pilot_list", "project_pilot_get", "project_pilot_explain",
  "recommendation_calibration_list", "recommendation_calibration_get",
  "recommendation_calibration_explain"
];

function response(data) {
  return new Response(JSON.stringify({ ok: true, data }), {
    status: 200,
    headers: { "Content-Type": "application/json" }
  });
}

test("agent read descriptors are focused, bounded, and truthfully read-only", () => {
  for (const name of names) {
    const tool = toolDescriptors.find((item) => item.name === name);
    assert.ok(tool, `missing ${name}`);
    assert.equal(tool.annotations.readOnlyHint, true);
    assert.equal(tool.annotations.destructiveHint, false);
    assert.equal(tool.annotations.idempotentHint, true);
    assert.equal(tool.annotations.openWorldHint, false);
    assert.equal(tool._meta.ui, undefined, `${name} must remain tool-only`);
    assert.equal(tool.inputSchema.additionalProperties, false);
    if (tool.inputSchema.properties.limit) {
      assert.equal(tool.inputSchema.properties.limit.maximum, 20);
    }
  }
});

test("list tools forward to the fixed REST path and return compact ids", async () => {
  const calls = [];
  const fetchImpl = async (url) => {
    const parsed = new URL(String(url));
    calls.push(parsed);
    return response({
      items: [{ run_id: "dar_test", request_manifest: "must-not-surface" }],
      count: 1
    });
  };
  const result = await callTool("decision_analysis_list", { limit: 500 }, {
    fetchImpl,
    restBaseUrl: "http://rest.test"
  });
  assert.equal(calls[0].pathname, "/agent/analysis");
  assert.equal(calls[0].searchParams.get("limit"), "20");
  assert.deepEqual(result.structuredContent.ids, ["dar_test"]);
  assert.equal(result.structuredContent.item_count, 1);
  assert.equal(result.structuredContent.next_action, "analysis_get");
  assert.equal(result.structuredContent.data, undefined);
  assert.equal(JSON.stringify(result.structuredContent).includes("must-not-surface"), false);
});

test("explicit explain keeps bounded verified data and honest calibration limits", async () => {
  const fetchImpl = async (url) => {
    const parsed = new URL(String(url));
    assert.equal(parsed.pathname, "/agent/calibration/explain");
    assert.equal(parsed.searchParams.get("protocol_id"), "calp_test");
    return response({
      protocol: [{ protocol_id: "calp_test" }],
      verdicts: [{ verdict_status: "INCONCLUSIVE" }],
      causal_claim: false,
      promotion_available: false
    });
  };
  const result = await callTool("recommendation_calibration_explain", {
    protocol_id: "calp_test"
  }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(result.structuredContent.data.causal_claim, false);
  assert.equal(result.structuredContent.data.promotion_available, false);
  assert.ok(result.structuredContent.limitations.includes("causal_claim=false"));
  assert.ok(JSON.stringify(result.structuredContent).length < 48000);
});

test("REST error codes survive the HTTP MCP adapter", async () => {
  const fetchImpl = async () => new Response(JSON.stringify({
    ok: false,
    error: { code: "invalid_run_id", detail: "run does not exist" }
  }), { status: 400, headers: { "Content-Type": "application/json" } });
  const result = await callTool("decision_analysis_get", { run_id: "missing" }, {
    fetchImpl,
    restBaseUrl: "http://rest.test"
  });
  assert.equal(result.isError, true);
  assert.equal(result.structuredContent.error_code, "invalid_run_id");
  assert.equal(result.structuredContent.error, "run does not exist");
});
