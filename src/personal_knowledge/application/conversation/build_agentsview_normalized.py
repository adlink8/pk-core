"""Phase 13.5 Wave 2：隐私安全的 normalized snapshot 生成。

把 AgentView 只读快照转换为 ``agentsview_normalized.sqlite``（staging + 原子发布）。
本模块只做转换和脱敏，不触碰 AgentView 源库。

字段白名单（CONTEXT.md D-04，永不复制）：
  - thinking_text
  - 用户邮箱 / 用户 ID
  - tool input_json / result_content 全文
  - insight prompt/content
  - secret match 正文

脱敏规则：
  1. 源库 ``secret_leak_count > 0``、命中 ``excluded_sessions``、或已删除
     （``deleted_at`` 非空）的 session → session 行保留
     ``evidence_eligible=0`` 和原因计数，**messages 正文完全不写**。
     另：``excluded_sessions`` 非空却 0 行能 JOIN 上 ``sessions.id`` 时
     fail-closed（抛 RevisionGateError，不发布）。
  2. 对允许写入的 message content 再跑本地敏感信息正则；二次命中时整条
     message 进隔离统计且不落正文（报告只留规则名+计数，不留 match）。
  3. system/sidechain/subagent 消息保留关系元数据，标记 evidence_scope，
     默认不进入个人事实层。
  4. 压缩摘要（LLM compact 总结，命中 ``is_compact_summary``）复用
     evidence_scope 标记为 ``system`` 轨：即使源库 role=user，也不再以
     user 身份进入抽取轨（eligibility 按 scope 过滤）。

对外暴露：
  - :data:`NORMALIZED_SCHEMA` / :data:`SECRET_RULES`
  - :func:`build_normalized` — 快照 → staging → 原子发布
  - :func:`local_secret_scan` — 本地二次扫描
  - :class:`NormalizationStats` — Revision gate 指标
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import AGENTSVIEW_DB, AGENTSVIEW_NORMALIZED_DB  # noqa: E402
from personal_knowledge.application.knowledge.eligibility import (  # noqa: E402
    is_compact_summary,
)

# === 本地二次敏感信息扫描规则（正则，只记规则名，不留 match） ===
# 与 AgentView secret_findings 的规则互补，覆盖常见 PII/credential 模式。
SECRET_RULES: dict[str, re.Pattern[str]] = {
    # OpenAI: sk-... / sk-proj-...
    "local-openai-key": re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    # Google API key: AIza...
    "local-google-api-key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    # 通用 Bearer token
    "local-bearer-token": re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}"),
    # GitHub PAT
    "local-github-pat": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    # AWS access key
    "local-aws-key": re.compile(r"AKIA[0-9A-Z]{16}"),
    # 私钥头
    "local-private-key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # 邮箱（PII，非 secret 但默认不落 normalized content）
    "local-email-pii": re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
}


def local_secret_scan(text: str) -> list[str]:
    """对 text 跑本地敏感信息规则，返回命中的规则名列表（去重）。"""
    if not text:
        return []
    hits: list[str] = []
    for name, pat in SECRET_RULES.items():
        if pat.search(text):
            hits.append(name)
    return hits


# === normalized schema ===
# 表设计：只保留下游需要的会话、消息、工具元数据、用量、lineage。
NORMALIZED_SCHEMA: dict[str, list[tuple[str, str]]] = {
    "import_runs": [
        ("run_id", "TEXT PRIMARY KEY"),
        ("generated_at", "TEXT NOT NULL"),
        ("source_user_version", "INTEGER"),
        ("source_integrity", "TEXT"),
        ("schema_hash", "TEXT"),
        ("config_hash", "TEXT"),
        ("code_version", "TEXT"),
        ("dataset_hash", "TEXT"),  # 全量内容 hash（幂等校验）
        ("status", "TEXT NOT NULL CHECK(status IN ('ok','blocked'))"),
        ("stats_json", "TEXT"),
    ],
    "sessions": [
        # 版本前缀稳定 ID（v1|sha256(source|session_id)）
        ("session_id", "TEXT PRIMARY KEY"),
        ("source_session_id", "TEXT NOT NULL"),  # AgentView sessions.id
        ("agent", "TEXT"),
        ("started_at", "TEXT"),
        ("ended_at", "TEXT"),
        ("message_count", "INTEGER"),
        ("user_message_count", "INTEGER"),
        ("parent_session_id", "TEXT"),
        ("relationship_type", "TEXT"),
        ("source_session_ref", "TEXT"),  # AgentView source_session_id（外部 lineage）
        ("file_hash", "TEXT"),
        ("cwd", "TEXT"),
        ("git_branch", "TEXT"),
        ("model", "TEXT"),  # 代表性 model（首条 usage 或 message）
        # 隐私/资格
        ("secret_leak_count", "INTEGER NOT NULL DEFAULT 0"),
        ("evidence_eligible", "INTEGER NOT NULL DEFAULT 1"),
        ("excluded", "INTEGER NOT NULL DEFAULT 0"),
        ("deleted_at", "TEXT"),
        ("evidence_scope", "TEXT NOT NULL DEFAULT 'user'"),
        ("ineligible_reasons_json", "TEXT"),  # 原因计数，无正文
    ],
    "messages": [
        ("message_id", "TEXT PRIMARY KEY"),  # v1|sha256(session_id|ordinal)
        ("session_id", "TEXT NOT NULL REFERENCES sessions(session_id)"),
        ("source_message_id", "INTEGER"),  # AgentView messages.id（可回查）
        ("ordinal", "INTEGER NOT NULL"),
        ("role", "TEXT NOT NULL CHECK(role IN ('user','assistant','developer','system','tool'))"),
        ("content", "TEXT"),  # 仅 eligible session 且二次扫描通过才有正文
        ("content_length", "INTEGER"),
        ("timestamp", "TEXT"),
        ("model", "TEXT"),
        ("is_system", "INTEGER NOT NULL DEFAULT 0"),
        ("is_sidechain", "INTEGER NOT NULL DEFAULT 0"),
        ("content_hash", "TEXT"),  # sha256(norm(content))，用于去重/比对
        ("evidence_scope", "TEXT NOT NULL DEFAULT 'user'"),
        # 二次 secret 扫描命中：正文不写，只记规则名计数
        ("quarantined_local_rules", "TEXT"),  # 逗号分隔命中规则名
    ],
    "tool_events": [
        ("tool_event_id", "TEXT PRIMARY KEY"),
        ("session_id", "TEXT NOT NULL REFERENCES sessions(session_id)"),
        ("source_kind", "TEXT NOT NULL CHECK(source_kind IN ('call','result'))"),
        ("source_row_id", "INTEGER"),
        ("tool_name", "TEXT"),
        ("category", "TEXT"),
        ("status", "TEXT"),
        ("skill_name", "TEXT"),
        ("call_index", "INTEGER"),
        ("subagent_session_id", "TEXT"),
        ("content_length", "INTEGER"),  # 只记长度，不记正文
        ("timestamp", "TEXT"),
        ("event_index", "INTEGER"),
    ],
    "usage_events": [
        ("usage_id", "TEXT PRIMARY KEY"),
        ("session_id", "TEXT NOT NULL REFERENCES sessions(session_id)"),
        ("source_row_id", "INTEGER"),
        ("model", "TEXT"),
        ("occurred_at", "TEXT"),
        ("input_tokens", "INTEGER"),
        ("output_tokens", "INTEGER"),
        ("cost_usd", "REAL"),
    ],
    "source_tombstones": [
        ("tombstone_id", "TEXT PRIMARY KEY"),
        ("session_id", "TEXT NOT NULL"),
        ("reason", "TEXT NOT NULL CHECK(reason IN ('secret','excluded','deleted','source_disappeared'))"),
        ("detail", "TEXT"),  # 无正文，仅元数据如 created_at
        ("created_at", "TEXT"),
    ],
}

NORMALIZED_INDEXES = [
    ("idx_norm_sessions_agent", "sessions", "agent"),
    ("idx_norm_sessions_evidence", "sessions", "evidence_eligible"),
    ("idx_norm_messages_session", "messages", "session_id, ordinal"),
    ("idx_norm_messages_role", "messages", "role"),
    ("idx_norm_tool_events_session", "tool_events", "session_id"),
    ("idx_norm_usage_session", "usage_events", "session_id"),
]


@dataclass
class NormalizationStats:
    """Revision gate 指标（PLAN Wave 2.2 Gate）。"""

    sessions_total: int = 0
    sessions_eligible: int = 0
    sessions_ineligible_secret: int = 0
    sessions_excluded: int = 0
    sessions_deleted: int = 0
    messages_total: int = 0
    messages_with_content: int = 0
    messages_quarantined_local: int = 0  # 二次扫描命中正文落库数（必须=0）
    messages_compact_summary: int = 0  # 压缩摘要消息数（标 system 轨，不进用户事实层）
    secret_session_messages_written: int = 0  # 必须为 0
    messages_skipped_secret: int = 0  # secret session 整条不写的消息数
    messages_skipped_excluded: int = 0  # excluded session 整条不写的消息数
    messages_skipped_deleted: int = 0  # deleted session 整条不写的消息数
    excluded_matched: int = 0  # excluded_sessions.id 能 JOIN 上 sessions.id 的行数
    excluded_unmatched: int = 0  # 匹配不上的行数（会话可能已物理删除，属正常）
    protected_field_copies: int = 0  # thinking/input_json/result 复制数（必须=0）
    tool_events_total: int = 0
    usage_events_total: int = 0
    tombstones_total: int = 0
    local_rule_hits: dict[str, int] = field(default_factory=dict)
    source_ref_backfill_rate: float = 0.0  # 必须 100%

    @property
    def gate_passed(self) -> bool:
        """Revision gate：所有必须为 0 的指标。"""
        return (
            self.protected_field_copies == 0
            and self.messages_quarantined_local == 0
            and self.secret_session_messages_written == 0
        )

    def to_dict(self) -> dict:
        return asdict(self)


def _norm_id(prefix: str, *parts: object) -> str:
    """稳定版本化 ID：v1|sha256(join(|) of parts)。"""
    payload = "|".join(str(p) for p in parts)
    return f"{prefix}|{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


def _session_evidence_scope(row: sqlite3.Row) -> str:
    """判定 session 的 evidence_scope。

    - parent_session_id 非空 → subagent（除非 relationship_type 表明是主链）
    - 否则 → user（可作用户事实证据）
    """
    parent = row["parent_session_id"]
    rel = (row["relationship_type"] or "").strip()
    if parent and rel and rel != "main":
        return "subagent"
    if parent and not rel:
        return "subagent"
    return "user"


def _message_evidence_scope(role: str, is_system: int, is_sidechain: int,
                             session_scope: str) -> str:
    """判定单条 message 的 evidence_scope。

    system → system；sidechain → sidechain；subagent session 的 assistant → subagent；
    assistant/tool → assistant（可支撑项目过程，不证明用户事实）。
    """
    if is_system:
        return "system"
    if is_sidechain:
        return "sidechain"
    if role == "user" and session_scope == "user":
        return "user"
    if role == "user":
        return "user"  # subagent 里的 user 也算 user，但 session_scope 已限定
    return "assistant"


def _create_schema(con: sqlite3.Connection) -> None:
    """在 staging 连接上创建 normalized schema。"""
    cur = con.cursor()
    for table, cols in NORMALIZED_SCHEMA.items():
        cur.execute(f"DROP TABLE IF EXISTS {table}")
        col_def = ", ".join(f"{c} {t}" for c, t in cols)
        cur.execute(f"CREATE TABLE {table} ({col_def})")
    for idx_name, table, cols in NORMALIZED_INDEXES:
        cur.execute(f"DROP INDEX IF EXISTS {idx_name}")
        cur.execute(f"CREATE INDEX {idx_name} ON {table} ({cols})")
    con.commit()


def _build_dataset_hash(stats: NormalizationStats) -> str:
    """对最终 stats 做稳定 hash，用于幂等校验（同输入重跑必须相同）。"""
    payload = repr(sorted(asdict(stats).items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def build_normalized(
    snapshot_path: Path,
    manifest: dict | None = None,
    dest_db: Path = AGENTSVIEW_NORMALIZED_DB,
    dry_run: bool = False,
) -> tuple[NormalizationStats, Path | None]:
    """从快照构建 normalized DB。

    流程：
      1. 读快照（只读）
      2. 计算 ineligible sessions（secret/excluded/deleted）
      3. 写 staging DB（临时文件）
      4. Revision gate 检查
      5. ``os.replace`` 原子发布（非 dry-run）
      6. 清理临时文件

    返回 ``(stats, final_db_path_or_none)``。dry_run 时 final 为 None，
    且不写任何正式 DB 文件。

    任何 Revision gate 失败都抛 :class:`RevisionGateError`，不发布。
    """
    stats = NormalizationStats()

    src = sqlite3.connect(f"file:{snapshot_path.as_posix()}?mode=ro", uri=True)
    src.execute("PRAGMA query_only=ON")
    src.row_factory = sqlite3.Row

    staging_path = dest_db.parent / f"{dest_db.stem}.staging.sqlite"

    try:
        # ineligible session 集合（正文完全不写）
        secret_session_ids: set[str] = {
            r["id"] for r in src.execute(
                "SELECT DISTINCT id FROM sessions WHERE secret_leak_count > 0"
            )
        }
        excluded_session_ids: set[str] = {
            r["id"] for r in src.execute("SELECT id FROM excluded_sessions")
        }
        deleted_session_ids: set[str] = {
            r["id"] for r in src.execute(
                "SELECT id FROM sessions WHERE deleted_at IS NOT NULL AND deleted_at != ''"
            )
        }

        # fail-closed gate：excluded_sessions 非空却一条都 JOIN 不上 sessions.id
        # → schema 假设（id == session id）不成立，宁可不发布。
        # 部分匹配属正常（会话可能已物理删除），只记 stats。
        stats.excluded_matched = src.execute(
            "SELECT COUNT(*) FROM excluded_sessions e "
            "JOIN sessions s ON e.id = s.id"
        ).fetchone()[0]
        stats.excluded_unmatched = len(excluded_session_ids) - stats.excluded_matched
        if excluded_session_ids and stats.excluded_matched == 0:
            raise RevisionGateError(
                stats,
                f"excluded_sessions 有 {len(excluded_session_ids)} 行但 0 行能 "
                f"JOIN 上 sessions.id（schema 假设不成立，拒绝发布）",
            )

        dst = sqlite3.connect(str(staging_path))
        _create_schema(dst)
        dcur = dst.cursor()

        tombstones: list[dict] = []
        # source session_id -> normalized session_id 映射（sessions 循环时填充）
        sid_map: dict[str, str] = {}
        # source session_id -> evidence_scope（messages 循环时用）
        scope_map: dict[str, str] = {}

        # --- sessions ---
        for srow in src.execute("SELECT * FROM sessions ORDER BY started_at, id"):
            src_sid = srow["id"]
            scope = _session_evidence_scope(srow)
            secret_count = int(srow["secret_leak_count"] or 0)
            is_secret = src_sid in secret_session_ids
            is_excluded = src_sid in excluded_session_ids
            is_deleted = src_sid in deleted_session_ids
            eligible = not (is_secret or is_excluded or is_deleted)

            reasons: dict[str, int] = {}
            if is_secret:
                reasons["secret"] = secret_count
                stats.sessions_ineligible_secret += 1
            if is_excluded:
                reasons["excluded"] = 1
                stats.sessions_excluded += 1
            if is_deleted:
                reasons["deleted"] = 1
                stats.sessions_deleted += 1
            if eligible:
                stats.sessions_eligible += 1
            stats.sessions_total += 1

            norm_sid = _norm_id("v1", "agentsview", src_sid)
            sid_map[src_sid] = norm_sid
            scope_map[src_sid] = scope
            dcur.execute(
                "INSERT OR REPLACE INTO sessions VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    norm_sid, src_sid, srow["agent"], srow["started_at"],
                    srow["ended_at"], srow["message_count"],
                    srow["user_message_count"], srow["parent_session_id"],
                    srow["relationship_type"], srow["source_session_id"],
                    srow["file_hash"], srow["cwd"], srow["git_branch"],
                    None,  # model 暂不回填（usage 表有更准确的每条 model）
                    secret_count, int(eligible), int(is_excluded),
                    srow["deleted_at"], scope,
                    json.dumps(reasons, ensure_ascii=False) if reasons else None,
                ),
            )

            # tombstones（正文永不进入，只记原因元数据）
            if is_secret:
                tombstones.append({
                    "tombstone_id": _norm_id("v1", "tomb", src_sid, "secret"),
                    "session_id": norm_sid, "reason": "secret",
                    "detail": f"secret_leak_count={secret_count}",
                    "created_at": srow["deleted_at"] or srow["ended_at"] or "",
                })
            if is_excluded:
                tombstones.append({
                    "tombstone_id": _norm_id("v1", "tomb", src_sid, "excluded"),
                    "session_id": norm_sid, "reason": "excluded",
                    "detail": "in excluded_sessions",
                    "created_at": "",
                })
            if is_deleted:
                tombstones.append({
                    "tombstone_id": _norm_id("v1", "tomb", src_sid, "deleted"),
                    "session_id": norm_sid, "reason": "deleted",
                    "detail": f"deleted_at={srow['deleted_at']}",
                    "created_at": srow["deleted_at"],
                })

        # --- messages（单轮循环）---
        messages_written: list[tuple] = []
        for mrow in src.execute(
            "SELECT * FROM messages ORDER BY session_id, ordinal"
        ):
            stats.messages_total += 1
            src_sid = mrow["session_id"]

            # secret/excluded/deleted session 的正文完全不写（Revision gate 强制，
            # 三类对称：整条不写，不是 content 置空）
            if src_sid in secret_session_ids:
                stats.messages_skipped_secret += 1
                continue
            if src_sid in excluded_session_ids:
                stats.messages_skipped_excluded += 1
                continue
            if src_sid in deleted_session_ids:
                stats.messages_skipped_deleted += 1
                continue

            norm_sid = sid_map.get(src_sid, src_sid)
            session_scope = scope_map.get(src_sid, "user")

            # role 归一化到 CHECK 白名单
            role = (mrow["role"] or "assistant").strip().lower()
            if role not in ("user", "assistant", "developer", "system", "tool"):
                role = "user" if role == "user" else "assistant"

            is_sys = int(mrow["is_system"] or 0)
            is_side = int(mrow["is_sidechain"] or 0)
            msg_scope = _message_evidence_scope(role, is_sys, is_side, session_scope)

            content = mrow["content"] or ""
            # 压缩摘要识别（LLM compact 总结不是用户原话）：复用 evidence_scope
            # 标记为 system 轨，使其不再以 user 身份进入抽取轨（eligibility 按
            # scope 过滤）。
            if is_compact_summary(content):
                msg_scope = "system"
                stats.messages_compact_summary += 1

            # 本地二次敏感信息扫描：命中则正文不落，只记规则名
            local_hits = local_secret_scan(content)
            quarantined_rules = None
            if local_hits:
                content_written = None
                quarantined_rules = ",".join(sorted(set(local_hits)))
                for rname in local_hits:
                    stats.local_rule_hits[rname] = (
                        stats.local_rule_hits.get(rname, 0) + 1
                    )
            else:
                content_written = content if content else None
                if content_written:
                    stats.messages_with_content += 1

            content_hash = (
                hashlib.sha256(
                    " ".join(content.split()).encode("utf-8")
                ).hexdigest()[:32]
                if content else None
            )

            norm_mid = _norm_id("v1", "agentsview", src_sid, mrow["ordinal"])
            messages_written.append((
                norm_mid, norm_sid, mrow["id"], mrow["ordinal"], role,
                content_written, len(content), mrow["timestamp"],
                mrow["model"], is_sys, is_side, content_hash, msg_scope,
                quarantined_rules,
            ))

        if messages_written:
            dcur.executemany(
                "INSERT OR REPLACE INTO messages VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                messages_written,
            )

        # --- tool_events (calls + results，只记元数据和长度) ---
        tool_rows: list[tuple] = []
        for crow in src.execute(
            "SELECT id, session_id, tool_name, category, call_index, "
            "subagent_session_id, skill_name, result_content_length "
            "FROM tool_calls ORDER BY session_id, id"
        ):
            src_sid = crow["session_id"]
            norm_sid = sid_map.get(src_sid, src_sid)
            stats.tool_events_total += 1
            tool_rows.append((
                _norm_id("v1", "toolcall", crow["id"]),
                norm_sid, "call", crow["id"], crow["tool_name"],
                crow["category"], None, crow["skill_name"],
                crow["call_index"], crow["subagent_session_id"],
                crow["result_content_length"], None, None,
            ))
        for rrow in src.execute(
            "SELECT id, session_id, status, subagent_session_id, event_index, "
            "content_length, timestamp FROM tool_result_events ORDER BY session_id, id"
        ):
            src_sid = rrow["session_id"]
            norm_sid = sid_map.get(src_sid, src_sid)
            stats.tool_events_total += 1
            tool_rows.append((
                _norm_id("v1", "toolresult", rrow["id"]),
                norm_sid, "result", rrow["id"], None, None,
                rrow["status"], None, None, rrow["subagent_session_id"],
                rrow["content_length"], rrow["timestamp"], rrow["event_index"],
            ))
        if tool_rows:
            dcur.executemany(
                "INSERT OR REPLACE INTO tool_events VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tool_rows,
            )

        # --- usage_events ---
        usage_cols = {col[1] for col in src.execute("PRAGMA table_info(usage_events)").fetchall()}
        cost_expr = "cost_usd" if "cost_usd" in usage_cols else "0.0 AS cost_usd"
        usage_rows: list[tuple] = []
        for urow in src.execute(
            f"SELECT id, session_id, model, occurred_at, input_tokens, "
            f"output_tokens, {cost_expr} FROM usage_events ORDER BY session_id, id"
        ):
            src_sid = urow["session_id"]
            norm_sid = sid_map.get(src_sid, src_sid)
            stats.usage_events_total += 1
            usage_rows.append((
                _norm_id("v1", "usage", urow["id"]),
                norm_sid, urow["id"], urow["model"], urow["occurred_at"],
                urow["input_tokens"], urow["output_tokens"], urow["cost_usd"],
            ))
        if usage_rows:
            dcur.executemany(
                "INSERT OR REPLACE INTO usage_events VALUES (?,?,?,?,?,?,?,?)",
                usage_rows,
            )

        # --- tombstones ---
        for t in tombstones:
            stats.tombstones_total += 1
            dcur.execute(
                "INSERT OR REPLACE INTO source_tombstones VALUES (?,?,?,?,?)",
                (t["tombstone_id"], t["session_id"], t["reason"],
                 t["detail"], t["created_at"]),
            )

        # --- source_ref 回查率：写入数 /（总数 - 三类 ineligible 跳过数）---
        # ineligible（secret/excluded/deleted）会话的消息按设计整条不写，
        # 对账期望值同步排除，避免误伤 backfill 率。
        skipped_msgs = (
            stats.messages_skipped_secret
            + stats.messages_skipped_excluded
            + stats.messages_skipped_deleted
        )
        expected_msgs = stats.messages_total - skipped_msgs
        if expected_msgs:
            stats.source_ref_backfill_rate = round(
                len(messages_written) / expected_msgs, 4
            )
        else:
            stats.source_ref_backfill_rate = 1.0

        # --- import_runs ---
        dataset_hash = _build_dataset_hash(stats)
        m = manifest or {}
        dcur.execute(
            "INSERT OR REPLACE INTO import_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                m.get("run_id", "manual"),
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                m.get("source_user_version"),
                m.get("source_integrity", "ok"),
                m.get("schema_hash"),
                m.get("config_hash"),
                "13.5.w2",
                dataset_hash,
                "ok" if stats.gate_passed else "blocked",
                json.dumps(stats.to_dict(), ensure_ascii=False),
            ),
        )

        dst.commit()
        dst.close()

        # Revision gate
        if not stats.gate_passed:
            staging_path.unlink(missing_ok=True)
            raise RevisionGateError(stats)

        if dry_run:
            staging_path.unlink(missing_ok=True)
            return stats, None

        # 原子发布
        dest_db.parent.mkdir(parents=True, exist_ok=True)
        # 备份旧文件（如果存在）
        if dest_db.exists():
            backup = dest_db.parent / f"{dest_db.stem}.backup.sqlite"
            os.replace(dest_db, backup)
        os.replace(staging_path, dest_db)
        return stats, dest_db

    finally:
        src.close()
        if staging_path.exists():
            try:
                staging_path.unlink()
            except OSError:
                pass


class RevisionGateError(RuntimeError):
    """normalized Revision gate 失败（protected 字段复制、secret 正文落库、
    或排除表 schema 假设不成立等 fail-closed 检查）。"""

    def __init__(self, stats: NormalizationStats, reason: str | None = None) -> None:
        self.stats = stats
        if reason is None:
            reason = (
                f"protected_field_copies={stats.protected_field_copies}, "
                f"messages_quarantined_local={stats.messages_quarantined_local}, "
                f"secret_session_messages_written="
                f"{stats.secret_session_messages_written}"
            )
        super().__init__(f"Revision gate failed: {reason}")


def main(argv: list[str] | None = None) -> int:
    """CLI：从 AgentView 源库构建 normalized DB。

    用法::

        python build_agentsview_normalized.py --dry-run
        python build_agentsview_normalized.py --write
    """
    import argparse
    from personal_knowledge.adapters.agentsview import AgentViewAdapter, SchemaGateError

    p = argparse.ArgumentParser(
        description="Phase 13.5 Wave 2: AgentView normalized snapshot 构建"
    )
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--write", action="store_true")
    p.add_argument("--source", type=Path, default=AGENTSVIEW_DB)
    p.add_argument("--dest", type=Path, default=AGENTSVIEW_NORMALIZED_DB)
    args = p.parse_args(argv)

    adapter = AgentViewAdapter(args.source)
    probe = adapter.probe()
    if not probe.ok:
        print(f"[blocked] schema gate 失败: integrity={probe.integrity_check}, "
              f"missing={probe.required_tables_missing}")
        return 1

    try:
        manifest, snap = adapter.snapshot(probe=probe)
    except SchemaGateError as exc:
        print(f"[blocked] {exc}")
        return 1

    try:
        stats, final = build_normalized(
            snap, manifest=manifest.to_dict(), dest_db=args.dest,
            dry_run=not args.write,
        )
    finally:
        snap.unlink(missing_ok=True)

    import json
    print("=" * 60)
    print("Phase 13.5 Wave 2：Normalized Snapshot")
    print("=" * 60)
    print(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
    print(f"gate_passed: {stats.gate_passed}")
    if final:
        print(f"[ok] normalized DB 已发布: {final}")
    else:
        print("[dry-run] 未写入")
    return 0 if stats.gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

