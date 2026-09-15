import { createHash } from "node:crypto";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { PROTOCOL_VERSION, toolDescriptors } from "../server.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const snapshotPath = path.resolve(here, "../contracts/tool-descriptors.snapshot.json");

function sortValue(value) {
  if (Array.isArray(value)) return value.map(sortValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, sortValue(value[key])]));
  }
  return value;
}

function descriptorCore(tool) {
  return sortValue({
    name: tool.name,
    inputSchema: tool.inputSchema,
    outputSchema: tool.outputSchema,
    annotations: tool.annotations,
    securitySchemes: tool.securitySchemes
  });
}

export function buildSnapshot() {
  const tools = toolDescriptors.map(descriptorCore).sort((a, b) => a.name.localeCompare(b.name));
  const canonical = JSON.stringify(tools);
  return {
    snapshot_version: "chatgpt_tool_descriptors_v1",
    protocol_version: PROTOCOL_VERSION,
    tool_count: tools.length,
    descriptor_sha256: createHash("sha256").update(canonical).digest("hex"),
    tools
  };
}

async function main() {
  const update = process.argv.includes("--update");
  const snapshot = buildSnapshot();
  const rendered = `${JSON.stringify(snapshot, null, 2)}\n`;
  if (update) {
    await mkdir(path.dirname(snapshotPath), { recursive: true });
    await writeFile(snapshotPath, rendered, "utf8");
    process.stdout.write(`updated ${snapshotPath} sha256=${snapshot.descriptor_sha256}\n`);
    return;
  }
  let existing;
  try {
    existing = await readFile(snapshotPath, "utf8");
  } catch {
    process.stderr.write(`snapshot missing: ${snapshotPath}; run with --update\n`);
    process.exitCode = 2;
    return;
  }
  if (existing !== rendered) {
    process.stderr.write(`descriptor snapshot drift: expected sha256=${snapshot.descriptor_sha256}; run with --update after review\n`);
    process.exitCode = 1;
    return;
  }
  process.stdout.write(`descriptor snapshot ok tools=${snapshot.tool_count} sha256=${snapshot.descriptor_sha256}\n`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
