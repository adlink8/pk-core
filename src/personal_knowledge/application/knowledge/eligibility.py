"""Phase 41 Plan 01：eligible 证据判定唯一入口（D-05）。

inspect / prepare / inventory 三方共用 ``compute_eligible_messages``，
消除"inspect 数裸 user、prepare 数清洗后 user+assistant"的口径差。

D-05：eligible 与 role 解耦——eligible = session 级 evidence_eligible
+ 内容清洗（剥离系统注入）+ 长度阈值；role 只决定进入哪条轨，
不影响单条消息的 eligible 判定。

隐私安全：本模块只读 canonical DB，返回的 stats 只含 count/hash，不含原文。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB  # noqa: E402
from personal_knowledge.core.canonical_visibility import (  # noqa: E402
    canonical_projection_predicate,
)

# system-reminder 预处理（唯一权威定义；build_knowledge_units /
# build_knowledge_inventory 均从此处 re-export）
SYSTEM_INJECTION_PATTERNS = [
    re.compile(r"<system-reminder[^>]*>.*?</system-reminder>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<recommended_plugins>.*?</recommended_plugins>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<environment_context>.*?</environment_context>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<additional_data>.*?</additional_data>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<user_info>.*?</user_info>", re.DOTALL | re.IGNORECASE),
]

# 压缩摘要识别（AgentView compact 摘要不是用户原话，不可作为个人事实证据）。
# 与 build_agentsview_normalized 的标记共用同一识别（0.2 把命中消息标记
# evidence_scope='system'；此处 content 检测为主、scope 标记兜底）。
COMPACT_SUMMARY_PATTERNS = [
    re.compile(r"This session is being continued from a previous conversation", re.IGNORECASE),
    re.compile(r"was compacted\.\s*The summary below is the authoritative context", re.IGNORECASE),
]


def is_compact_summary(text: str) -> bool:
    """判断文本是否为 LLM compact 摘要特征。"""
    if not text:
        return False
    return any(p.search(text) for p in COMPACT_SUMMARY_PATTERNS)


# assistant 消息工具命令排除模式（13 种前缀 + Task 元数据块）
ASSISTANT_TOOL_PREFIX_PATTERNS = [
    re.compile(r'^\[Bash\]', re.DOTALL),
    re.compile(r'^\[Tool:', re.DOTALL),
    re.compile(r'^\[Thinking\]', re.DOTALL),
    re.compile(r'^\[Read\]', re.DOTALL),
    re.compile(r'^\[Edit\]', re.DOTALL),
    re.compile(r'^\[Write\]', re.DOTALL),
    re.compile(r'^\[Grep\]', re.DOTALL),
    re.compile(r'^\[Glob\]', re.DOTALL),
    re.compile(r'^\[TodoWrite\]', re.DOTALL),
    re.compile(r'^\[Agent\]', re.DOTALL),
    re.compile(r'^\[WebFetch\]', re.DOTALL),
    re.compile(r'^\[WebSearch\]', re.DOTALL),
    re.compile(r'^\[Skill\]', re.DOTALL),
    # [Task]\n{"target":...} 元数据/凭据 blob（pilot 实测：newest-first 队列头部全是此类）
    re.compile(r'^\[Task\]\s*\{', re.DOTALL),
]


def strip_system_injections(text: str) -> str:
    """剥离系统注入标签内容。返回清洗后的文本。"""
    cleaned = text
    for pat in SYSTEM_INJECTION_PATTERNS:
        cleaned = pat.sub("", cleaned)
    return cleaned.strip()


def is_meaningful(text: str) -> bool:
    """判断清洗后的文本是否有实质内容（>30 字非空白）。"""
    return len(text.strip()) > 30


def compute_content_hash(text: str) -> str:
    """空白归一后的 sha256 前 32 位。"""
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def compute_source_checksum(db_path: Path) -> str:
    """canonical DB 的 schema hash + count 校验值。"""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    # schema hash
    ddl = con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE '%fts%' ESCAPE '\\' ORDER BY name"
    ).fetchall()
    schema_text = "\n;;;".join(sql or "" for _name, sql in ddl)
    schema_hash = hashlib.sha256(schema_text.encode("utf-8")).hexdigest()[:16]
    # counts
    session_filter, session_params = canonical_projection_predicate(
        con, "canonical_session_id"
    )
    message_filter, message_params = canonical_projection_predicate(
        con, "canonical_session_id"
    )
    session_count = con.execute(
        f"SELECT COUNT(*) FROM canonical_sessions WHERE {session_filter}",
        session_params,
    ).fetchone()[0]
    message_count = con.execute(
        f"SELECT COUNT(*) FROM canonical_messages WHERE {message_filter}",
        message_params,
    ).fetchone()[0]
    con.close()
    payload = f"{schema_hash}|{session_count}|{message_count}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class EligibleMessage:
    """一条 eligible 证据消息（D-05：role 只决定轨，不决定 eligible）。"""

    evidence_ref: str
    content_hash: str
    role: str
    session_id: str
    source: str
    agent: str
    started_at: str
    has_injection: bool


def compute_eligible_messages(
    canonical_db: Path = AGENT_CONVERSATIONS_DB,
    *,
    roles: tuple[str, ...] = ("user", "assistant"),
    exclude_assistant_tool_prefix: bool = True,
) -> tuple[list[EligibleMessage], dict]:
    """唯一 eligible 判定。返回 (items, stats)。

    过滤链（与原 build_inventory 判定逐项一致）：
      1. SQL 粗筛：evidence_eligible=1 且 content 非空且 length>20 且 role IN roles
      2. 压缩摘要（LLM compact 总结）→ excluded_compact_summary（content 检测；
         canonical_messages 有 evidence_scope 列时，scope='system' 一并兜底排除）
      3. 清洗后 len(cleaned) <= 30 → excluded_short
      4. assistant 命中工具命令前缀（exclude_assistant_tool_prefix=True 时）→ excluded_tool
      5. content_hash 去重 → excluded_dup
      6. 清洗后仅剩注入残骸 → excluded_injection_only

    stats 必含 coarse_count 与五个排除计数；另附 source_checksum /
    dataset_hash / inventory_id（供 prepare 的 after inventory 标识复用）、
    cleaned_len / ref_roles / ref_started_at（ref → 派生值，供 inventory
    分桶与 prepare 元数据复用）。全部只含 count/hash/派生值，不含原文。
    """
    stats: dict = {
        "coarse_count": 0,
        "excluded_compact_summary": 0,
        "excluded_short": 0,
        "excluded_tool": 0,
        "excluded_dup": 0,
        "excluded_injection_only": 0,
        "cleaned_len": {},
        "ref_roles": {},
        "ref_started_at": {},
    }
    if not roles:
        stats["source_checksum"] = ""
        stats["dataset_hash"] = hashlib.sha256(b"").hexdigest()[:32]
        stats["inventory_id"] = ""
        return [], stats

    source_checksum = compute_source_checksum(canonical_db)
    con = sqlite3.connect(f"file:{canonical_db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # canonical_messages 是否有 evidence_scope 列（旧 fixture/旧库可能没有；
    # 有则按 scope='system' 兜底排除压缩摘要，兼容 build_agentsview_normalized
    # 0.2 起的标记）。
    has_scope_col = any(
        r["name"] == "evidence_scope"
        for r in con.execute("PRAGMA table_info(canonical_messages)")
    )
    scope_select = ", m.evidence_scope" if has_scope_col else ""

    placeholders = ",".join("?" * len(roles))
    projection_filter, projection_params = canonical_projection_predicate(
        con, "s.canonical_session_id"
    )
    coarse_rows = con.execute(
        "SELECT m.canonical_message_id, m.canonical_session_id, m.content, "
        "m.source, m.role, s.agent, s.started_at, s.evidence_eligible"
        f"{scope_select} "
        "FROM canonical_messages m JOIN canonical_sessions s "
        "ON m.canonical_session_id=s.canonical_session_id "
        f"WHERE {projection_filter} AND s.evidence_eligible=1 "
        "AND m.content IS NOT NULL AND length(m.content) > 20 "
        f"AND m.role IN ({placeholders}) "
        "ORDER BY s.started_at DESC, m.canonical_message_id",
        (*projection_params, *roles),
    ).fetchall()
    stats["coarse_count"] = len(coarse_rows)

    items: list[EligibleMessage] = []
    seen_hashes: set[str] = set()
    cleaned_len: dict[str, int] = stats["cleaned_len"]
    ref_roles: dict[str, str] = stats["ref_roles"]
    ref_started_at: dict[str, str] = stats["ref_started_at"]

    for row in coarse_rows:
        raw_content = row["content"]
        cleaned = strip_system_injections(raw_content)
        has_injection = cleaned != raw_content.strip()

        # 压缩摘要排除：content 命中（主）+ evidence_scope='system' 标记（兜底）。
        # LLM compact 总结不是用户原话，抽取出来的 quote 无原文支撑。
        if is_compact_summary(cleaned) or (
            has_scope_col and row["evidence_scope"] == "system"
        ):
            stats["excluded_compact_summary"] += 1
            continue

        # assistant 工具命令排除
        if (
            exclude_assistant_tool_prefix
            and row["role"] == "assistant"
            and any(p.match(cleaned) for p in ASSISTANT_TOOL_PREFIX_PATTERNS)
        ):
            stats["excluded_tool"] += 1
            continue

        # 清洗后太短
        if len(cleaned) <= 30:
            stats["excluded_short"] += 1
            continue

        chash = compute_content_hash(cleaned)
        if chash in seen_hashes:
            stats["excluded_dup"] += 1
            continue
        seen_hashes.add(chash)

        # 只有注入内容（清洗后虽然 >30 但全是系统文本）也排除
        if has_injection and len(cleaned.replace("<", "").replace(">", "").strip()) <= 30:
            stats["excluded_injection_only"] += 1
            continue

        ref = row["canonical_message_id"]
        started = row["started_at"] or ""
        cleaned_len[ref] = len(cleaned)
        ref_roles[ref] = row["role"]
        if started:
            ref_started_at[ref] = started
        items.append(
            EligibleMessage(
                evidence_ref=ref,
                content_hash=chash,
                role=row["role"],
                session_id=row["canonical_session_id"],
                source=row["source"],
                agent=row["agent"] or "unknown",
                started_at=started,
                has_injection=has_injection,
            )
        )

    con.close()

    # 有序 dataset hash（Merkle-like：所有 content_hash 的有序拼接）
    ordered_hashes = "|".join(item.content_hash for item in items)
    dataset_hash = hashlib.sha256(ordered_hashes.encode("utf-8")).hexdigest()[:32]
    inventory_id = hashlib.sha256(
        f"{source_checksum}|{dataset_hash}".encode("utf-8")
    ).hexdigest()[:32]
    stats["source_checksum"] = source_checksum
    stats["dataset_hash"] = dataset_hash
    stats["inventory_id"] = inventory_id

    return items, stats
