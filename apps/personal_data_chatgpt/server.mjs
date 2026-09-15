import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const DEFAULT_HOST = process.env.HOST || "127.0.0.1";
const DEFAULT_PORT = Number.parseInt(process.env.PORT || "8789", 10);
const DEFAULT_REST_BASE_URL = process.env.PERSONAL_DATA_REST_URL || "http://127.0.0.1:8000";
const PROTOCOL_VERSION = "2025-06-18";
// core (default): KU-first surface. full: same tools + no extra legacy for GPT app.
// PERSONAL_DATA_MCP_PROFILE=core|full
const MCP_PROFILE = String(process.env.PERSONAL_DATA_MCP_PROFILE || "core").toLowerCase() === "full"
  ? "full"
  : "core";

const PROJECT_CAPABILITY_BUNDLE_PATH = path.resolve(__dirname, "../../governance/manifests/capabilities/generated/project-capability-descriptors.production.json");
const PROJECT_CAPABILITY_BUNDLE = (() => {
  try { return JSON.parse(readFileSync(PROJECT_CAPABILITY_BUNDLE_PATH, "utf8")); } catch { return null; }
})();
const LEGACY_PROJECT_CAPABILITY_ALIASES = Object.freeze({
  knowledge_status: "retrieval.status",
  get_system_stats: "system.health",
  data_quality_report: "data_quality.report",
  show_memory_graph: "retrieval.search",
  fetch: "knowledge.get",
});
const PROJECT_CAPABILITY_ALIASES = Object.freeze(Object.fromEntries([
  ...Object.entries(LEGACY_PROJECT_CAPABILITY_ALIASES),
  ...((PROJECT_CAPABILITY_BUNDLE?.mcp?.operations || []).flatMap((operation) => (operation.aliases || []).map((alias) => [alias.name, operation.name]))),
]));

function projectCapabilityForTool(name) {
  const canonical = PROJECT_CAPABILITY_ALIASES[name] || name;
  const operation = PROJECT_CAPABILITY_BUNDLE?.mcp?.operations?.find((candidate) => candidate.name === canonical);
  return operation ? { ...operation, compatibility_alias: name === canonical ? null : name } : null;
}

const WIDGETS = {
  graph: {
    uri: "ui://personal-data/memory-graph-widget.html",
    file: path.join(__dirname, "public", "memory-graph-widget.html"),
    name: "Memory graph widget",
    description: "Read-only visualization for bounded memory graph results."
  },
  review: {
    uri: "ui://personal-data/relation-review-widget.html",
    file: path.join(__dirname, "public", "relation-review-widget.html"),
    name: "Relation review widget",
    description: "Read-only table for memory relation review queue results."
  },
  data: {
    uri: "ui://personal-data/data-browser-widget.html",
    file: path.join(__dirname, "public", "data-browser-widget.html"),
    name: "Data browser widget",
    description: "Read-only browser for /data event, memory, relation, aggregate, timeline, export, and quality tools."
  }
};

const noAuth = [{ type: "noauth" }];
const readOnlyAnnotations = {
  readOnlyHint: true,
  destructiveHint: false,
  idempotentHint: true,
  openWorldHint: false
};
const guardedWriteAnnotations = {
  readOnlyHint: false,
  destructiveHint: false,
  idempotentHint: true,
  openWorldHint: false
};

const textContent = (text) => [{ type: "text", text }];

// 出站隐私防护（与 Python privacy_guard v2 对齐）：密钥/凭据/PII → [PRIVACY:kind:fp:…]
const PRIVACY_GUARD_ON = !["0", "false", "no", "off"].includes(
  String(process.env.PERSONAL_DATA_PRIVACY_GUARD || "1").trim().toLowerCase()
);
const PRIVACY_SEAL_KEY = String(
  process.env.PERSONAL_DATA_PRIVACY_SEAL_KEY || "personal-data-privacy-guard-dev-salt-v1"
);
const PRIVACY_SCOPE = (() => {
  const raw = String(process.env.PERSONAL_DATA_PRIVACY_SCOPE || "credentials,pii,fields").toLowerCase();
  const parts = new Set(raw.split(",").map((s) => s.trim()).filter(Boolean));
  if (!parts.size || parts.has("all")) return new Set(["credentials", "pii", "fields"]);
  return parts;
})();

function luhnOk(digits) {
  if (!/^\d{13,19}$/.test(digits)) return false;
  let total = 0;
  const rev = digits.split("").reverse();
  for (let i = 0; i < rev.length; i += 1) {
    let n = Number(rev[i]);
    if (i % 2 === 1) {
      n *= 2;
      if (n > 9) n -= 9;
    }
    total += n;
  }
  return total % 10 === 0;
}

const PRIVACY_RULES = [
  // --- API keys / platform tokens ---
  { kind: "openai-key", scope: "credentials", re: /\bsk-proj-[A-Za-z0-9_-]{20,}\b/g },
  { kind: "anthropic-key", scope: "credentials", re: /\bsk-ant-(?:oat01-|api03-)?[A-Za-z0-9_-]{20,}\b/g },
  { kind: "openai-key", scope: "credentials", re: /\bsk-[A-Za-z0-9]{20,}\b/g },
  { kind: "google-api-key", scope: "credentials", re: /\bAIza[0-9A-Za-z_-]{30,}\b/g },
  { kind: "aws-access-key", scope: "credentials", re: /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g },
  { kind: "github-pat", scope: "credentials", re: /\bgithub_pat_[A-Za-z0-9_]{20,}\b/g },
  { kind: "github-token", scope: "credentials", re: /\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b/g },
  { kind: "gitlab-token", scope: "credentials", re: /\bglpat-[A-Za-z0-9_-]{20,}\b/g },
  { kind: "slack-token", scope: "credentials", re: /\bxox[baprs]-[A-Za-z0-9-]{10,}\b/g },
  { kind: "stripe-key", scope: "credentials", re: /\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b/g },
  { kind: "twilio-sid", scope: "credentials", re: /\bAC[0-9a-fA-F]{32}\b/g },
  { kind: "sendgrid-key", scope: "credentials", re: /\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b/g },
  { kind: "huggingface-token", scope: "credentials", re: /\bhf_[A-Za-z0-9]{20,}\b/g },
  { kind: "npm-token", scope: "credentials", re: /\bnpm_[A-Za-z0-9]{20,}\b/g },
  { kind: "pypi-token", scope: "credentials", re: /\bpypi-[A-Za-z0-9_-]{20,}\b/g },
  { kind: "digitalocean-token", scope: "credentials", re: /\bdop_v1_[a-f0-9]{64}\b/g },
  { kind: "shopify-token", scope: "credentials", re: /\bshpat_[a-f0-9]{32}\b/g },
  { kind: "mailgun-key", scope: "credentials", re: /\bkey-[0-9a-f]{32}\b/g },
  { kind: "linear-token", scope: "credentials", re: /\blin_api_[A-Za-z0-9_]{20,}\b/g },
  { kind: "notion-token", scope: "credentials", re: /\bsecret_[A-Za-z0-9]{32,}\b/g },
  { kind: "telegram-bot", scope: "credentials", re: /\b\d{6,14}:[A-Za-z0-9_-]{30,}\b/g },
  { kind: "xai-key", scope: "credentials", re: /\bxai-[A-Za-z0-9_-]{20,}\b/g },
  { kind: "jwt", scope: "credentials", re: /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/g },
  { kind: "bearer", scope: "credentials", re: /\bBearer\s+([A-Za-z0-9._\-+/=]{16,})/gi, group: 1 },
  { kind: "basic-auth", scope: "credentials", re: /\bBasic\s+([A-Za-z0-9+/=]{12,})/gi, group: 1 },

  // --- PEM ---
  {
    kind: "private-key",
    scope: "credentials",
    re: /-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?(?:PRIVATE KEY|PRIVATE KEY BLOCK)-----[\s\S]*?-----END (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?(?:PRIVATE KEY|PRIVATE KEY BLOCK)-----/g
  },

  // --- Connection / URL credentials ---
  {
    kind: "connection-password",
    scope: "credentials",
    re: /\b(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|rediss|amqp|mssql|sqlserver):\/\/[^:\s/]+:([^@\s/]{3,})@/gi,
    group: 1
  },
  {
    kind: "url-password",
    scope: "credentials",
    re: /:\/\/[^:\s/@]+:([^@\s/]{4,})@[A-Za-z0-9.-]+/g,
    group: 1
  },

  // --- Assignments / env / zh labels / cookies ---
  {
    kind: "assignment",
    scope: "credentials",
    re: /\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|app[_-]?secret|app[_-]?key|bot[_-]?token|session[_-]?token|session[_-]?id|password|passwd|pwd|passphrase|private[_-]?key|refresh[_-]?token|id[_-]?token|credential|credentials|auth[_-]?code|webhook[_-]?secret|signing[_-]?secret|encryption[_-]?key|master[_-]?key|aes[_-]?key|cookie|authorization)\s*[:=]\s*["']?([^\s"'\\,]{6,})["']?/gi,
    group: 1
  },
  {
    kind: "env-secret",
    scope: "credentials",
    re: /\b(?:export\s+)?[A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIALS?|WEBHOOK_SECRET|SIGNING_KEY|ENCRYPTION_KEY|MASTER_KEY|CLIENT_SECRET|SESSION_KEY|SESSION_SECRET|AUTH_TOKEN|AUTH_KEY|COOKIE_SECRET)[A-Z0-9_]*\s*=\s*["']?([^\s"']{6,})["']?/g,
    group: 1
  },
  {
    kind: "zh-secret-label",
    scope: "credentials",
    re: /(?:密码|口令|密钥|秘钥|私钥|令牌|访问令牌|刷新令牌|鉴权码|授权码|凭证|密文)\s*[：:=]\s*["']?([^\s"'，,；;]{4,})["']?/g,
    group: 1
  },
  {
    kind: "cookie-pair",
    scope: "credentials",
    re: /\b(?:session|sessionid|sid|auth|token|jwt|access_token|refresh_token|remember_me|connect\.sid)\s*=\s*([^\s;,]{8,})/gi,
    group: 1
  },
  {
    kind: "labeled-hex-secret",
    scope: "credentials",
    re: /(?:secret|token|key|password|passwd|seed|private)\s*[:=]\s*["']?([a-f0-9]{32,})["']?/gi,
    group: 1
  },

  // --- PII ---
  {
    kind: "email",
    scope: "pii",
    re: /(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![A-Za-z0-9._%+-])/g,
    group: 1
  },
  { kind: "phone-cn", scope: "pii", re: /(?<!\d)(1[3-9]\d{9})(?!\d)/g, group: 1 },
  { kind: "phone-intl", scope: "pii", re: /(?<!\d)(\+[1-9]\d{9,14})(?!\d)/g, group: 1 },
  {
    kind: "id-card-cn",
    scope: "pii",
    re: /(?<!\d)([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)/g,
    group: 1
  },
  {
    kind: "bank-card",
    scope: "pii",
    re: /(?<!\d)([3-6]\d{12,18})(?!\d)/g,
    group: 1,
    guard: (secret) => (luhnOk(secret) ? secret : null)
  },
  {
    kind: "zh-pii-label",
    scope: "pii",
    re: /(?:身份证|身份证号|银行卡|银行卡号|手机号|电话|邮箱|护照号|护照)\s*[：:=]\s*["']?([^\s"'，,；;]{4,})["']?/g,
    group: 1
  }
];

const SENSITIVE_KEYS_EXACT = new Set([
  "password", "passwd", "pwd", "passphrase", "secret", "token",
  "api_key", "apikey", "api-key", "access_token", "access-token",
  "refresh_token", "refresh-token", "id_token", "id-token",
  "client_secret", "client-secret", "private_key", "private-key",
  "authorization", "auth", "cookie", "set-cookie", "set_cookie",
  "session", "session_id", "sessionid", "session_token",
  "credential", "credentials", "ssn", "id_card", "idcard",
  "bank_card", "bankcard", "credit_card", "creditcard",
  "email", "phone", "mobile", "telephone",
  "密码", "口令", "密钥", "秘钥", "私钥", "令牌", "凭证",
  "身份证", "身份证号", "银行卡", "手机号", "邮箱"
]);
const SENSITIVE_KEY_SUFFIXES = [
  "_password", "_passwd", "_pwd", "_secret", "_token",
  "_api_key", "_apikey", "_private_key", "_credential",
  "_credentials", "_session", "_cookie", "_auth"
];
const SENSITIVE_KEY_DENY = new Set([
  "token_count", "token_type", "memory_type", "unit_type",
  "event_type", "content_type", "mime_type", "source_type",
  "relation_type", "assertion_type", "type", "status",
  "id", "event_id", "memory_id", "unit_id", "run_id",
  "session_id",
  "query_hash", "content_hash", "checksum", "fingerprint"
]);

function isSensitiveFieldKey(key) {
  if (typeof key !== "string" || !key) return false;
  const low = key.trim().toLowerCase();
  if (SENSITIVE_KEY_DENY.has(low)) return false;
  if (SENSITIVE_KEYS_EXACT.has(key) || SENSITIVE_KEYS_EXACT.has(low)) return true;
  return SENSITIVE_KEY_SUFFIXES.some((sfx) => low.endsWith(sfx));
}

function isIntegrityFieldKey(key) {
  if (typeof key !== "string" || !key) return false;
  const low = key.trim().toLowerCase();
  return SENSITIVE_KEY_DENY.has(low)
    || low === "ids"
    || low.endsWith("_id")
    || low.endsWith("_hash")
    || low.endsWith("_checksum")
    || low.endsWith("_fingerprint");
}

function privacyFingerprint(kind, secret) {
  // 轻量稳定指纹（非密码学 HMAC，防御性二次封存；主防护在 Python REST）
  let h = 2166136261;
  const material = `${PRIVACY_SEAL_KEY}\0${kind}\0${secret}`;
  for (let i = 0; i < material.length; i += 1) {
    h ^= material.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0).toString(16).padStart(8, "0");
}

function guardText(text) {
  if (!PRIVACY_GUARD_ON || typeof text !== "string" || !text) return text;
  const seals = [];
  const placeholder = (kind, secret) => {
    const id = seals.length;
    seals.push(`[PRIVACY:${kind}:fp:${privacyFingerprint(kind, secret)}]`);
    return `\x00SEAL${id}\x00`;
  };
  let out = text;
  for (const rule of PRIVACY_RULES) {
    if (rule.scope && !PRIVACY_SCOPE.has(rule.scope)) continue;
    out = out.replace(rule.re, (...args) => {
      const full = args[0];
      if (full.includes("\x00SEAL")) return full;
      let secret = rule.group ? args[rule.group] : full;
      if (!secret) return full;
      if (typeof rule.guard === "function") {
        secret = rule.guard(secret, text);
        if (secret === null || secret === undefined) return full;
      }
      const ph = placeholder(rule.kind, secret);
      if (rule.group && String(secret) !== full && full.includes(secret)) {
        return full.replace(secret, ph);
      }
      return ph;
    });
  }
  return out.replace(/\x00SEAL(\d+)\x00/g, (_, id) => seals[Number(id)]);
}

function guardJsonable(value, parentKey = null) {
  if (!PRIVACY_GUARD_ON) return value;
  if (typeof value === "string") {
    // Typed identifiers and integrity digests are protocol data, not free text.
    // Scanning them with phone/PII regexes can corrupt signed previews.
    if (parentKey && isIntegrityFieldKey(parentKey)) return value;
    if (
      PRIVACY_SCOPE.has("fields")
      && parentKey
      && isSensitiveFieldKey(parentKey)
      && value
      && !value.startsWith("[PRIVACY:")
      && !["null", "None", "undefined", "true", "false"].includes(value)
    ) {
      return `[PRIVACY:field-secret:fp:${privacyFingerprint("field-secret", value)}]`;
    }
    return guardText(value);
  }
  if (Array.isArray(value)) return value.map((v) => guardJsonable(v, parentKey));
  if (value && typeof value === "object") {
    const next = {};
    for (const [k, v] of Object.entries(value)) next[k] = guardJsonable(v, k);
    return next;
  }
  return value;
}

function applyPrivacyGuardToToolResult(result) {
  if (!result || typeof result !== "object") return result;
  const next = { ...result };
  if (next.structuredContent !== undefined) {
    next.structuredContent = guardJsonable(next.structuredContent);
  }
  if (Array.isArray(next.content)) {
    next.content = next.content.map((part) => {
      if (part && part.type === "text" && typeof part.text === "string") {
        return { ...part, text: guardText(part.text) };
      }
      return part;
    });
  }
  return next;
}

const objectSchema = (properties, required = []) => ({
  type: "object",
  properties,
  required,
  additionalProperties: true
});

const strictObjectSchema = (properties, required = []) => ({
  type: "object",
  properties,
  required,
  additionalProperties: false
});

const graphOutputSchema = objectSchema({
  ok: { type: "boolean" },
  scope: { type: "object" },
  counts: { type: "object" },
  nodes: { type: "array", items: { type: "object" } },
  edges: { type: "array", items: { type: "object" } },
  truncated: { type: "boolean" },
  error: { type: "string" }
}, ["ok"]);

const reviewOutputSchema = objectSchema({
  ok: { type: "boolean" },
  count: { type: "number" },
  items: { type: "array", items: { type: "object" } },
  truncated: { type: "boolean" },
  error: { type: "string" }
}, ["ok"]);

const dataListOutputSchema = objectSchema({
  ok: { type: "boolean" },
  scope: { type: "object" },
  counts: { type: "object" },
  count: { type: "number" },
  total: { type: "number" },
  limit: { type: "number" },
  offset: { type: "number" },
  fields: { type: "array", items: { type: "string" } },
  filters: { type: "object" },
  available: { type: "boolean" },
  items: { type: "array", items: { type: "object" } },
  truncated: { type: "boolean" },
  error: { type: "string" }
}, ["ok"]);

const dataAggregateOutputSchema = objectSchema({
  ok: { type: "boolean" },
  scope: { type: "object" },
  counts: { type: "object" },
  count: { type: "number" },
  limit: { type: "number" },
  group_by: { type: "array", items: { type: "string" } },
  filters: { type: "object" },
  rows: { type: "array", items: { type: "object" } },
  error: { type: "string" }
}, ["ok"]);

const dataExportOutputSchema = objectSchema({
  ok: { type: "boolean" },
  scope: { type: "object" },
  counts: { type: "object" },
  format: { type: "string" },
  text: { type: "string" },
  content: {},
  fields: { type: "array", items: { type: "string" } },
  filters: { type: "object" },
  hard_cap: { type: "number" },
  truncated: { type: "boolean" },
  error: { type: "string" }
}, ["ok"]);

const dataQualityOutputSchema = objectSchema({
  ok: { type: "boolean" },
  generated_at: { type: "string" },
  tables: { type: "object" },
  events: { type: "object" },
  memories: { type: "object" },
  relations: { type: "object" },
  judgments: { type: "object" },
  warnings: { type: "array", items: { type: "string" } },
  error: { type: "string" }
}, ["ok"]);

const agentReadOutputSchema = objectSchema({
  ok: { type: "boolean" },
  authority: { type: "string" },
  operation: { type: "string" },
  summary: { type: "string" },
  item_count: { type: "integer" },
  ids: { type: "array", items: { type: "string" } },
  limitations: { type: "array", items: { type: "string" } },
  next_action: { type: "string" },
  data: { type: "object" },
  truncated: { type: "boolean" },
  error_code: { type: "string" },
  error: { type: "string" }
}, ["ok", "authority", "operation"]);

const agentReadToolSpecs = [
  ["external_context_list", "External context list", "List verified external sources, active snapshot, and bounded public facts.", "/agent/external", "external", "list", strictObjectSchema({ limit: { type: "integer", minimum: 1, maximum: 20, default: 10 } })],
  ["external_context_get", "External context get", "Get one verified source, fact, or snapshot by stable id.", "/agent/external/item", "external", "get", strictObjectSchema({ resource_type: { type: "string", enum: ["source", "fact", "snapshot"] }, resource_id: { type: "string" } }, ["resource_type"])],
  ["external_context_explain", "External context explain", "Explain one external resource's lineage, limits, and safe drill-down.", "/agent/external/explain", "external", "explain", strictObjectSchema({ resource_type: { type: "string", enum: ["source", "fact", "snapshot"] }, resource_id: { type: "string" } }, ["resource_type"])],
  ["decision_analysis_list", "Decision analysis list", "List checksum-verified analysis runs without provider bodies.", "/agent/analysis", "analysis", "list", strictObjectSchema({ limit: { type: "integer", minimum: 1, maximum: 20, default: 10 } })],
  ["decision_analysis_get", "Decision analysis get", "Get one verified analysis candidate, claims, and typed evidence references.", "/agent/analysis/item", "analysis", "get", strictObjectSchema({ run_id: { type: "string" } }, ["run_id"])],
  ["decision_analysis_explain", "Decision analysis explain", "Explain evidence and non-authoritative limits for one analysis run.", "/agent/analysis/explain", "analysis", "explain", strictObjectSchema({ run_id: { type: "string" } }, ["run_id"])],
  ["project_pilot_list", "Project pilot list", "List low-risk project pilot cases; no external actions.", "/agent/pilot", "pilot", "list", strictObjectSchema({ limit: { type: "integer", minimum: 1, maximum: 20, default: 10 } })],
  ["project_pilot_get", "Project pilot get", "Get one pilot case, recommendation, and protocol.", "/agent/pilot/item", "pilot", "get", strictObjectSchema({ case_id: { type: "string" } }, ["case_id"])],
  ["project_pilot_explain", "Project pilot explain", "Explain one pilot history, controls, and observed outcome.", "/agent/pilot/explain", "pilot", "explain", strictObjectSchema({ case_id: { type: "string" }, as_of: { type: "string" } }, ["case_id"])],
  ["recommendation_calibration_list", "Calibration list", "List recommendation calibration protocols.", "/agent/calibration", "calibration", "list", strictObjectSchema({ limit: { type: "integer", minimum: 1, maximum: 20, default: 10 } })],
  ["recommendation_calibration_get", "Calibration get", "Get one calibration protocol, arms, measurements, verdict, and proposals.", "/agent/calibration/item", "calibration", "get", strictObjectSchema({ protocol_id: { type: "string" } }, ["protocol_id"])],
  ["recommendation_calibration_explain", "Calibration explain", "Explain calibration limitations; never claims causality or promotes policy.", "/agent/calibration/explain", "calibration", "explain", strictObjectSchema({ protocol_id: { type: "string" } }, ["protocol_id"])]
];

const agentReadToolDescriptors = agentReadToolSpecs.map(([name, title, description, , , , inputSchema]) => ({
  name,
  title,
  description,
  inputSchema,
  outputSchema: agentReadOutputSchema,
  annotations: readOnlyAnnotations,
  securitySchemes: noAuth,
  _meta: {
    securitySchemes: noAuth,
    "openai/toolInvocation/invoking": `Reading ${title.toLowerCase()}`,
    "openai/toolInvocation/invoked": `${title} ready`
  }
}));

const orchestrationOutputSchema = objectSchema({
  ok: { type: "boolean" },
  operation: { type: "string" },
  state: { type: "string" },
  session_id: { type: "string" },
  sequence: { type: "integer" },
  next_action: { type: "string" },
  references: { type: "object" },
  limitations: { type: "array", items: { type: "string" } },
  data: { type: "object" },
  error_code: { type: "string" },
  error: { type: "string" }
}, ["ok", "operation"]);

const confirmedMutationSchema = strictObjectSchema({
  preview: { type: "object" },
  confirmed: { type: "boolean", const: true },
  idempotency_key: { type: "string" },
  now: { type: "string" }
}, ["preview", "confirmed", "idempotency_key", "now"]);

const orchestrationToolSpecs = [
  { name: "agent_session_prepare", title: "Prepare agent session", path: "/agent/session/prepare", method: "post", readOnly: true, inputSchema: strictObjectSchema({ goal: { type: "string" }, constraints: { type: "array", items: { type: "string" } }, weights: { type: "object" }, actor_identity_hash: { type: "string" }, domain: { type: "string", enum: ["project"] }, risk_budget: { type: "string", enum: ["low"] }, region: { type: "string" }, max_external_age_seconds: { type: "integer" }, now: { type: "string" } }, ["goal", "constraints", "weights", "actor_identity_hash"]) },
  { name: "agent_session_confirm", title: "Confirm agent session", path: "/agent/session/confirm", method: "post", readOnly: false, inputSchema: confirmedMutationSchema },
  { name: "agent_session_preview", title: "Preview agent transition", path: "/agent/session/preview", method: "post", readOnly: true, inputSchema: strictObjectSchema({ session_id: { type: "string" }, transition: { type: "string", enum: ["generate", "publish", "decide", "preregister", "action_start", "action_complete", "observe", "calibrate"] }, payload: { type: "object" }, actor_identity_hash: { type: "string" }, expected_sequence: { type: "integer" }, now: { type: "string" } }, ["session_id", "transition", "payload", "actor_identity_hash", "expected_sequence"]) },
  ...["generate", "publish", "decide", "preregister", "action_start", "action_complete", "observe", "calibrate"].map((operation) => ({ name: `agent_session_${operation}`, title: `${operation} agent session`, path: `/agent/session/${operation.replaceAll("_", "-")}`, method: "post", readOnly: false, inputSchema: confirmedMutationSchema })),
  { name: "agent_session_resume", title: "Resume agent session", path: "/agent/session/resume", method: "get", readOnly: true, inputSchema: strictObjectSchema({ session_id: { type: "string" }, now: { type: "string" } }, ["session_id"]) },
  { name: "agent_session_explain", title: "Explain agent session", path: "/agent/session/explain", method: "get", readOnly: true, inputSchema: strictObjectSchema({ session_id: { type: "string" }, now: { type: "string" } }, ["session_id"]) }
];

const orchestrationToolDescriptors = orchestrationToolSpecs.map((spec) => ({
  name: spec.name,
  title: spec.title,
  description: `${spec.title}; project/low-risk only, no automated external action or promotion.`,
  inputSchema: spec.inputSchema,
  outputSchema: orchestrationOutputSchema,
  annotations: spec.readOnly ? readOnlyAnnotations : guardedWriteAnnotations,
  securitySchemes: noAuth,
  _meta: {
    securitySchemes: noAuth,
    "openai/toolInvocation/invoking": spec.title,
    "openai/toolInvocation/invoked": `${spec.title} complete`
  }
}));

const toolDescriptors = [
  ...agentReadToolDescriptors,
  ...orchestrationToolDescriptors,
  {
    name: "search",
    title: "Search personal data",
    description: "Use this when the user asks to search their local personal event history or memory-adjacent records.",
    inputSchema: objectSchema({
      query: { type: "string", description: "Natural-language search query." },
      top_k: { type: "integer", minimum: 1, maximum: 20, default: 5 },
      source: { type: "string", description: "Optional source filter." }
    }, ["query"]),
    outputSchema: objectSchema({
      ok: { type: "boolean" },
      query: { type: "string" },
      count: { type: "number" },
      results: { type: "array", items: { type: "object" } },
      error: { type: "string" }
    }, ["ok"]),
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      "openai/toolInvocation/invoking": "Searching personal data",
      "openai/toolInvocation/invoked": "Search complete"
    }
  },
  {
    name: "fetch",
    title: "Fetch personal data record",
    description: "Use this when the user asks to open one local event id or memory subject returned by a prior search.",
    inputSchema: objectSchema({
      id: { type: "string", description: "Event id or memory subject." },
      kind: { type: "string", enum: ["auto", "event", "memory"], default: "auto" },
      neighbors: { type: "integer", minimum: 0, maximum: 5, default: 2 }
    }, ["id"]),
    outputSchema: objectSchema({
      ok: { type: "boolean" },
      id: { type: "string" },
      kind: { type: "string" },
      item: { type: "object" },
      error: { type: "string" }
    }, ["ok", "id"]),
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      "openai/toolInvocation/invoking": "Fetching record",
      "openai/toolInvocation/invoked": "Record fetched"
    }
  },
  {
    name: "show_memory_graph",
    title: "Show memory graph",
    description: "Use this when the user asks to inspect a bounded long-term memory graph or compare rule and LLM judgment edges.",
    inputSchema: objectSchema({
      subject: { type: "string", description: "Optional subject focus." },
      hops: { type: "integer", minimum: 0, maximum: 3, default: 1 },
      include_llm: { type: "boolean", default: true },
      limit: { type: "integer", minimum: 1, maximum: 200, default: 80 }
    }),
    outputSchema: graphOutputSchema,
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      ui: { resourceUri: WIDGETS.graph.uri },
      "openai/outputTemplate": WIDGETS.graph.uri,
      "openai/toolInvocation/invoking": "Loading memory graph",
      "openai/toolInvocation/invoked": "Memory graph ready"
    }
  },
  {
    name: "show_memory_subject",
    title: "Show memory subject",
    description: "Use this when the user asks to inspect one memory subject and its local neighbors.",
    inputSchema: objectSchema({
      subject: { type: "string", description: "Memory subject to inspect." },
      neighbors: { type: "integer", minimum: 0, maximum: 5, default: 2 }
    }, ["subject"]),
    outputSchema: graphOutputSchema,
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      ui: { resourceUri: WIDGETS.graph.uri },
      "openai/outputTemplate": WIDGETS.graph.uri,
      "openai/toolInvocation/invoking": "Loading memory subject",
      "openai/toolInvocation/invoked": "Memory subject ready"
    }
  },
  {
    name: "show_relation_review_queue",
    title: "Show relation review queue",
    description: "Use this when the user asks to list memory relation candidates or LLM judgments that need human review.",
    inputSchema: objectSchema({
      limit: { type: "integer", minimum: 1, maximum: 100, default: 50 },
      status: { type: "string", enum: ["review", "accepted", "rejected", "all"], default: "review" }
    }),
    outputSchema: reviewOutputSchema,
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      ui: { resourceUri: WIDGETS.review.uri },
      "openai/outputTemplate": WIDGETS.review.uri,
      "openai/toolInvocation/invoking": "Loading review queue",
      "openai/toolInvocation/invoked": "Review queue ready"
    }
  },
  {
    name: "get_system_stats",
    title: "Get system stats",
    description: "Use this when the user asks for current local personal data system counts or health summary.",
    inputSchema: objectSchema({}),
    outputSchema: objectSchema({
      ok: { type: "boolean" },
      stats: { type: "object" },
      error: { type: "string" }
    }, ["ok"]),
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      "openai/toolInvocation/invoking": "Loading stats",
      "openai/toolInvocation/invoked": "Stats ready"
    }
  },
  {
    name: "knowledge_status",
    title: "Knowledge index status",
    description:
      "Return active knowledge-unit collection status: availability, unit_count, fallback_policy, ssot layers, and pointer path. Aligns with GET /knowledge. Does not promote or rollback indexes.",
    inputSchema: objectSchema({
      probe_chroma: {
        type: "boolean",
        default: true,
        description: "When false, skip live Chroma probe (no_chroma=1)."
      }
    }),
    outputSchema: objectSchema({
      ok: { type: "boolean" },
      available: { type: "boolean" },
      active_collection: { type: "string" },
      unit_count: { type: ["number", "null"] },
      db_unit_count: { type: ["number", "null"] },
      fallback_policy: { type: "string" },
      ssot: { type: "object" },
      pointer_path: { type: "string" },
      allow_legacy_pad: { type: "boolean" },
      knowledge: { type: "object" },
      error: { type: "string" }
    }, ["ok"]),
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      "openai/toolInvocation/invoking": "Checking knowledge index",
      "openai/toolInvocation/invoked": "Knowledge status ready"
    }
  },
  {
    name: "list_google_assertions",
    title: "List Google light assertions",
    description:
      "List privacy-filtered Google light structure assertions (interest topics / services / channels). These are aggregate_ok signals, not dialogue knowledge units. Aligns with GET /google/assertions.",
    inputSchema: objectSchema({
      type: {
        type: "string",
        description: "Optional assertion_type filter (e.g. interest_topic, service, channel)."
      },
      assertion_type: {
        type: "string",
        description: "Alias for type."
      },
      limit: { type: "integer", minimum: 1, maximum: 200, default: 50 },
      offset: { type: "integer", minimum: 0, default: 0 }
    }),
    outputSchema: objectSchema({
      ok: { type: "boolean" },
      kind: { type: "string" },
      not_knowledge_unit: { type: "boolean" },
      total: { type: "number" },
      limit: { type: "number" },
      offset: { type: "number" },
      items: { type: "array", items: { type: "object" } },
      error: { type: "string" }
    }, ["ok"]),
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      "openai/toolInvocation/invoking": "Listing Google assertions",
      "openai/toolInvocation/invoked": "Google assertions ready"
    }
  },
  {
    name: "get_google_assertion",
    title: "Get Google light assertion",
    description:
      "Fetch one Google light assertion by id. Response includes not_knowledge_unit=true and g| evidence refs. Aligns with GET /google/assertions/<id>.",
    inputSchema: objectSchema({
      assertion_id: {
        type: "string",
        description: "Assertion id from list_google_assertions."
      },
      id: {
        type: "string",
        description: "Alias for assertion_id."
      }
    }),
    outputSchema: objectSchema({
      ok: { type: "boolean" },
      found: { type: "boolean" },
      not_knowledge_unit: { type: "boolean" },
      item: { type: "object" },
      error: { type: "string" }
    }, ["ok"]),
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      "openai/toolInvocation/invoking": "Fetching Google assertion",
      "openai/toolInvocation/invoked": "Google assertion ready"
    }
  },
  {
    name: "data_list_events",
    title: "Data.list_events",
    description: "Page through local personal events with bounded filters. Defaults to compact fields without full content.",
    inputSchema: objectSchema({
      limit: { type: "integer", minimum: 1, maximum: 500, default: 100 },
      offset: { type: "integer", minimum: 0, default: 0 },
      source: { type: "string" },
      service: { type: "string" },
      category: { type: "string" },
      category_v2: { type: "string", description: "Alias for category, matching REST /data/events category_v2." },
      start_time: { type: "string" },
      end_time: { type: "string" },
      keyword: { type: "string" },
      fields: { type: "string", description: "Comma-separated event fields. Content fields are returned only when requested." },
      order: { type: "string", enum: ["desc", "asc"], default: "desc" }
    }),
    outputSchema: dataListOutputSchema,
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      "openai/toolInvocation/invoking": "Loading events",
      "openai/toolInvocation/invoked": "Events loaded"
    }
  },
  {
    name: "data_list_memories",
    title: "Data.list_memories",
    description: "Page through long-term memory records with type and subject filters.",
    inputSchema: objectSchema({
      limit: { type: "integer", minimum: 1, maximum: 500, default: 100 },
      offset: { type: "integer", minimum: 0, default: 0 },
      memory_type: { type: "string" },
      subject_like: { type: "string" }
    }),
    outputSchema: dataListOutputSchema,
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      "openai/toolInvocation/invoking": "Loading memories",
      "openai/toolInvocation/invoked": "Memories loaded"
    }
  },
  {
    name: "data_list_relations",
    title: "Data.list_relations",
    description: "Page through rule and LLM long-term memory relations.",
    inputSchema: objectSchema({
      limit: { type: "integer", minimum: 1, maximum: 500, default: 100 },
      offset: { type: "integer", minimum: 0, default: 0 },
      relation_type: { type: "string" },
      subject: { type: "string" },
      from_memory_id: { type: "string" },
      to_memory_id: { type: "string" },
      status: { type: "string", enum: ["review", "accepted", "rejected", "all"], default: "all" }
    }),
    outputSchema: dataListOutputSchema,
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      "openai/toolInvocation/invoking": "Loading relations",
      "openai/toolInvocation/invoked": "Relations loaded"
    }
  },
  {
    name: "data_aggregate",
    title: "Data.aggregate",
    description: "Aggregate local events, memories, or relations by month, source, service, category, memory_type, or relation_type.",
    inputSchema: objectSchema({
      group_by: { type: "string", default: "month", description: "Single group field. Kept for backward compatibility." },
      group_by_fields: {
        type: "array",
        items: { type: "string", enum: ["month", "source", "service", "category", "memory_type", "relation_type"] },
        description: "Preferred multi-field grouping, for example [\"source\", \"service\"]. Overrides group_by when provided."
      },
      metric: { type: "string", enum: ["count"], default: "count" },
      source: { type: "string" },
      service: { type: "string" },
      category: { type: "string" },
      category_v2: { type: "string" },
      keyword: { type: "string" },
      start_time: { type: "string" },
      end_time: { type: "string" },
      limit: { type: "integer", minimum: 1, maximum: 500, default: 100 }
    }),
    outputSchema: dataAggregateOutputSchema,
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      "openai/toolInvocation/invoking": "Aggregating data",
      "openai/toolInvocation/invoked": "Aggregation ready"
    }
  },
  {
    name: "data_timeline",
    title: "Data.timeline",
    description: "Return a day/month/year timeline for a subject or keyword across event titles/content and memory evidence.",
    inputSchema: objectSchema({
      subject: { type: "string" },
      bucket: { type: "string", enum: ["day", "month", "year"], default: "month" },
      keyword: { type: "string", description: "Alias for subject; filters titles/content when subject is not provided." },
      source: { type: "string" },
      service: { type: "string" },
      category: { type: "string" },
      category_v2: { type: "string" },
      start_time: { type: "string" },
      end_time: { type: "string" },
      limit: { type: "integer", minimum: 1, maximum: 500, default: 100 }
    }),
    outputSchema: dataAggregateOutputSchema,
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      "openai/toolInvocation/invoking": "Building timeline",
      "openai/toolInvocation/invoked": "Timeline ready"
    }
  },
  {
    name: "data_export",
    title: "Data.export",
    description:
      "Export a bounded event slice as JSON/JSONL/CSV. Optional query/keyword filter "
      + "(merged data_export_all + data_export_query).",
    inputSchema: objectSchema({
      query: { type: "string", description: "Optional free-text filter." },
      format: { type: "string", enum: ["jsonl", "csv", "json"], default: "jsonl" },
      limit: { type: "integer", minimum: 1, maximum: 5000, default: 500 },
      offset: { type: "integer", minimum: 0, default: 0 },
      source: { type: "string" },
      service: { type: "string" },
      category: { type: "string" },
      category_v2: { type: "string" },
      keyword: { type: "string" },
      start_time: { type: "string" },
      end_time: { type: "string" },
      fields: { type: "string" },
      order: { type: "string", enum: ["desc", "asc"], default: "desc" }
    }),
    outputSchema: dataExportOutputSchema,
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      "openai/toolInvocation/invoking": "Exporting data",
      "openai/toolInvocation/invoked": "Export ready"
    }
  },
  {
    name: "data_get_event_by_id",
    title: "Data.get_event_by_id",
    description: "Fetch one event by exact event_id without auto-detecting memory subjects.",
    inputSchema: objectSchema({
      event_id: { type: "string" },
      fields: { type: "string" }
    }, ["event_id"]),
    outputSchema: objectSchema({
      ok: { type: "boolean" },
      event_id: { type: "string" },
      found: { type: "boolean" },
      fields: { type: "array", items: { type: "string" } },
      item: { type: "object" },
      error: { type: "string" }
    }, ["ok"]),
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      "openai/toolInvocation/invoking": "Fetching event",
      "openai/toolInvocation/invoked": "Event fetched"
    }
  },
  {
    name: "data_get_memory_by_id",
    title: "Data.get_memory_by_id",
    description: "Fetch one long-term memory by exact memory_id without subject matching.",
    inputSchema: objectSchema({
      memory_id: { type: "string" },
      include_evidence: { type: "boolean", default: true }
    }, ["memory_id"]),
    outputSchema: objectSchema({
      ok: { type: "boolean" },
      memory_id: { type: "string" },
      found: { type: "boolean" },
      item: { type: "object" },
      evidence: { type: "array", items: { type: "object" } },
      error: { type: "string" }
    }, ["ok"]),
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      "openai/toolInvocation/invoking": "Fetching memory",
      "openai/toolInvocation/invoked": "Memory fetched"
    }
  },
  {
    name: "data_quality_report",
    title: "Data.data_quality_report",
    description: "Return read-only quality checks for event, memory, relation, and LLM judgment data.",
    inputSchema: objectSchema({}),
    outputSchema: dataQualityOutputSchema,
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      "openai/toolInvocation/invoking": "Checking data quality",
      "openai/toolInvocation/invoked": "Quality report ready"
    }
  },
  {
    name: "show_data_browser",
    title: "Show Data browser",
    description: "Render the read-only Data browser widget. The widget uses the MCP Apps bridge to call the Data.* tools backed by /data/* endpoints.",
    inputSchema: objectSchema({
      view: { type: "string", enum: ["events", "memories", "relations", "aggregate", "timeline", "quality"], default: "events" },
      query: { type: "string", description: "Optional initial keyword or subject." },
      source: { type: "string" },
      service: { type: "string" },
      category: { type: "string" }
    }),
    outputSchema: objectSchema({
      ok: { type: "boolean" },
      view: { type: "string" },
      actions: { type: "array", items: { type: "string" } },
      error: { type: "string" }
    }, ["ok"]),
    annotations: readOnlyAnnotations,
    securitySchemes: noAuth,
    _meta: {
      securitySchemes: noAuth,
      ui: { resourceUri: WIDGETS.data.uri },
      "openai/outputTemplate": WIDGETS.data.uri,
      "openai/toolInvocation/invoking": "Opening Data browser",
      "openai/toolInvocation/invoked": "Data browser ready"
    }
  }
];

const DATA_TOOL_NAMES = new Set([
  "data_list_events",
  "data_list_memories",
  "data_list_relations",
  "data_aggregate",
  "data_timeline",
  "data_export",
  "data_get_event_by_id",
  "data_get_memory_by_id",
  "data_quality_report"
]);

// Model-facing KU-first surface. Heavier data ops stay app/widget-visible only.
const MODEL_TOOL_NAMES = new Set([
  "search",
  "fetch",
  "knowledge_status",
  "list_google_assertions",
  "get_google_assertion",
  "get_system_stats",
  "show_memory_graph",
  "show_memory_subject",
  "show_relation_review_queue",
  "show_data_browser",
  "data_list_events",
  "data_list_memories",
  "data_list_relations",
  "data_get_event_by_id",
  "data_get_memory_by_id"
]);

for (const tool of toolDescriptors) {
  if (DATA_TOOL_NAMES.has(tool.name)) {
    const forModel = MODEL_TOOL_NAMES.has(tool.name) || MCP_PROFILE === "full";
    tool._meta.ui = {
      ...(tool._meta.ui || {}),
      visibility: forModel ? ["model", "app"] : ["app"]
    };
    tool._meta["openai/widgetAccessible"] = true;
  }
}

function listedToolDescriptors() {
  // GPT app always lists all tools for widget bridge; model visibility is via _meta.
  // core vs full currently only changes visibility of heavy data tools.
  return toolDescriptors;
}

function clampInt(value, fallback, min, max) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(Math.max(parsed, min), max);
}

function buildUrl(baseUrl, pathname, params = {}) {
  const url = new URL(pathname, baseUrl.endsWith("/") ? baseUrl : `${baseUrl}/`);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  return url;
}

async function readJsonResponse(response) {
  const text = await response.text();
  let payload;
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`REST returned non-JSON HTTP ${response.status}: ${text.slice(0, 160)}`);
  }
  if (!response.ok || payload?.ok === false) {
    const detail = payload?.error;
    const code = typeof detail === "object" && detail ? detail.code : undefined;
    const message = typeof detail === "object" && detail
      ? detail.message || detail.detail || detail.code
      : detail;
    const error = new Error(message || `REST HTTP ${response.status}`);
    if (code) {
      error.code = code;
      error.category = detail.category;
      error.retryable = detail.retryable;
      error.recoveryActions = detail.recovery_actions;
    }
    throw error;
  }
  if (payload?.schema_version === "agent_compact_envelope_v1") return payload;
  return payload?.data ?? payload;
}

function makeRestClient(baseUrl, fetchImpl = fetch) {
  return {
    async get(pathname, params) {
      const response = await fetchImpl(buildUrl(baseUrl, pathname, params), {
        method: "GET",
        headers: { Accept: "application/json" }
      });
      return readJsonResponse(response);
    },
    async post(pathname, body) {
      const response = await fetchImpl(buildUrl(baseUrl, pathname), {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify(body || {})
      });
      return readJsonResponse(response);
    }
  };
}

function normalizeGraph(data, scope = {}) {
  const nodes = Array.isArray(data?.nodes) ? data.nodes : [];
  const edges = Array.isArray(data?.edges) ? data.edges : [];
  return {
    ok: data?.ok ?? true,
    scope: data?.scope || scope,
    counts: data?.counts || { nodes: nodes.length, edges: edges.length, truncated: Boolean(data?.truncated) },
    nodes,
    edges,
    truncated: Boolean(data?.truncated || data?.counts?.truncated)
  };
}

function normalizeReview(data) {
  const items = Array.isArray(data?.items) ? data.items : [];
  return {
    ok: data?.ok ?? true,
    count: Number.isFinite(data?.count) ? data.count : items.length,
    items,
    truncated: Boolean(data?.truncated)
  };
}

function subjectToGraph(data, subject, neighbors) {
  const root = data?.memory || data;
  const neighborRows = Array.isArray(data?.neighbors) ? data.neighbors : [];
  const rootId = String(root?.id || root?.memory_id || root?.subject || subject);
  const nodes = [
    {
      id: rootId,
      subject: root?.subject || subject,
      memory_type: root?.memory_type || root?.type || "memory",
      memory_subtype: root?.memory_subtype || root?.subtype || "",
      summary: root?.summary || root?.description || root?.content || ""
    },
    ...neighborRows.map((row, index) => ({
      id: String(row.id || row.memory_id || row.subject || `neighbor_${index}`),
      subject: row.subject || row.target_subject || row.source_subject || `neighbor_${index}`,
      memory_type: row.memory_type || row.type || "memory",
      memory_subtype: row.memory_subtype || row.subtype || "",
      summary: row.summary || row.description || row.content || ""
    }))
  ];
  const edges = neighborRows.map((row, index) => ({
    source: rootId,
    target: String(row.id || row.memory_id || row.subject || `neighbor_${index}`),
    relation: row.relation || row.relation_type || "neighbor",
    edge_source: row.edge_source || "rule",
    confidence: row.confidence,
    gate_status: row.gate_status
  }));
  return normalizeGraph({
    scope: { subject, neighbors },
    nodes,
    edges,
    truncated: neighborRows.length >= neighbors
  });
}

function errorResult(error, fallback = {}) {
  return {
    ok: false,
    error_code: error instanceof Error ? error.code : undefined,
    error_category: error instanceof Error ? error.category : undefined,
    retryable: error instanceof Error ? error.retryable : undefined,
    recovery_actions: error instanceof Error ? error.recoveryActions : undefined,
    error: error instanceof Error ? error.message : String(error),
    ...fallback
  };
}

function eventFilterParams(args = {}) {
  return {
    limit: args.limit,
    offset: args.offset,
    source: args.source,
    service: args.service,
    category: args.category || args.category_v2,
    category_v2: args.category_v2,
    start_time: args.start_time,
    end_time: args.end_time,
    keyword: args.keyword,
    fields: args.fields,
    order: args.order
  };
}

function listCount(data) {
  if (Number.isFinite(data?.counts?.returned)) return data.counts.returned;
  if (Number.isFinite(data?.count)) return data.count;
  if (Array.isArray(data?.items)) return data.items.length;
  if (Array.isArray(data?.rows)) return data.rows.length;
  return 0;
}

function normalizeDataList(data) {
  const items = Array.isArray(data?.items) ? data.items : [];
  return {
    ...data,
    counts: data?.counts || {
      total: Number.isFinite(data?.total) ? data.total : items.length,
      returned: Number.isFinite(data?.count) ? data.count : items.length,
      limit: data?.limit,
      offset: data?.offset
    },
    items,
    truncated: Boolean(data?.truncated)
  };
}

function normalizeDataRows(data) {
  const rows = Array.isArray(data?.rows) ? data.rows : Array.isArray(data?.items) ? data.items : [];
  return {
    ...data,
    counts: data?.counts || {
      returned: Number.isFinite(data?.count) ? data.count : rows.length,
      limit: data?.limit
    },
    rows
  };
}

function normalizeExport(data) {
  const text = data?.text ?? data?.content ?? "";
  return {
    ...data,
    text,
    content: data?.content ?? text,
    counts: data?.counts || {
      total: data?.total,
      returned: Number.isFinite(data?.count) ? data.count : undefined,
      limit: data?.limit,
      offset: data?.offset
    },
    truncated: Boolean(data?.truncated)
  };
}

async function callTool(name, args = {}, options = {}) {
  const rest = options.restClient || makeRestClient(options.restBaseUrl || DEFAULT_REST_BASE_URL, options.fetchImpl);

  try {
    const result = await callToolInner(name, args, rest);
    return applyPrivacyGuardToToolResult(result);
  } catch (error) {
    const descriptor = toolDescriptors.find((tool) => tool.name === name);
    const fallback = name === "show_memory_graph" || name === "show_memory_subject"
      ? { scope: {}, counts: { nodes: 0, edges: 0, truncated: false }, nodes: [], edges: [], truncated: false }
      : name === "show_relation_review_queue"
        ? { count: 0, items: [], truncated: false }
        : name === "show_data_browser"
          ? { view: args.view || "events", actions: [] }
        : {};
    return applyPrivacyGuardToToolResult({
      isError: true,
      structuredContent: errorResult(error, fallback),
      content: textContent(`Tool ${name} failed: ${error instanceof Error ? error.message : String(error)}`),
      _meta: descriptor?._meta?.ui?.resourceUri
        ? { ui: { resourceUri: descriptor._meta.ui.resourceUri }, "openai/outputTemplate": descriptor._meta.ui.resourceUri }
        : undefined
    });
  }
}

async function callToolInner(name, args = {}, rest) {
  const orchestrationSpec = orchestrationToolSpecs.find((spec) => spec.name === name);
  if (orchestrationSpec) {
    const data = orchestrationSpec.method === "get"
      ? await rest.get(orchestrationSpec.path, args)
      : await rest.post(orchestrationSpec.path, args);
    if (data?.schema_version === "agent_compact_envelope_v1") {
      return {
        structuredContent: data,
        content: textContent(data.summary)
      };
    }
    const nextAction = data?.next_operation
      ? `agent_session_${data.next_operation}`
      : (name === "agent_session_prepare" ? "agent_session_confirm" : undefined);
    const structuredContent = {
      ok: true,
      operation: name.slice("agent_session_".length),
      state: data?.state,
      session_id: data?.session_id,
      sequence: data?.sequence,
      next_action: nextAction,
      references: data?.references || {},
      limitations: ["project domain and low risk only", "explicit confirmation required", "no automated external action", "no automatic promotion"],
      data
    };
    return {
      structuredContent,
      content: textContent(`${name} completed${data?.state ? ` in state ${data.state}` : ""}.`)
    };
  }

  const agentSpec = agentReadToolSpecs.find(([toolName]) => toolName === name);
  if (agentSpec) {
    const [, , , pathname, authority, operation] = agentSpec;
    const query = { ...args };
    if (query.limit !== undefined) query.limit = clampInt(query.limit, 10, 1, 20);
    const data = await rest.get(pathname, query);
    if (data?.schema_version === "agent_compact_envelope_v1") {
      return {
        structuredContent: data,
        content: textContent(data.summary)
      };
    }
    const ids = [];
    const collect = (value) => {
      if (Array.isArray(value)) return value.forEach(collect);
      if (!value || typeof value !== "object") return;
      for (const key of ["source_id", "snapshot_id", "fact_id", "run_id", "candidate_id", "case_id", "protocol_id"]) {
        if (typeof value[key] === "string" && !ids.includes(value[key])) ids.push(value[key]);
      }
      for (const child of Object.values(value)) collect(child);
    };
    collect(data);
    const encoded = JSON.stringify(data);
    const includeData = operation !== "list" && encoded.length <= 48000;
    const itemCount = Number.isFinite(data?.count)
      ? data.count
      : operation === "list"
        ? ids.length
        : 1;
    const structuredContent = {
      ok: true,
      authority,
      operation,
      summary: `${authority} ${operation} returned ${ids.length} stable id(s).`,
      item_count: itemCount,
      ids: ids.slice(0, 100),
      limitations: authority === "calibration"
        ? ["causal_claim=false", "automatic promotion unavailable"]
        : ["read-only verified metadata", "no external action"],
      next_action: operation === "list" ? `${authority}_get` : `${authority}_explain`,
      data: includeData ? data : undefined,
      truncated: !includeData && operation !== "list"
    };
    return {
      structuredContent,
      content: textContent(structuredContent.summary)
    };
  }

  if (name === "search") {
      const query = String(args.query || "").trim();
      if (!query) throw new Error("query is required");
      const topK = clampInt(args.top_k, 5, 1, 20);
      const data = await rest.post("/search/semantic", { query, top_k: topK, source: args.source });
      // REST returns knowledge-first payload object; older mocks may return a bare array.
      const rows = Array.isArray(data)
        ? data
        : (Array.isArray(data?.results) ? data.results : []);
      return {
        structuredContent: {
          ok: true,
          query,
          count: rows.length,
          results: rows,
          route: data?.route,
          fallback_policy: data?.fallback_policy,
          telemetry: data?.telemetry,
          versions: data?.versions,
          active_collection: data?.active_collection
        },
        content: textContent(
          `Found ${rows.length} result(s) for "${query}"`
          + (data?.route ? ` (route=${data.route})` : "")
          + (data?.fallback_policy ? ` policy=${data.fallback_policy}` : "")
          + "."
        )
      };
    }

    if (name === "fetch") {
      const id = String(args.id || "").trim();
      if (!id) throw new Error("id is required");
      const kind = args.kind || "auto";
      const neighbors = clampInt(args.neighbors, 2, 0, 5);
      let item;
      let resolvedKind = kind;
      if (kind === "event") {
        item = await rest.get(`/event/${encodeURIComponent(id)}`);
      } else if (kind === "memory") {
        item = await rest.get(`/memory/${encodeURIComponent(id)}`, { neighbors });
      } else {
        try {
          item = await rest.get(`/event/${encodeURIComponent(id)}`);
          resolvedKind = "event";
        } catch {
          item = await rest.get(`/memory/${encodeURIComponent(id)}`, { neighbors });
          resolvedKind = "memory";
        }
      }
      return {
        structuredContent: { ok: true, id, kind: resolvedKind, item },
        content: textContent(`Fetched ${resolvedKind} record ${id}.`)
      };
    }

    if (name === "show_memory_graph") {
      const scope = {
        subject: args.subject || undefined,
        hops: clampInt(args.hops, 1, 0, 3),
        include_llm: args.include_llm !== false,
        limit: clampInt(args.limit, 80, 1, 200)
      };
      const data = await rest.get("/memory/graph", scope);
      const structuredContent = normalizeGraph(data, scope);
      return {
        structuredContent,
        content: textContent(`Memory graph has ${structuredContent.nodes.length} node(s) and ${structuredContent.edges.length} edge(s).`),
        _meta: { ui: { resourceUri: WIDGETS.graph.uri }, "openai/outputTemplate": WIDGETS.graph.uri }
      };
    }

    if (name === "show_memory_subject") {
      const subject = String(args.subject || "").trim();
      if (!subject) throw new Error("subject is required");
      const neighbors = clampInt(args.neighbors, 2, 0, 5);
      const data = await rest.get(`/memory/${encodeURIComponent(subject)}`, { neighbors });
      const structuredContent = subjectToGraph(data, subject, neighbors);
      return {
        structuredContent,
        content: textContent(`Loaded memory subject "${subject}" with ${structuredContent.edges.length} neighbor edge(s).`),
        _meta: { ui: { resourceUri: WIDGETS.graph.uri }, "openai/outputTemplate": WIDGETS.graph.uri }
      };
    }

    if (name === "show_relation_review_queue") {
      const params = {
        limit: clampInt(args.limit, 50, 1, 100),
        status: args.status && args.status !== "all" ? args.status : undefined
      };
      const data = await rest.get("/memory/relation-review", params);
      const structuredContent = normalizeReview(data);
      return {
        structuredContent,
        content: textContent(`Relation review queue has ${structuredContent.count} item(s).`),
        _meta: { ui: { resourceUri: WIDGETS.review.uri }, "openai/outputTemplate": WIDGETS.review.uri }
      };
    }

    if (name === "get_system_stats") {
      const stats = await rest.get("/stats");
      return {
        structuredContent: { ok: true, stats },
        content: textContent("Loaded personal data system stats.")
      };
    }

    if (name === "knowledge_status") {
      const probe = args.probe_chroma !== false;
      const knowledge = await rest.get("/knowledge", probe ? {} : { no_chroma: 1 });
      const structuredContent = {
        ok: true,
        available: knowledge?.available,
        active_collection: knowledge?.active_collection,
        unit_count: knowledge?.unit_count ?? knowledge?.db_unit_count ?? null,
        db_unit_count: knowledge?.db_unit_count ?? null,
        fallback_policy: knowledge?.fallback_policy,
        ssot: knowledge?.ssot,
        pointer_path: knowledge?.pointer_path,
        allow_legacy_pad: knowledge?.allow_legacy_pad,
        knowledge
      };
      return {
        structuredContent,
        content: textContent(
          knowledge?.available
            ? `Knowledge index active=${knowledge?.active_collection || "n/a"}`
              + ` units=${structuredContent.unit_count ?? "n/a"}`
              + ` policy=${knowledge?.fallback_policy || "n/a"}.`
            : "Knowledge index not available."
        )
      };
    }

    if (name === "list_google_assertions") {
      const data = await rest.get("/google/assertions", {
        type: args.type || args.assertion_type,
        assertion_type: args.assertion_type || args.type,
        limit: clampInt(args.limit, 50, 1, 200),
        offset: clampInt(args.offset, 0, 0, 100000)
      });
      const items = Array.isArray(data?.items) ? data.items : [];
      const structuredContent = {
        ok: true,
        kind: data?.kind || "google_light_assertion",
        not_knowledge_unit: data?.not_knowledge_unit !== false,
        total: Number.isFinite(data?.total) ? data.total : items.length,
        limit: data?.limit,
        offset: data?.offset,
        items,
        event_id_prefix: data?.event_id_prefix,
        privacy_policy_version: data?.privacy_policy_version
      };
      return {
        structuredContent,
        content: textContent(
          `Loaded ${items.length} Google light assertion(s)`
          + (Number.isFinite(structuredContent.total) ? ` of ${structuredContent.total}` : "")
          + " (not knowledge units)."
        )
      };
    }

    if (name === "get_google_assertion") {
      const assertionId = String(args.assertion_id || args.id || "").trim();
      if (!assertionId) throw new Error("assertion_id is required");
      const item = await rest.get(`/google/assertions/${encodeURIComponent(assertionId)}`);
      return {
        structuredContent: {
          ok: true,
          found: Boolean(item),
          not_knowledge_unit: item?.not_knowledge_unit !== false,
          item: item || null
        },
        content: textContent(
          item
            ? `Fetched Google assertion ${assertionId} (not a knowledge unit).`
            : `Google assertion not found: ${assertionId}.`
        )
      };
    }

    if (name === "data_list_events") {
      const data = normalizeDataList(await rest.get("/data/events", eventFilterParams(args)));
      return {
        structuredContent: data,
        content: textContent(`Loaded ${listCount(data)} event(s).`)
      };
    }

    if (name === "data_list_memories") {
      const data = normalizeDataList(await rest.get("/data/memories", {
        limit: args.limit,
        offset: args.offset,
        memory_type: args.memory_type,
        subject_like: args.subject_like
      }));
      return {
        structuredContent: data,
        content: textContent(`Loaded ${listCount(data)} memory record(s).`)
      };
    }

    if (name === "data_list_relations") {
      const data = normalizeDataList(await rest.get("/data/relations", {
        limit: args.limit,
        offset: args.offset,
        relation_type: args.relation_type,
        subject: args.subject,
        from_memory_id: args.from_memory_id,
        to_memory_id: args.to_memory_id,
        status: args.status && args.status !== "all" ? args.status : undefined
      }));
      return {
        structuredContent: data,
        content: textContent(`Loaded ${listCount(data)} relation record(s).`)
      };
    }

    if (name === "data_aggregate") {
      const groupBy = Array.isArray(args.group_by_fields) && args.group_by_fields.length
        ? args.group_by_fields.join(",")
        : (args.group_by || "month");
      const data = normalizeDataRows(await rest.get("/data/aggregate", {
        group_by: groupBy,
        metric: args.metric || "count",
        source: args.source,
        service: args.service,
        category: args.category || args.category_v2,
        category_v2: args.category_v2,
        keyword: args.keyword,
        start_time: args.start_time,
        end_time: args.end_time,
        limit: args.limit
      }));
      return {
        structuredContent: data,
        content: textContent(`Aggregation returned ${listCount(data)} row(s).`)
      };
    }

    if (name === "data_timeline") {
      const subject = String(args.subject || args.keyword || "").trim();
      const data = normalizeDataRows(await rest.get("/data/timeline", {
        subject: subject || undefined,
        bucket: args.bucket || "month",
        source: args.source,
        service: args.service,
        category: args.category || args.category_v2,
        category_v2: args.category_v2,
        start_time: args.start_time,
        end_time: args.end_time,
        limit: args.limit
      }));
      return {
        structuredContent: data,
        content: textContent(subject
          ? `Timeline for "${subject}" returned ${listCount(data)} bucket(s).`
          : `Timeline returned ${listCount(data)} bucket(s).`)
      };
    }

    if (name === "data_export" || name === "data_export_all" || name === "data_export_query") {
      const query = String(args.query || "").trim();
      const data = normalizeExport(await rest.get("/data/export", {
        ...eventFilterParams({ ...args, keyword: args.keyword || query || undefined }),
        query: query || undefined,
        format: args.format || "jsonl"
      }));
      return {
        structuredContent: data,
        content: textContent(`Exported ${listCount(data)} row(s) as ${data?.format || args.format || "jsonl"}.`)
      };
    }

    if (name === "data_get_event_by_id") {
      const eventId = String(args.event_id || "").trim();
      if (!eventId) throw new Error("event_id is required");
      const data = await rest.get(`/data/event/${encodeURIComponent(eventId)}`, { fields: args.fields });
      return {
        structuredContent: data,
        content: textContent(`Fetched event ${eventId}.`)
      };
    }

    if (name === "data_get_memory_by_id") {
      const memoryId = String(args.memory_id || "").trim();
      if (!memoryId) throw new Error("memory_id is required");
      const data = await rest.get(`/data/memory/${encodeURIComponent(memoryId)}`, {
        include_evidence: args.include_evidence !== false
      });
      return {
        structuredContent: data,
        content: textContent(`Fetched memory ${memoryId}.`)
      };
    }

    if (name === "data_quality_report") {
      const data = await rest.get("/data/quality");
      return {
        structuredContent: data,
        content: textContent("Loaded data quality report.")
      };
    }

    if (name === "show_data_browser") {
      const structuredContent = {
        ok: true,
        view: args.view || "events",
        initialFilters: {
          query: args.query,
          source: args.source,
          service: args.service,
          category: args.category
        },
        actions: [
          "data_list_events",
          "data_list_memories",
          "data_list_relations",
          "data_aggregate",
          "data_timeline",
          "data_export",
          "data_get_event_by_id",
          "data_get_memory_by_id",
          "data_quality_report"
        ]
      };
      return {
        structuredContent,
        content: textContent("Opened read-only Data browser."),
        _meta: { ui: { resourceUri: WIDGETS.data.uri }, "openai/outputTemplate": WIDGETS.data.uri }
      };
    }

  throw new Error(`Unknown tool: ${name}`);
}

function jsonRpcResult(id, result) {
  return { jsonrpc: "2.0", id, result };
}

function jsonRpcError(id, code, message) {
  return { jsonrpc: "2.0", id: id ?? null, error: { code, message } };
}

async function handleRpc(request, options = {}) {
  const { id, method, params } = request || {};
  if (!method) return jsonRpcError(id, -32600, "Invalid JSON-RPC request");

  if (method === "initialize") {
    return jsonRpcResult(id, {
      protocolVersion: PROTOCOL_VERSION,
      capabilities: { tools: {}, resources: {} },
      serverInfo: { name: "personal-data-chatgpt-app", version: "0.3.0" },
      instructions:
        "Read-only local personal data tools (KU-first). Prefer search + knowledge_status for questions; "
        + "use list_google_assertions for Google light signals (not knowledge units). "
        + "Data browser tools cover bulk export/browse. Do not claim write actions."
    });
  }

  if (method === "notifications/initialized") {
    return null;
  }

  if (method === "tools/list") {
    return jsonRpcResult(id, { tools: listedToolDescriptors() });
  }

  if (method === "tools/call") {
    const name = params?.name;
    const args = params?.arguments || {};
    const result = await callTool(name, args, options);
    return jsonRpcResult(id, result);
  }

  if (method === "resources/list") {
    return jsonRpcResult(id, {
      resources: Object.values(WIDGETS).map((widget) => ({
        uri: widget.uri,
        name: widget.name,
        description: widget.description,
        mimeType: "text/html;profile=mcp-app"
      }))
    });
  }

  if (method === "resources/read") {
    const uri = params?.uri;
    const widget = Object.values(WIDGETS).find((candidate) => candidate.uri === uri);
    if (!widget) return jsonRpcError(id, -32602, `Unknown resource: ${uri}`);
    const text = await readFile(widget.file, "utf8");
    return jsonRpcResult(id, {
      contents: [{
        uri: widget.uri,
        mimeType: "text/html;profile=mcp-app",
        text,
        _meta: {
          ui: {
            prefersBorder: true,
            csp: { connect_domains: [], resource_domains: [] }
          },
          "openai/widgetPrefersBorder": true,
          "openai/widgetCSP": { connect_domains: [], resource_domains: [] }
        }
      }]
    });
  }

  return jsonRpcError(id, -32601, `Method not found: ${method}`);
}

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

function sendJson(res, statusCode, body) {
  const payload = JSON.stringify(body);
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(payload),
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, MCP-Protocol-Version, Mcp-Session-Id"
  });
  res.end(payload);
}

function sendText(res, statusCode, text, contentType = "text/plain; charset=utf-8") {
  res.writeHead(statusCode, {
    "Content-Type": contentType,
    "Content-Length": Buffer.byteLength(text),
    "Access-Control-Allow-Origin": "*"
  });
  res.end(text);
}

export function createAppServer(options = {}) {
  const restBaseUrl = options.restBaseUrl || DEFAULT_REST_BASE_URL;
  const fetchImpl = options.fetchImpl;

  return createServer(async (req, res) => {
    try {
      const url = new URL(req.url || "/", `http://${req.headers.host || "127.0.0.1"}`);

      if (req.method === "OPTIONS") {
        res.writeHead(204, {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type, MCP-Protocol-Version, Mcp-Session-Id"
        });
        res.end();
        return;
      }

      if (req.method === "GET" && url.pathname === "/health") {
        sendJson(res, 200, { ok: true, status: "ok", restBaseUrl });
        return;
      }

      if (req.method === "GET" && (
        url.pathname === "/.well-known/oauth-protected-resource/mcp"
        || url.pathname === "/.well-known/oauth-protected-resource"
      )) {
        // The local MCP target is not an OAuth resource server. Authentication is
        // terminated by the OpenAI Secure MCP Tunnel control plane.
        sendJson(res, 200, {
          resource: `${url.origin}/mcp`,
          authorization_servers: [],
          bearer_methods_supported: []
        });
        return;
      }

      if (req.method === "GET" && url.pathname.startsWith("/widgets/")) {
        const filename = path.basename(url.pathname);
        const file = path.join(__dirname, "public", filename);
        if (!file.startsWith(path.join(__dirname, "public")) || !existsSync(file)) {
          sendText(res, 404, "Not found");
          return;
        }
        const html = await readFile(file, "utf8");
        sendText(res, 200, html, filename.endsWith(".html") ? "text/html;profile=mcp-app; charset=utf-8" : "text/plain; charset=utf-8");
        return;
      }

      if (req.method === "POST" && url.pathname === "/mcp") {
        let body;
        try {
          body = JSON.parse(await readBody(req) || "{}");
        } catch {
          sendJson(res, 400, jsonRpcError(null, -32700, "Parse error"));
          return;
        }
        const rpcOptions = { restBaseUrl, fetchImpl, restClient: options.restClient };
        const result = Array.isArray(body)
          ? (await Promise.all(body.map((item) => handleRpc(item, rpcOptions)))).filter(Boolean)
          : await handleRpc(body, rpcOptions);
        if (result === null) {
          res.writeHead(204);
          res.end();
          return;
        }
        sendJson(res, 200, result);
        return;
      }

      if (req.method === "GET" && url.pathname === "/mcp") {
        res.writeHead(200, {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          "Connection": "keep-alive",
          "Access-Control-Allow-Origin": "*",
        });
        res.write("event: endpoint\n");
        res.write(`data: ${DEFAULT_REST_BASE_URL}/mcp\n\n`);
        res.write(": keepalive\n\n");
        const keepalive = setInterval(() => {
          try { res.write(": keepalive\n\n"); } catch { clearInterval(keepalive); }
        }, 15000);
        req.on("close", () => { clearInterval(keepalive); });
        return;
      }

      if (url.pathname === "/mcp") {
        sendJson(res, 405, jsonRpcError(null, -32000, "Use POST /mcp for JSON-RPC requests"));
        return;
      }

      sendText(res, 404, "Not found");
    } catch (error) {
      sendJson(res, 500, jsonRpcError(null, -32603, error instanceof Error ? error.message : String(error)));
    }
  });
}

export { callTool, handleRpc, makeRestClient, PROTOCOL_VERSION, projectCapabilityForTool, PROJECT_CAPABILITY_BUNDLE, toolDescriptors, WIDGETS };

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  const server = createAppServer();
  server.listen(DEFAULT_PORT, DEFAULT_HOST, () => {
    console.log(`[apps] personal-data ChatGPT MCP server listening at http://${DEFAULT_HOST}:${DEFAULT_PORT}`);
    console.log(`[apps] MCP endpoint: http://${DEFAULT_HOST}:${DEFAULT_PORT}/mcp`);
    console.log(`[apps] REST backend: ${DEFAULT_REST_BASE_URL}`);
  });
}
