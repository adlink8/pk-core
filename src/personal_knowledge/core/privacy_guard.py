"""检索出口隐私防护：检测明文密钥/凭据/PII 并自动封存。

设计目标
--------
- 覆盖 MCP / REST / 任意调用方的 **出站文本与 JSON**
- 默认 **单向封存**（HMAC 指纹），外部客户端无法还原明文
- 可选 ``PERSONAL_DATA_PRIVACY_MODE=encrypt`` + seal key → Fernet 可逆密文
  （仅当本机持有密钥时可解密；默认不给远程 AI 可逆材料）
- 标准库优先；encrypt 模式在缺少 cryptography 时自动降级为 redact
- 范围可配：credentials / pii / fields（默认全开）

环境变量
--------
PERSONAL_DATA_PRIVACY_GUARD
    1/true（默认）启用；0/false 关闭（仅本地排障）
PERSONAL_DATA_PRIVACY_MODE
    redact（默认）| encrypt
PERSONAL_DATA_PRIVACY_SEAL_KEY
    封存盐/Fernet 密钥材料；未设置时使用开发盐（生产务必设置）
PERSONAL_DATA_PRIVACY_SCOPE
    逗号分隔：credentials,pii,fields（默认三者全开）
    例：credentials 仅密钥；credentials,fields 不含邮箱/手机等
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Pattern

POLICY_ID = "privacy-guard-v2"
SEAL_PREFIX = "PRIVACY"
_ALREADY_SEALED = re.compile(r"\[" + SEAL_PREFIX + r":[^\]]+\]")

# 作用域：credentials=密钥/令牌；pii=邮箱手机身份证银行卡；fields=JSON 敏感键整值封存
_DEFAULT_SCOPE = frozenset({"credentials", "pii", "fields"})

# ---------------------------------------------------------------------------
# 规则：(kind, pattern, scope)
# 顺序：长/特殊优先。命名组 value → 只替换密钥本体。
# ---------------------------------------------------------------------------
_RULES: list[tuple[str, Pattern[str], str]] = [
    # ---- PEM / 私钥块 ----
    (
        "private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?"
            r"(?:PRIVATE KEY|PRIVATE KEY BLOCK)-----"
            r"[\s\S]*?"
            r"-----END (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?"
            r"(?:PRIVATE KEY|PRIVATE KEY BLOCK)-----",
            re.MULTILINE,
        ),
        "credentials",
    ),
    # ---- 云/平台 API key 形态（更具体的前缀在前，避免 sk- 抢 sk-ant-） ----
    ("openai-key", re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}\b"), "credentials"),
    ("anthropic-key", re.compile(r"\bsk-ant-(?:oat01-|api03-)?[A-Za-z0-9_-]{20,}\b"), "credentials"),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "credentials"),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "credentials"),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "credentials"),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "credentials"),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b"), "credentials"),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), "credentials"),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "credentials"),
    ("stripe-key", re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"), "credentials"),
    ("twilio-sid", re.compile(r"\bAC[0-9a-fA-F]{32}\b"), "credentials"),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"), "credentials"),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"), "credentials"),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"), "credentials"),
    ("pypi-token", re.compile(r"\bpypi-[A-Za-z0-9_-]{20,}\b"), "credentials"),
    ("digitalocean-token", re.compile(r"\bdop_v1_[a-f0-9]{64}\b"), "credentials"),
    ("shopify-token", re.compile(r"\bshpat_[a-f0-9]{32}\b"), "credentials"),
    ("mailgun-key", re.compile(r"\bkey-[0-9a-f]{32}\b"), "credentials"),
    ("linear-token", re.compile(r"\blin_api_[A-Za-z0-9_]{20,}\b"), "credentials"),
    ("notion-token", re.compile(r"\bsecret_[A-Za-z0-9]{32,}\b"), "credentials"),
    ("telegram-bot", re.compile(r"\b\d{6,14}:[A-Za-z0-9_-]{30,}\b"), "credentials"),
    ("xai-key", re.compile(r"\bxai-[A-Za-z0-9_-]{20,}\b"), "credentials"),
    ("azure-conn-key", re.compile(
        r"(?i)(?:AccountKey|SharedAccessKey|SharedAccessSignature)\s*=\s*"
        r"(?P<value>[A-Za-z0-9+/=]{16,})"
    ), "credentials"),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
        "credentials",
    ),
    (
        "bearer",
        re.compile(r"(?i)\bBearer\s+(?P<value>[A-Za-z0-9._\-+/=]{16,})"),
        "credentials",
    ),
    (
        "basic-auth",
        re.compile(r"(?i)\bBasic\s+(?P<value>[A-Za-z0-9+/=]{12,})"),
        "credentials",
    ),
    # ---- 连接串 / URL 内嵌口令 ----
    (
        "connection-password",
        re.compile(
            r"(?i)(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|rediss|"
            r"mssql|sqlserver)://[^:\s/]+:(?P<value>[^@\s/]{3,})@"
        ),
        "credentials",
    ),
    (
        "url-password",
        re.compile(
            r"(?i)://[^:\s/@]+:(?P<value>[^@\s/]{4,})@[A-Za-z0-9.-]+"
        ),
        "credentials",
    ),
    # ---- 赋值 / 环境变量（英） ----
    (
        "assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|"
            r"auth[_-]?token|client[_-]?secret|app[_-]?secret|app[_-]?key|"
            r"bot[_-]?token|session[_-]?token|session[_-]?id|"
            r"password|passwd|pwd|passphrase|private[_-]?key|"
            r"refresh[_-]?token|id[_-]?token|credential|credentials|"
            r"auth[_-]?code|webhook[_-]?secret|signing[_-]?secret|"
            r"encryption[_-]?key|master[_-]?key|aes[_-]?key|"
            r"cookie|set-cookie|authorization)\s*[:=]\s*"
            r"[\"']?(?P<value>[^\s\"'\\,]{6,})[\"']?"
        ),
        "credentials",
    ),
    (
        "env-secret",
        re.compile(
            r"(?i)\b(?:export\s+)?(?:"
            r"[A-Z][A-Z0-9_]*"
            r"(?:SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|API_KEY|ACCESS_KEY|"
            r"PRIVATE_KEY|CREDENTIALS?|WEBHOOK_SECRET|SIGNING_KEY|"
            r"ENCRYPTION_KEY|MASTER_KEY|CLIENT_SECRET|SESSION_KEY|"
            r"SESSION_SECRET|AUTH_TOKEN|AUTH_KEY|COOKIE_SECRET)"
            r"[A-Z0-9_]*"
            r")\s*=\s*[\"']?(?P<value>[^\s\"']{6,})[\"']?"
        ),
        "credentials",
    ),
    # ---- 中文标签赋值 ----
    (
        "zh-secret-label",
        re.compile(
            r"(?:密码|口令|密钥|秘钥|私钥|令牌|访问令牌|刷新令牌|"
            r"鉴权码|授权码|凭证|密文|验证码(?=\s*[：:=]))\s*[：:=]\s*"
            r"[\"']?(?P<value>[^\s\"'，,；;]{4,})[\"']?"
        ),
        "credentials",
    ),
    # ---- Cookie 片段 ----
    (
        "cookie-pair",
        re.compile(
            r"(?i)\b(?:session|sessionid|sid|auth|token|jwt|access_token|"
            r"refresh_token|remember_me|connect\.sid)\s*=\s*"
            r"(?P<value>[^\s;,]{8,})"
        ),
        "credentials",
    ),
    # ---- 高熵 hex 仅在密钥上下文（避免误伤 event_id / sha256） ----
    (
        "labeled-hex-secret",
        re.compile(
            r"(?i)(?:secret|token|key|password|passwd|seed|private)\s*[:=]\s*"
            r"[\"']?(?P<value>[a-f0-9]{32,})[\"']?"
        ),
        "credentials",
    ),
    (
        "labeled-b64-secret",
        re.compile(
            r"(?i)(?:secret|token|key|password|passwd|credential)\s*[:=]\s*"
            r"[\"']?(?P<value>[A-Za-z0-9+/]{40,}={0,2})[\"']?"
        ),
        "credentials",
    ),
    # ---- PII ----
    (
        "email",
        re.compile(
            r"(?<![A-Za-z0-9._%+-])"
            r"(?P<value>[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
            r"(?![A-Za-z0-9._%+-])"
        ),
        "pii",
    ),
    (
        "phone-cn",
        re.compile(r"(?<!\d)(?P<value>1[3-9]\d{9})(?!\d)"),
        "pii",
    ),
    (
        "phone-intl",
        # 仅国际格式 +国家码，避免误伤普通数字串
        re.compile(r"(?<!\d)(?P<value>\+[1-9]\d{9,14})(?!\d)"),
        "pii",
    ),
    (
        "id-card-cn",
        re.compile(
            r"(?<!\d)(?P<value>[1-9]\d{5}(?:19|20)\d{2}"
            r"(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)"
        ),
        "pii",
    ),
    (
        "bank-card",
        re.compile(r"(?<!\d)(?P<value>[3-6]\d{12,18})(?!\d)"),
        "pii",
    ),
    (
        "zh-pii-label",
        re.compile(
            r"(?:身份证|身份证号|银行卡|银行卡号|手机号|电话|邮箱|"
            r"护照号|护照)\s*[：:=]\s*"
            r"[\"']?(?P<value>[^\s\"'，,；;]{4,})[\"']?"
        ),
        "pii",
    ),
]

# JSON 敏感键：整值封存（fields 作用域）
_SENSITIVE_KEYS_EXACT = frozenset({
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
    "身份证", "身份证号", "银行卡", "手机号", "邮箱",
})
_SENSITIVE_KEY_SUFFIXES = (
    "_password", "_passwd", "_pwd", "_secret", "_token",
    "_api_key", "_apikey", "_private_key", "_credential",
    "_credentials", "_session", "_cookie", "_auth",
)
# 明确不封存的键（避免误伤业务字段）
_SENSITIVE_KEY_DENY = frozenset({
    "token_count", "token_type", "memory_type", "unit_type",
    "event_type", "content_type", "mime_type", "source_type",
    "relation_type", "assertion_type", "type", "status",
    "id", "event_id", "memory_id", "unit_id", "run_id",
    "session_id",
    "query_hash", "content_hash", "checksum", "fingerprint",
})


def _enabled() -> bool:
    raw = os.environ.get("PERSONAL_DATA_PRIVACY_GUARD", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _mode() -> str:
    raw = os.environ.get("PERSONAL_DATA_PRIVACY_MODE", "redact").strip().lower()
    return raw if raw in {"redact", "encrypt"} else "redact"


def _active_scope() -> frozenset[str]:
    raw = os.environ.get("PERSONAL_DATA_PRIVACY_SCOPE", "").strip()
    if not raw:
        return _DEFAULT_SCOPE
    parts = {p.strip().lower() for p in raw.split(",") if p.strip()}
    if not parts or "all" in parts:
        return _DEFAULT_SCOPE
    return frozenset(parts & _DEFAULT_SCOPE) or _DEFAULT_SCOPE


def _seal_key_bytes() -> bytes:
    material = os.environ.get("PERSONAL_DATA_PRIVACY_SEAL_KEY", "").strip()
    if not material:
        material = "personal-data-privacy-guard-dev-salt-v1"
    return material.encode("utf-8")


def _fingerprint(kind: str, secret: str) -> str:
    digest = hmac.new(
        _seal_key_bytes(),
        f"{kind}\0{secret}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:12]


def _try_fernet_encrypt(secret: str) -> str | None:
    try:
        from cryptography.fernet import Fernet  # type: ignore
    except Exception:
        return None
    raw = hashlib.sha256(_seal_key_bytes()).digest()
    key = base64.urlsafe_b64encode(raw)
    token = Fernet(key).encrypt(secret.encode("utf-8"))
    return token.decode("ascii")


def seal_value(kind: str, secret: str, *, mode: str | None = None) -> str:
    """将单段明文封存为不可读占位符。"""
    m = mode or _mode()
    if m == "encrypt":
        enc = _try_fernet_encrypt(secret)
        if enc:
            return f"[{SEAL_PREFIX}:{kind}:enc:{enc}]"
    fp = _fingerprint(kind, secret)
    return f"[{SEAL_PREFIX}:{kind}:fp:{fp}]"


def _luhn_ok(digits: str) -> bool:
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    total = 0
    reverse = digits[::-1]
    for i, ch in enumerate(reverse):
        n = ord(ch) - 48
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _is_sensitive_field_key(key: str) -> bool:
    if not isinstance(key, str) or not key:
        return False
    low = key.strip().lower()
    if low in _SENSITIVE_KEY_DENY:
        return False
    if key in _SENSITIVE_KEYS_EXACT or low in _SENSITIVE_KEYS_EXACT:
        return True
    if any(low.endswith(sfx) for sfx in _SENSITIVE_KEY_SUFFIXES):
        return True
    return False


def _is_integrity_field_key(key: str) -> bool:
    """Return true for typed protocol values that must remain byte-exact."""
    if not isinstance(key, str) or not key:
        return False
    low = key.strip().lower()
    return (
        low in _SENSITIVE_KEY_DENY
        or low == "ids"
        or low.endswith(("_id", "_hash", "_checksum", "_fingerprint"))
    )


def _should_accept_match(kind: str, secret: str) -> bool:
    """额外门禁，压低误报。"""
    if not secret or secret.startswith(f"[{SEAL_PREFIX}:"):
        return False
    # 纯占位 / 过短
    if secret in {"null", "None", "undefined", "true", "false", "N/A", "na", "***"}:
        return False
    if kind == "bank-card":
        # 仅 Luhn 通过的卡号，避免吞掉普通长数字 / event 序号
        return _luhn_ok(secret)
    if kind == "phone-intl":
        digits = re.sub(r"\D", "", secret)
        # 排除纯中国手机（由 phone-cn 处理）、过短、像年份区间
        if len(digits) < 10 or len(digits) > 15:
            return False
        if re.fullmatch(r"1[3-9]\d{9}", digits):
            return False
        # 排除常见日期 2024-01-01 类
        if re.fullmatch(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", secret.strip()):
            return False
        return True
    if kind == "email":
        # 排除示例域可选：保留封存，示例也宁封勿漏
        return "@" in secret and "." in secret.split("@")[-1]
    if kind in {"assignment", "env-secret", "zh-secret-label"} and len(secret) < 6:
        return False
    return True


@dataclass
class PrivacyResult:
    text: str
    hit_count: int = 0
    kinds: list[str] = field(default_factory=list)
    policy_id: str = POLICY_ID
    mode: str = "redact"
    scope: list[str] = field(default_factory=list)

    @property
    def sealed(self) -> bool:
        return self.hit_count > 0


def _sealed_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _ALREADY_SEALED.finditer(text)]


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    for a, b in spans:
        if start < b and end > a:
            return True
    return False


def guard_text(text: str | None, *, mode: str | None = None) -> PrivacyResult:
    """扫描并封存文本中的敏感 span（非重叠，不二次封存）。"""
    if text is None:
        return PrivacyResult(text="")
    if not isinstance(text, str):
        text = str(text)
    m = mode or _mode()
    scope = _active_scope()
    if not text or not _enabled():
        return PrivacyResult(text=text, mode=m, scope=sorted(scope))

    protected = _sealed_spans(text)
    # (match_start, match_end, replace_start, replace_end, kind, secret)
    candidates: list[tuple[int, int, int, int, str, str]] = []

    for kind, pattern, rule_scope in _RULES:
        if rule_scope not in scope:
            continue
        for match in pattern.finditer(text):
            if _overlaps(match.start(), match.end(), protected):
                continue
            if "value" in match.re.groupindex and match.group("value") is not None:
                vs, ve = match.span("value")
                secret = match.group("value")
            else:
                vs, ve = match.start(), match.end()
                secret = match.group(0)
            if not _should_accept_match(kind, secret):
                continue
            candidates.append((match.start(), match.end(), vs, ve, kind, secret))

    candidates.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    chosen: list[tuple[int, int, int, int, str, str]] = []
    occupied: list[tuple[int, int]] = list(protected)
    for c in candidates:
        # 用替换区间做重叠判断，避免 label 冲突吞掉相邻值
        if _overlaps(c[2], c[3], occupied):
            continue
        chosen.append(c)
        occupied.append((c[2], c[3]))

    chosen.sort(key=lambda x: x[2])
    kinds: list[str] = []
    parts: list[str] = []
    cursor = 0
    for _ms, _me, rs, re_, kind, secret in chosen:
        parts.append(text[cursor:rs])
        parts.append(seal_value(kind, secret, mode=m))
        kinds.append(kind)
        cursor = re_
    parts.append(text[cursor:])

    return PrivacyResult(
        text="".join(parts),
        hit_count=len(kinds),
        kinds=kinds,
        mode=m,
        scope=sorted(scope),
    )


def guard_jsonable(obj: Any, *, mode: str | None = None) -> tuple[Any, PrivacyResult]:
    """递归处理 dict/list/str；敏感字段键整值封存 + 字符串内容扫描。"""
    m = mode or _mode()
    scope = _active_scope()
    kinds: list[str] = []

    def _walk(node: Any, parent_key: str | None = None) -> Any:
        if isinstance(node, str):
            # Stable IDs and integrity digests are signed protocol data. Free-text
            # phone/PII regexes can otherwise corrupt a valid preview by chance.
            if parent_key is not None and _is_integrity_field_key(parent_key):
                return node
            # fields 作用域：敏感键下整串封存（非空且非已封存）
            if (
                "fields" in scope
                and parent_key is not None
                and _is_sensitive_field_key(parent_key)
                and node
                and not node.startswith(f"[{SEAL_PREFIX}:")
                and node not in {"null", "None", "undefined", "true", "false"}
            ):
                kinds.append("field-secret")
                return seal_value("field-secret", node, mode=m)
            r = guard_text(node, mode=m)
            if r.hit_count:
                kinds.extend(r.kinds)
            return r.text
        if isinstance(node, dict):
            out: dict[Any, Any] = {}
            for k, v in node.items():
                key_str = str(k) if k is not None else ""
                out[k] = _walk(v, parent_key=key_str)
            return out
        if isinstance(node, list):
            return [_walk(v, parent_key=parent_key) for v in node]
        if isinstance(node, tuple):
            return tuple(_walk(v, parent_key=parent_key) for v in node)
        return node

    if not _enabled():
        return obj, PrivacyResult(text="", mode=m, scope=sorted(scope))

    walked = _walk(obj)
    return walked, PrivacyResult(
        text="",
        hit_count=len(kinds),
        kinds=kinds,
        mode=m,
        scope=sorted(scope),
    )


def guard_mcp_payload(text: str) -> str:
    """MCP tool 出口：封存正文；有命中时追加简短策略脚注。"""
    result = guard_text(text)
    if not result.sealed:
        return result.text
    footnote = (
        f"\n\n[{POLICY_ID}] sealed {result.hit_count} secret span(s); "
        f"kinds={sorted(set(result.kinds))}; mode={result.mode}; "
        f"scope={result.scope}; "
        "plaintext credentials/PII are not returned."
    )
    return result.text + footnote


def iter_rule_names() -> Iterable[str]:
    return sorted({name for name, _, _ in _RULES})


def iter_rules_by_scope() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, _, scope in _RULES:
        out.setdefault(scope, [])
        if name not in out[scope]:
            out[scope].append(name)
    return out
