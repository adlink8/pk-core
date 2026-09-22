import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";

const root = path.resolve(import.meta.dirname, "..");

async function readAppFile(relativePath) {
  return readFile(path.join(root, relativePath), "utf8");
}

test("memory graph widget contains bridge listener and graph rendering hooks", async () => {
  const html = await readAppFile("public/memory-graph-widget.html");
  assert.match(html, /ui\/notifications\/tool-result/);
  assert.match(html, /edge_source/);
  assert.match(html, /llm_judgment/);
  assert.match(html, /typeFilter/);
  assert.match(html, /edgeFilter/);
  assert.match(html, /function setData/);
});

test("relation review widget contains bridge listener and read-only review table", async () => {
  const html = await readAppFile("public/relation-review-widget.html");
  assert.match(html, /ui\/notifications\/tool-result/);
  assert.match(html, /Relation Review Queue/);
  assert.match(html, /candidate_id/);
  assert.match(html, /gate_status/);
  assert.match(html, /First version is read-only/);
});

test("data browser widget calls Data tools through the bridge", async () => {
  const html = await readAppFile("public/data-browser-widget.html");
  assert.match(html, /ui\/notifications\/tool-result/);
  assert.match(html, /ui\/notifications\/tool-input/);
  assert.match(html, /tools\/call/);
  assert.match(html, /window\.openai\.callTool/);
  assert.match(html, /data_list_events/);
  assert.match(html, /data_quality_report/);
});

test("widget harness can inject both fixtures", async () => {
  const html = await readAppFile("public/widget-harness.html");
  assert.match(html, /postMessage/);
  assert.match(html, /memory-graph-widget\.html/);
  assert.match(html, /relation-review-widget\.html/);
  assert.match(html, /data-browser-widget\.html/);
  assert.match(html, /ui\/notifications\/tool-result/);
});

test("fixtures include expected graph and review structures", async (t) => {
  // The sanitized public repo ships no widget fixtures; the private ones were
  // removed during open-source cleanup. Skip loudly instead of failing ENOENT.
  const required = ["test/widget-fixtures/graph.json", "test/widget-fixtures/review.json"];
  const missing = [];
  for (const relativePath of required) {
    try {
      await readAppFile(relativePath);
    } catch (error) {
      if (error && error.code === "ENOENT") {
        missing.push(relativePath);
      } else {
        throw error;
      }
    }
  }
  if (missing.length > 0) {
    t.skip(`fixture removed during open-source sanitization: ${missing.join(", ")}`);
    return;
  }

  const graph = JSON.parse(await readAppFile("test/widget-fixtures/graph.json"));
  const review = JSON.parse(await readAppFile("test/widget-fixtures/review.json"));

  assert.equal(graph.ok, true);
  assert.equal(graph.nodes.length, 3);
  assert.equal(graph.edges.some((edge) => edge.edge_source === "rule"), true);
  assert.equal(graph.edges.some((edge) => edge.edge_source === "llm_judgment"), true);

  assert.equal(review.ok, true);
  assert.equal(review.items.length, 2);
  assert.equal(review.items.every((item) => item.gate_status === "review"), true);
});
