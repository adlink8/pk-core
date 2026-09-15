"""Phase 13.5 Wave 3：Legacy 去重与 canonical conversation store。

输入：legacy ``agent_data.sqlite`` + Wave 2 ``agentsview_normalized.sqlite``
输出：``data/canonical/agent/structured/db/agent_conversations.sqlite``

匹配优先级（CONTEXT.md D-06）：
  1. file_hash 精确匹配（AgentView sessions.file_hash == legacy source_files.sha256）
     → 合并为一个 canonical session，AgentView 表示优先，legacy raw refs 作 provenance
  2. 明确 source mapping（source_session_id / raw_file basename）
  3. 版本化稳定签名（无强证据时不自动合并，写 review_required）

canonical store 契约：
  - canonical message 保留 role；assistant/subagent/tool 不能伪装为 user evidence
  - parent/subagent session 保持独立内容边界，通过 relation 表连接
  - staging + 原子 replace；旧 canonical store 发布前备份
  - legacy-only 和 AgentView-only session 都可回查
  - 无强证据的相似会话不自动合并

用法::

    python build_canonical_agent_conversations.py --dry-run
    python build_canonical_agent_conversations.py --write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from personal_knowledge.core.project_paths import (
    AGENT_DB,
    AGENTSVIEW_NORMALIZED_DB,
    AGENT_CONVERSATIONS_DB,
)

CANONICAL_SCHEMA: dict[str, list[tuple[str, str]]] = {
    "canonical_sessions": [
        ("canonical_session_id", "TEXT PRIMARY KEY"),
        ("primary_source", "TEXT NOT NULL CHECK(primary_source IN ('agentsview','legacy'))"),
        ("agent", "TEXT"),
        ("started_at", "TEXT"),
        ("ended_at", "TEXT"),
        ("message_count", "INTEGER"),
        ("user_message_count", "INTEGER"),
        ("file_hash", "TEXT"),
        ("parent_canonical_id", "TEXT"),
        ("relationship_type", "TEXT"),
        ("cwd", "TEXT"),
        ("git_branch", "TEXT"),
        ("model", "TEXT"),
        ("evidence_eligible", "INTEGER NOT NULL DEFAULT 1"),
        ("evidence_scope", "TEXT NOT NULL DEFAULT 'user'"),
        ("merged", "INTEGER NOT NULL DEFAULT 0"),  # 是否由多 source 合并
        ("lifecycle", "TEXT NOT NULL DEFAULT 'active'"),
        ("superseded_by_canonical_id", "TEXT"),
    ],
    "canonical_messages": [
        ("canonical_message_id", "TEXT PRIMARY KEY"),
        ("canonical_session_id", "TEXT NOT NULL REFERENCES canonical_sessions(canonical_session_id)"),
        ("source", "TEXT NOT NULL CHECK(source IN ('agentsview','legacy'))"),
        ("source_message_ref", "TEXT"),  # 原 source 的 message ID（回查用）
        ("ordinal", "INTEGER NOT NULL"),
        ("role", "TEXT NOT NULL CHECK(role IN ('user','assistant','developer','system','tool'))"),
        ("content", "TEXT"),  # 仅 eligible + 脱敏后
        ("content_length", "INTEGER"),
        ("timestamp", "TEXT"),
        ("model", "TEXT"),
        ("is_system", "INTEGER NOT NULL DEFAULT 0"),
        ("is_sidechain", "INTEGER NOT NULL DEFAULT 0"),
        ("content_hash", "TEXT"),
        ("evidence_scope", "TEXT NOT NULL DEFAULT 'user'"),
    ],
    "canonical_tool_events": [
        ("canonical_tool_id", "TEXT PRIMARY KEY"),
        ("canonical_session_id", "TEXT NOT NULL REFERENCES canonical_sessions(canonical_session_id)"),
        ("source", "TEXT NOT NULL CHECK(source IN ('agentsview','legacy'))"),
        ("source_kind", "TEXT NOT NULL"),
        ("tool_name", "TEXT"),
        ("category", "TEXT"),
        ("status", "TEXT"),
        ("call_index", "INTEGER"),
        ("subagent_session_id", "TEXT"),
        ("content_length", "INTEGER"),
        ("timestamp", "TEXT"),
    ],
    "session_source_links": [
        ("link_id", "TEXT PRIMARY KEY"),
        ("canonical_session_id", "TEXT NOT NULL REFERENCES canonical_sessions(canonical_session_id)"),
        ("source", "TEXT NOT NULL CHECK(source IN ('agentsview','legacy'))"),
        ("source_session_id", "TEXT NOT NULL"),
        ("source_raw_file", "TEXT"),
        ("match_method", "TEXT NOT NULL CHECK(match_method IN ('file_hash','source_mapping','review_required','single_source'))"),
        ("match_confidence", "TEXT NOT NULL DEFAULT 'strong'"),
    ],
    "session_relations": [
        ("relation_id", "TEXT PRIMARY KEY"),
        ("parent_canonical_id", "TEXT NOT NULL"),
        ("child_canonical_id", "TEXT NOT NULL"),
        ("relationship_type", "TEXT"),
    ],
    "crosswalk_review": [
        ("review_id", "TEXT PRIMARY KEY"),
        ("agentsview_session_id", "TEXT"),
        ("legacy_session_id", "TEXT"),
        ("reason", "TEXT"),
        ("similarity_score", "REAL"),
    ],
}

CANONICAL_INDEXES = [
    ("idx_cs_agent", "canonical_sessions", "agent"),
    ("idx_cs_evidence", "canonical_sessions", "evidence_eligible"),
    ("idx_cm_session", "canonical_messages", "canonical_session_id, ordinal"),
    ("idx_cm_role", "canonical_messages", "role"),
    ("idx_cte_session", "canonical_tool_events", "canonical_session_id"),
    ("idx_ssl_canonical", "session_source_links", "canonical_session_id"),
    ("idx_ssl_source", "session_source_links", "source, source_session_id"),
]


def _norm_id(prefix: str, *parts: object) -> str:
    payload = "|".join(str(p) for p in parts)
    return f"{prefix}|{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


@dataclass
class CrosswalkStats:
    agentsview_sessions: int = 0
    legacy_sessions: int = 0
    merged_by_file_hash: int = 0
    merged_by_source_mapping: int = 0
    agentsview_only: int = 0
    legacy_only: int = 0
    review_required: int = 0
    canonical_sessions: int = 0
    canonical_messages: int = 0
    canonical_tool_events: int = 0
    duplicate_source_links: int = 0  # Revision gate: 必须 0
    review_auto_merged: int = 0  # Revision gate: review 项被自动合并数必须 0
    stable_key_matched: int = 0
    file_hash_confirmed: int = 0
    file_hash_divergent: int = 0
    superseded_marked: int = 0
    unexpected_duplicate_stable_key: int = 0


def _native_session_uuid(source: str, session_id: str) -> str | None:
    """Return the cross-source native UUID, without using source_session_ref."""
    value = str(session_id or "")
    if source == "agentsview" and value.startswith("codex:"):
        value = value[len("codex:"):]
    elif source == "legacy":
        match = re.search(
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
            value,
            flags=re.IGNORECASE,
        )
        value = match.group(1) if match else ""
    return value.lower() if value else None


def _load_agentsview_sessions(db: Path) -> list[dict]:
    """从 normalized DB 读 eligible agent sessions + messages + tools。"""
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sessions = [
        dict(r) for r in con.execute(
            "SELECT session_id, source_session_id, agent, started_at, ended_at, "
            "message_count, user_message_count, file_hash, parent_session_id, "
            "relationship_type, cwd, git_branch, evidence_eligible, evidence_scope "
            "FROM sessions"
            " ORDER BY started_at, session_id"
        )
    ]
    con.close()
    return sessions


def _load_legacy_sessions(db: Path) -> list[dict]:
    """从 legacy agent_data 读 session meta（session_id 去重）。

    legacy ``agent_sessions_meta`` 的 session_id 不唯一（同一 session 的
    每个 jsonl 行都有一条 meta）。这里按 session_id 去重，每个 session
    只保留第一条记录作为代表。
    """
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sessions = [
        dict(r) for r in con.execute(
            "SELECT session_id, source, family, raw_file, timestamp, cwd, model "
            "FROM agent_sessions_meta "
            "ORDER BY timestamp ASC, raw_file ASC, rowid ASC"
        )
    ]
    con.close()
    # session_id 去重（保留第一条）
    seen: set[str] = set()
    unique: list[dict] = []
    for s in sessions:
        sid = s["session_id"]
        if sid not in seen:
            seen.add(sid)
            unique.append(s)
    return unique


def _build_legacy_file_hash_index(legacy_db: Path) -> dict[str, str]:
    """legacy: raw_file basename → sha256（用于与 AgentView file_hash 匹配）。

    AgentView 的 file_hash 是原始会话文件的 sha256；
    legacy source_files.sha256 也是文件 sha256。
    但 legacy 的 session_id 是 rollout 文件名，source_files 记录的是扫描到的文件。
    匹配需要通过文件路径/名称关联。
    """
    if not legacy_db.exists():
        return {}
    con = sqlite3.connect(f"file:{legacy_db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    # 直接取 source_files 的 sha256 → relative_path 映射
    hash_to_path: dict[str, str] = {}
    for r in con.execute(
        "SELECT sha256, relative_path, copied_path FROM source_files "
        "WHERE sha256 IS NOT NULL AND sha256 != ''"
    ):
        hash_to_path[r["sha256"]] = r["relative_path"] or r["copied_path"] or ""
    con.close()
    return hash_to_path


def _legacy_session_to_hash(legacy_sessions: list[dict],
                             legacy_db: Path) -> dict[str, str]:
    """为 legacy session 关联 file_hash。

    匹配链：session.raw_file → source_files.copied_path（路径后缀匹配）→ sha256。

    raw_file 格式：``Agent\\raw\\Codex\\archived_sessions\\rollout-xxx.jsonl``
    copied_path 格式：``raw\\Codex\\archived_sessions\\rollout-xxx.jsonl``
    两者只差一个 ``Agent\\`` 前缀，用归一化后的完整相对路径精确匹配，
    避免 basename 碰撞（如 history.jsonl 在多个项目目录中）。
    """
    if not legacy_db.exists():
        return {}
    con = sqlite3.connect(f"file:{legacy_db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    # 归一化路径（去前缀差异，统一 / 分隔）→ sha256
    path_to_hash: dict[str, str] = {}
    for r in con.execute(
        "SELECT sha256, copied_path, relative_path FROM source_files "
        "WHERE sha256 IS NOT NULL AND sha256 != ''"
    ):
        for p in (r["copied_path"], r["relative_path"]):
            if not p:
                continue
            norm = p.replace("\\", "/")
            # 去掉 raw/ 前缀（copied_path）使其与 raw_file 去掉 Agent/raw/ 后对齐
            if norm.startswith("raw/"):
                norm = norm[4:]
            path_to_hash[norm] = r["sha256"]
    con.close()

    result: dict[str, str] = {}
    for s in legacy_sessions:
        raw_file = (s.get("raw_file") or "").replace("\\", "/")
        # 去掉 Agent/raw/ 前缀
        if raw_file.startswith("Agent/raw/"):
            norm = raw_file[len("Agent/raw/"):]
        elif raw_file.startswith("raw/"):
            norm = raw_file[4:]
        else:
            norm = raw_file
        if norm in path_to_hash:
            result[s["session_id"]] = path_to_hash[norm]
    return result


def build_crosswalk(
    agentsview_sessions: list[dict],
    legacy_sessions: list[dict],
    legacy_db: Path,
    stats: CrosswalkStats,
) -> tuple[list[dict], list[dict]]:
    """构建 canonical session 列表 + source links。

    返回 ``(canonical_sessions_meta, source_links)``。
    canonical_sessions_meta 每项含 canonical_session_id, primary_source, merged source ids。
    """
    stats.agentsview_sessions = len(agentsview_sessions)
    stats.legacy_sessions = len(legacy_sessions)

    # Stable source-session index. Unexpected duplicate native keys are kept
    # deterministic rather than silently overwritten by a hash index.
    av_by_sid: dict[str, dict] = {}
    for s in agentsview_sessions:
        sid = s.get("source_session_id") or s.get("session_id")
        if not sid:
            continue
        previous = av_by_sid.get(sid)
        if previous is not None:
            stats.unexpected_duplicate_stable_key += 1
            candidates = [previous, s]
            av_by_sid[sid] = sorted(
                candidates,
                key=lambda x: (x.get("started_at") or "", x.get("session_id") or sid),
            )[0]
        else:
            av_by_sid[sid] = s
    av_sessions = sorted(av_by_sid.values(), key=lambda x: (
        x.get("started_at") or "", x.get("source_session_id") or ""
    ))

    legacy_hash = _legacy_session_to_hash(legacy_sessions, legacy_db)
    legacy_by_hash: dict[str, list[dict]] = {}
    legacy_by_uuid: dict[str, list[dict]] = {}
    for s in legacy_sessions:
        fh = legacy_hash.get(s["session_id"])
        if fh:
            legacy_by_hash.setdefault(fh, []).append(s)
        native = _native_session_uuid("legacy", s["session_id"])
        if native:
            legacy_by_uuid.setdefault(native, []).append(s)
    for values in legacy_by_hash.values():
        values.sort(key=lambda x: (
            x.get("timestamp") or "", x.get("raw_file") or "", x.get("session_id") or ""
        ))
    for values in legacy_by_uuid.values():
        values.sort(key=lambda x: (
            x.get("timestamp") or "", x.get("raw_file") or "", x.get("session_id") or ""
        ))

    legacy_message_counts: dict[str, int] = {}
    if legacy_db.exists():
        con = sqlite3.connect(f"file:{legacy_db.as_posix()}?mode=ro", uri=True)
        legacy_message_counts = dict(con.execute(
            "SELECT session_id, COUNT(*) FROM agent_messages GROUP BY session_id"
        ).fetchall())
        con.close()

    canonical_list: list[dict] = []
    source_links: list[dict] = []
    seen_av: set[str] = set()
    seen_legacy: set[str] = set()

    def add_canonical(av_sess: dict | None, leg_sess: dict | None,
                      *, match_method: str, merged: bool) -> dict:
        av_sid = (av_sess or {}).get("source_session_id") if av_sess else None
        leg_sid = (leg_sess or {}).get("session_id") if leg_sess else None
        csid = _norm_id(
            "cs", "agentsview", av_sid
        ) if av_sid else _norm_id("cs", "legacy", leg_sid)
        item = {
            "canonical_session_id": csid,
            "primary_source": "agentsview" if av_sess else "legacy",
            "agent": (av_sess or {}).get("agent") or (leg_sess or {}).get("source"),
            "started_at": (av_sess or {}).get("started_at") or (leg_sess or {}).get("timestamp"),
            "ended_at": (av_sess or {}).get("ended_at"),
            "message_count": (av_sess or {}).get("message_count") or (
                legacy_message_counts.get(leg_sid, 0) if leg_sid else None
            ),
            "user_message_count": (av_sess or {}).get("user_message_count"),
            "file_hash": (av_sess or {}).get("file_hash") or (legacy_hash.get(leg_sid) if leg_sid else None),
            "parent_session_id": (av_sess or {}).get("parent_session_id"),
            "relationship_type": (av_sess or {}).get("relationship_type"),
            "cwd": (av_sess or {}).get("cwd") or (leg_sess or {}).get("cwd"),
            "git_branch": (av_sess or {}).get("git_branch"),
            "evidence_eligible": (av_sess or {}).get("evidence_eligible", 1),
            "evidence_scope": (av_sess or {}).get("evidence_scope", "user"),
            "merged": int(merged),
            "lifecycle": "active",
            "superseded_by_canonical_id": None,
            "_av_session_id": av_sid,
            "_legacy_session_id": leg_sid,
            "_legacy_raw_file": (leg_sess or {}).get("raw_file"),
            "_message_count_hint": (av_sess or {}).get("message_count") or legacy_message_counts.get(leg_sid, 0),
        }
        canonical_list.append(item)
        if av_sid:
            source_links.append({
                "link_id": _norm_id("link", csid, "agentsview", av_sid),
                "canonical_session_id": csid, "source": "agentsview",
                "source_session_id": av_sid, "source_raw_file": None,
                "match_method": match_method, "match_confidence": "strong",
            })
            seen_av.add(av_sid)
        if leg_sid:
            source_links.append({
                "link_id": _norm_id("link", csid, "legacy", leg_sid),
                "canonical_session_id": csid, "source": "legacy",
                "source_session_id": leg_sid,
                "source_raw_file": (leg_sess or {}).get("raw_file"),
                "match_method": match_method, "match_confidence": "strong",
            })
            seen_legacy.add(leg_sid)
        return item

    # Pass 1: native source mapping is identity. file_hash is only a signal.
    for av_sess in av_sessions:
        av_sid = av_sess["source_session_id"]
        native = _native_session_uuid("agentsview", av_sid)
        candidates = [s for s in legacy_by_uuid.get(native or "", []) if s["session_id"] not in seen_legacy]
        if not candidates:
            continue
        leg_sess = candidates[0]
        add_canonical(av_sess, leg_sess, match_method="source_mapping", merged=True)
        stats.stable_key_matched += 1
        stats.merged_by_source_mapping += 1
        av_hash = av_sess.get("file_hash")
        leg_hash = legacy_hash.get(leg_sess["session_id"])
        if av_hash and leg_hash:
            if av_hash == leg_hash:
                stats.file_hash_confirmed += 1
            else:
                stats.file_hash_divergent += 1

    # Pass 2: file_hash is a fallback only for sessions without native mapping.
    for av_sess in av_sessions:
        av_sid = av_sess["source_session_id"]
        if av_sid in seen_av:
            continue
        fh = av_sess.get("file_hash")
        candidates = [s for s in legacy_by_hash.get(fh or "", []) if s["session_id"] not in seen_legacy]
        if candidates:
            leg_sess = candidates[0]
            add_canonical(av_sess, leg_sess, match_method="file_hash", merged=True)
            stats.merged_by_file_hash += 1
            continue
        add_canonical(av_sess, None, match_method="single_source", merged=False)
        stats.agentsview_only += 1

    # Pass 3: legacy-only sessions. Same-hash legacy twins remain separate so
    # lifecycle/supersede semantics are observable instead of being collapsed.
    for leg_sess in legacy_sessions:
        leg_sid = leg_sess["session_id"]
        if leg_sid in seen_legacy:
            continue
        add_canonical(None, leg_sess, match_method="single_source", merged=False)
        stats.legacy_only += 1

    weak_groups: dict[str, list[dict]] = {}
    for item in canonical_list:
        if item["_av_session_id"] is None and item.get("file_hash"):
            weak_groups.setdefault(item["file_hash"], []).append(item)
    for group in weak_groups.values():
        if len(group) < 2:
            continue
        highest_count = max(x.get("_message_count_hint") or 0 for x in group)
        count_tied = [x for x in group if (x.get("_message_count_hint") or 0) == highest_count]
        latest_started = max(x.get("started_at") or "" for x in count_tied)
        active = sorted(
            [x for x in count_tied if (x.get("started_at") or "") == latest_started],
            key=lambda x: x["canonical_session_id"],
        )[0]
        for item in group:
            if item is active:
                continue
            item["lifecycle"] = "superseded"
            item["superseded_by_canonical_id"] = active["canonical_session_id"]
            item["evidence_eligible"] = 0
            stats.superseded_marked += 1

    stats.canonical_sessions = len(canonical_list)
    return canonical_list, source_links


def _write_canonical_store(
    dest_db: Path,
    canonical_list: list[dict],
    source_links: list[dict],
    av_db: Path,
    legacy_db: Path,
    stats: CrosswalkStats,
    dry_run: bool,
) -> Path | None:
    """写 canonical store：sessions + messages + tools + links + relations。"""
    staging = dest_db.parent / f"{dest_db.stem}.staging.sqlite"
    con = sqlite3.connect(str(staging))
    cur = con.cursor()

    # 建 schema
    for table, cols in CANONICAL_SCHEMA.items():
        cur.execute(f"DROP TABLE IF EXISTS {table}")
        col_def = ", ".join(f"{c} {t}" for c, t in cols)
        cur.execute(f"CREATE TABLE {table} ({col_def})")
    for idx_name, table, cols in CANONICAL_INDEXES:
        cur.execute(f"DROP INDEX IF EXISTS {idx_name}")
        cur.execute(f"CREATE INDEX {idx_name} ON {table} ({cols})")

    # av_session_id → canonical_session_id 映射
    av_to_cs: dict[str, str] = {}
    legacy_to_cs: dict[str, str] = {}
    av_parent_to_cs: dict[str, str] = {}

    # canonical_sessions（16 列，按 schema 顺序）
    for c in canonical_list:
        csid = c["canonical_session_id"]
        cur.execute(
            "INSERT INTO canonical_sessions VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                csid, c["primary_source"], c["agent"], c["started_at"],
                c["ended_at"], c["message_count"], c["user_message_count"],
                c["file_hash"], None,  # parent_canonical_id（第二趟回填）
                c["relationship_type"], c["cwd"], c["git_branch"], None,  # model
                c["evidence_eligible"], c["evidence_scope"],
                c["merged"], c["lifecycle"], c["superseded_by_canonical_id"],
            ),
        )
        if c["_av_session_id"]:
            av_to_cs[c["_av_session_id"]] = csid
            av_parent_to_cs[c["_av_session_id"]] = csid
        if c["_legacy_session_id"]:
            legacy_to_cs[c["_legacy_session_id"]] = csid

    # 补全 legacy_to_cs：所有 source_links 里的 legacy session 都映射到对应 canonical
    for link in source_links:
        if link["source"] == "legacy":
            legacy_to_cs[link["source_session_id"]] = link["canonical_session_id"]

    # source_links + 重复检测
    seen_links: set[str] = set()
    for link in source_links:
        key = (link["canonical_session_id"], link["source"], link["source_session_id"])
        if key in seen_links:
            stats.duplicate_source_links += 1
            continue
        seen_links.add(key)
        cur.execute(
            "INSERT INTO session_source_links VALUES (?,?,?,?,?,?,?)",
            (
                link["link_id"], link["canonical_session_id"], link["source"],
                link["source_session_id"], link["source_raw_file"],
                link["match_method"], link["match_confidence"],
            ),
        )

    def _table_exists(c: sqlite3.Connection, name: str) -> bool:
        return c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    # canonical_messages：从 normalized（AgentView）和 legacy 读
    msg_rows: list[tuple] = []
    tool_rows: list[tuple] = []

    # AgentView messages + tool events（从 normalized DB）
    if av_db.exists():
        probe_con = sqlite3.connect(f"file:{av_db.as_posix()}?mode=ro", uri=True)
        has_sessions = _table_exists(probe_con, "sessions")
        has_av_messages = _table_exists(probe_con, "messages")
        has_av_tools = _table_exists(probe_con, "tool_events")
        probe_con.close()

        if has_sessions:
            avcon = sqlite3.connect(f"file:{av_db.as_posix()}?mode=ro", uri=True)
            avcon.row_factory = sqlite3.Row
            # normalized messages.session_id → sessions.source_session_id → AV id
            av_msg_sessions: dict[str, str] = {}
            # eligible session 集合（与 legacy 路径对称：ineligible session
            # 正文不进 canonical，即使旧版 normalized 库里还留着正文）
            av_eligible_sessions: set[str] = set()
            for r in avcon.execute(
                "SELECT session_id, source_session_id, evidence_eligible "
                "FROM sessions"
            ):
                av_msg_sessions[r["session_id"]] = r["source_session_id"]
                if r["evidence_eligible"]:
                    av_eligible_sessions.add(r["session_id"])

            if has_av_messages:
                for m in avcon.execute(
                    "SELECT message_id, session_id, source_message_id, ordinal, role, "
                    "content, content_length, timestamp, model, is_system, is_sidechain, "
                    "content_hash, evidence_scope FROM messages "
                    "ORDER BY session_id, ordinal"
                ):
                    # ineligible（secret/excluded/deleted）session 正文不可检索
                    if m["session_id"] not in av_eligible_sessions:
                        continue
                    av_src = av_msg_sessions.get(m["session_id"])
                    csid = av_to_cs.get(av_src)
                    if not csid:
                        continue
                    stats.canonical_messages += 1
                    msg_rows.append((
                        _norm_id("cm", "av", m["source_message_id"] or m["message_id"]),
                        csid, "agentsview", str(m["source_message_id"] or m["message_id"]),
                        m["ordinal"], m["role"], m["content"], m["content_length"],
                        m["timestamp"], m["model"], m["is_system"], m["is_sidechain"],
                        m["content_hash"], m["evidence_scope"],
                    ))

            if has_av_tools:
                for t in avcon.execute(
                    "SELECT tool_event_id, session_id, source_kind, tool_name, category, "
                    "status, call_index, subagent_session_id, content_length, timestamp "
                    "FROM tool_events ORDER BY session_id"
                ):
                    av_src = av_msg_sessions.get(t["session_id"])
                    csid = av_to_cs.get(av_src)
                    if not csid:
                        continue
                    stats.canonical_tool_events += 1
                    tool_rows.append((
                        _norm_id("cte", "av", t["tool_event_id"]),
                        csid, "agentsview", t["source_kind"], t["tool_name"],
                        t["category"], t["status"], t["call_index"],
                        t["subagent_session_id"], t["content_length"], t["timestamp"],
                    ))
            avcon.close()

    # Legacy messages
    # 策略：merged session 如果 AgentView 已提供 message 则跳过 legacy；
    # 如果 AV 没有该 session 的 message（canon 空壳），用 legacy 填充。
    # 但 ineligible session（secret/excluded/deleted）的 legacy message 也不写，
    # 保持隐私 gate：ineligible session 正文不可检索。
    merged_csids = {c["canonical_session_id"] for c in canonical_list if c["merged"]}
    ineligible_csids = {
        c["canonical_session_id"] for c in canonical_list
        if not c.get("evidence_eligible", 1) and c.get("lifecycle") != "superseded"
    }
    av_populated_csids: set[str] = {r[1] for r in msg_rows}  # 已有 AV message 的 csid
    if legacy_db.exists():
        lcon = sqlite3.connect(f"file:{legacy_db.as_posix()}?mode=ro", uri=True)
        lcon.row_factory = sqlite3.Row
        ordinal_counter: dict[str, int] = {}  # csid → next ordinal
        for m in lcon.execute(
            "SELECT session_id, event_index, timestamp, role, text "
            "FROM agent_messages ORDER BY session_id, event_index"
        ):
            csid = legacy_to_cs.get(m["session_id"])
            if not csid:
                continue
            # ineligible session 的 legacy message 也不写（隐私 gate）
            if csid in ineligible_csids:
                continue
            # merged session：AV 优先，但 AV 空壳时用 legacy 填充
            if csid in merged_csids and csid in av_populated_csids:
                continue
            role = (m["role"] or "assistant").lower()
            if role not in ("user", "assistant", "developer", "system", "tool"):
                role = "assistant"
            ordinal = ordinal_counter.get(csid, 0) + 1
            ordinal_counter[csid] = ordinal
            content = m["text"] or ""
            chash = hashlib.sha256(" ".join(content.split()).encode("utf-8")).hexdigest()[:32] if content else None
            stats.canonical_messages += 1
            msg_rows.append((
                _norm_id("cm", "legacy", m["session_id"], m["event_index"]),
                csid, "legacy", f"legacy:{m['session_id']}:{m['event_index']}",
                ordinal, role, content if content else None, len(content),
                m["timestamp"], None, 0, 0, chash, "user",
            ))
        lcon.close()

    if msg_rows:
        cur.executemany(
            "INSERT OR REPLACE INTO canonical_messages VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            msg_rows,
        )
    if tool_rows:
        cur.executemany(
            "INSERT OR REPLACE INTO canonical_tool_events VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?)",
            tool_rows,
        )

    # session_relations（parent/subagent）
    for c in canonical_list:
        if c.get("parent_session_id") and c["_av_session_id"]:
            parent_csid = av_parent_to_cs.get(c["parent_session_id"])
            if parent_csid and parent_csid != c["canonical_session_id"]:
                cur.execute(
                    "INSERT OR REPLACE INTO session_relations VALUES (?,?,?,?)",
                    (
                        _norm_id("rel", parent_csid, c["canonical_session_id"]),
                        parent_csid, c["canonical_session_id"],
                        c["relationship_type"],
                    ),
                )

    con.commit()
    con.close()

    if dry_run:
        staging.unlink(missing_ok=True)
        return None

    dest_db.parent.mkdir(parents=True, exist_ok=True)
    if dest_db.exists():
        backup = dest_db.parent / f"{dest_db.stem}.backup.sqlite"
        os.replace(dest_db, backup)
    os.replace(staging, dest_db)
    return dest_db


def run(dry_run: bool, write: bool,
        av_db: Path = AGENTSVIEW_NORMALIZED_DB,
        legacy_db: Path = AGENT_DB,
        dest_db: Path = AGENT_CONVERSATIONS_DB) -> int:
    if dry_run and write:
        print("[error] --dry-run 与 --write 互斥", file=sys.stderr)
        return 2

    stats = CrosswalkStats()

    av_sessions = _load_agentsview_sessions(av_db)
    legacy_sessions = _load_legacy_sessions(legacy_db)

    canonical_list, source_links = build_crosswalk(
        av_sessions, legacy_sessions, legacy_db, stats
    )

    final = _write_canonical_store(
        dest_db, canonical_list, source_links, av_db, legacy_db, stats, dry_run
    )

    print("=" * 60)
    print("Phase 13.5 Wave 3：Canonical Conversation Store")
    print("=" * 60)
    print(f"AgentView sessions: {stats.agentsview_sessions}")
    print(f"Legacy sessions:    {stats.legacy_sessions}")
    print(f"Merged (file_hash): {stats.merged_by_file_hash}")
    print(f"Merged (source_mapping): {stats.merged_by_source_mapping}")
    print(f"stable_key_matched: {stats.stable_key_matched}")
    print(f"file_hash_confirmed: {stats.file_hash_confirmed}")
    print(f"file_hash_divergent: {stats.file_hash_divergent}")
    print(f"superseded_marked: {stats.superseded_marked}")
    print(f"unexpected_duplicate_stable_key: {stats.unexpected_duplicate_stable_key}")
    print(f"AgentView-only:     {stats.agentsview_only}")
    print(f"Legacy-only:        {stats.legacy_only}")
    print(f"Review required:    {stats.review_required}")
    print(f"--- canonical ---")
    print(f"Sessions:           {stats.canonical_sessions}")
    print(f"Messages:           {stats.canonical_messages}")
    print(f"Tool events:        {stats.canonical_tool_events}")
    print(f"Duplicate links:    {stats.duplicate_source_links} (must be 0)")
    print(f"Review auto-merged: {stats.review_auto_merged} (must be 0)")

    if final:
        print(f"\n[ok] canonical store 已发布: {final}")
    elif dry_run:
        print(f"\n[dry-run] 未写入")
    return 0 if stats.duplicate_source_links == 0 else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Phase 13.5 Wave 3: canonical conversation store"
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--write", action="store_true")
    p.add_argument("--av-db", type=Path, default=AGENTSVIEW_NORMALIZED_DB)
    p.add_argument("--legacy-db", type=Path, default=AGENT_DB)
    p.add_argument("--dest-db", type=Path, default=AGENT_CONVERSATIONS_DB)
    args = p.parse_args(argv)
    if not args.dry_run and not args.write:
        args.dry_run = True  # 默认 dry-run
    return run(args.dry_run, args.write, args.av_db, args.legacy_db, args.dest_db)


if __name__ == "__main__":
    raise SystemExit(main())
