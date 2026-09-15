import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

import { buildSnapshot } from "../scripts/descriptor-snapshot.mjs";

test("reviewed descriptor snapshot matches the current server contract", async () => {
  const reviewed = JSON.parse(await readFile(
    new URL("../contracts/tool-descriptors.snapshot.json", import.meta.url), "utf8"
  ));
  const current = buildSnapshot();
  assert.deepEqual(current, reviewed);
  assert.equal(current.tool_count, 44);
  assert.match(current.descriptor_sha256, /^[0-9a-f]{64}$/);
  for (const tool of current.tools) {
    assert.equal(tool.inputSchema.type, "object", tool.name);
    assert.equal(tool.outputSchema.type, "object", tool.name);
    assert.equal(tool.annotations.destructiveHint, false, tool.name);
    assert.equal(tool.annotations.openWorldHint, false, tool.name);
  }
});
