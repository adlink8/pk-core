import assert from "node:assert/strict";
import { test } from "node:test";
import { callTool, toolDescriptors } from "../server.mjs";

const reads = new Set(["agent_session_prepare", "agent_session_preview", "agent_session_resume", "agent_session_explain"]);
const names = [
  "agent_session_prepare", "agent_session_confirm", "agent_session_preview",
  "agent_session_generate", "agent_session_publish", "agent_session_decide",
  "agent_session_preregister", "agent_session_action_start", "agent_session_action_complete",
  "agent_session_observe", "agent_session_calibrate", "agent_session_resume", "agent_session_explain"
];

function response(data) {
  return new Response(JSON.stringify({ ok: true, data }), {
    status: 200, headers: { "Content-Type": "application/json" }
  });
}

test("guarded orchestration descriptors are strict and truthfully annotated", () => {
  for (const name of names) {
    const tool = toolDescriptors.find((item) => item.name === name);
    assert.ok(tool, `missing ${name}`);
    assert.equal(tool.inputSchema.additionalProperties, false);
    assert.equal(tool.annotations.readOnlyHint, reads.has(name));
    assert.equal(tool.annotations.destructiveHint, false);
    assert.equal(tool.annotations.idempotentHint, true);
    assert.equal(tool.annotations.openWorldHint, false);
    assert.equal(tool._meta.ui, undefined);
  }
});

test("mutations forward complete confirmation tuple to fixed paths", async () => {
  const calls = [];
  const args = {
    preview: { operation: "observe", preview_checksum: "p" },
    confirmed: true,
    idempotency_key: "observe-1",
    now: "2026-07-19T02:00:00Z"
  };
  const fetchImpl = async (url, init) => {
    calls.push({ url: new URL(String(url)), init, body: JSON.parse(init.body) });
    return response({ session_id: "ors_test", state: "observed", sequence: 7, references: { causal_claim: false } });
  };
  const result = await callTool("agent_session_observe", args, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(calls[0].url.pathname, "/agent/session/observe");
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(calls[0].body, args);
  assert.equal(result.structuredContent.state, "observed");
  assert.equal(result.structuredContent.references.causal_claim, false);
  assert.ok(result.structuredContent.limitations.includes("no automated external action"));
});

test("resume uses fixed GET path and typed REST errors survive", async () => {
  const paths = [];
  const fetchImpl = async (url) => {
    const parsed = new URL(String(url));
    paths.push(parsed);
    if (parsed.searchParams.get("session_id") === "missing") {
      return new Response(JSON.stringify({ ok: false, error: { code: "session_missing", detail: "missing" } }), {
        status: 400, headers: { "Content-Type": "application/json" }
      });
    }
    return response({ session_id: "ors_ok", state: "confirmed", sequence: 1 });
  };
  const ok = await callTool("agent_session_resume", { session_id: "ors_ok" }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(paths[0].pathname, "/agent/session/resume");
  assert.equal(ok.structuredContent.state, "confirmed");
  const failed = await callTool("agent_session_resume", { session_id: "missing" }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(failed.isError, true);
  assert.equal(failed.structuredContent.error_code, "session_missing");
});
