import assert from "node:assert/strict";
import { test } from "node:test";
import { callTool } from "../server.mjs";

function response(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status, headers: { "Content-Type": "application/json" }
  });
}

test("read and orchestration tools pass through the shared compact envelope", async () => {
  const compact = {
    schema_version: "agent_compact_envelope_v1",
    operation: "analysis.get",
    ok: true,
    status: "success",
    summary: "Get completed; 1 stable reference(s) available.",
    ids: ["dar_test"],
    limitations: ["verified metadata only"],
    next_actions: [{ operation: "analysis.explain", requires: ["stable_id"] }],
    evidence_links: [{ authority: "analysis", record_id: "dar_test", checksum: "a".repeat(64), drill_down: "analysis.get" }],
    data: { run_id: "dar_test" },
    truncated: false,
    budget: { limit_bytes: 16384, used_bytes: 500 }
  };
  const fetchImpl = async () => response(compact);
  const read = await callTool("decision_analysis_get", { run_id: "dar_test" }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.deepEqual(read.structuredContent, compact);
  assert.equal(read.content[0].text, compact.summary);

  const session = { ...compact, operation: "session.resume", ids: ["ors_test"], data: { session_id: "ors_test", state: "generated" } };
  const sessionFetch = async () => response(session);
  const resumed = await callTool("agent_session_resume", { session_id: "ors_test" }, { fetchImpl: sessionFetch, restBaseUrl: "http://rest.test" });
  assert.deepEqual(resumed.structuredContent.ids, ["ors_test"]);
  assert.equal(resumed.structuredContent.data.state, "generated");
  assert.equal(resumed.structuredContent.data.session_id, "ors_test");
});

test("privacy guard preserves typed IDs and integrity hashes byte-for-byte", async () => {
  const phoneShapedDigest = `a13800138000${"b".repeat(52)}`;
  const compact = {
    schema_version: "agent_compact_envelope_v1", operation: "session.prepare",
    ok: true, status: "success", summary: "Prepared.", ids: [`ors_${phoneShapedDigest}`],
    limitations: [], next_actions: [], evidence_links: [], truncated: false,
    data: {
      session_id: `ors_${phoneShapedDigest}`,
      actor_identity_hash: phoneShapedDigest,
      preview_checksum: phoneShapedDigest,
      payload: { binding_hash: phoneShapedDigest, goal: "local validation only" }
    },
    budget: { limit_bytes: 16384, used_bytes: 500 }
  };
  const result = await callTool("agent_session_prepare", {}, {
    fetchImpl: async () => response(compact), restBaseUrl: "http://rest.test"
  });
  assert.deepEqual(result.structuredContent, compact);
});

test("typed compact REST failures retain retryability and recovery actions", async () => {
  const payload = {
    schema_version: "agent_compact_envelope_v1", operation: "session.execute",
    ok: false, status: "error", summary: "Provider outcome is unknown; automatic retry is unsafe.",
    error: {
      code: "provider_outcome_unknown", category: "unknown_outcome",
      message: "Provider outcome is unknown; automatic retry is unsafe.",
      retryable: false,
      recovery_actions: ["resume_session", "inspect_provider_reservation", "manual_review"]
    }
  };
  const fetchImpl = async () => response(payload, 400);
  const result = await callTool("agent_session_generate", {
    preview: { operation: "generate" }, confirmed: true,
    idempotency_key: "unknown", now: "2026-07-19T03:00:00Z"
  }, { fetchImpl, restBaseUrl: "http://rest.test" });
  assert.equal(result.isError, true);
  assert.equal(result.structuredContent.error_code, "provider_outcome_unknown");
  assert.equal(result.structuredContent.error_category, "unknown_outcome");
  assert.equal(result.structuredContent.retryable, false);
  assert.deepEqual(result.structuredContent.recovery_actions, payload.error.recovery_actions);
});

test("compact tool output stays below the declared model budget", async () => {
  const compact = {
    schema_version: "agent_compact_envelope_v1", operation: "pilot.list", ok: true,
    status: "success", summary: "List completed.", ids: [], limitations: [],
    next_actions: [{ operation: "pilot.get" }], evidence_links: [], data: null,
    truncated: true, budget: { limit_bytes: 16384, used_bytes: 350 }
  };
  const result = await callTool("project_pilot_list", {}, {
    fetchImpl: async () => response(compact), restBaseUrl: "http://rest.test"
  });
  assert.ok(Buffer.byteLength(JSON.stringify(result.structuredContent), "utf8") <= 16384);
  assert.equal(result.structuredContent.truncated, true);
});
