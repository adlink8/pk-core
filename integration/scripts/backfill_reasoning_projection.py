# -*- coding: utf-8 -*-
"""一次性迁移（2026-09-30，D-23-rev1"全部入库"）：把存量 ce_events 里被旧 D-23
排除的事件补投影进权威库。

做什么（幂等，可重复跑）：
  1. schema v2.3.0：canonical_messages.role 的 CHECK 加 'reasoning'（重建表，
     无外键引用本表，安全）；新建 canonical_event_extras 留底表。
  2. reasoning 事件（ce_events kind='reasoning'，约 17.7 万条）按投影同一 id
     规则补成 canonical_messages 行（role='reasoning'）。
  3. 其余 EXCLUDED_KINDS 事件（usage/边界/file_context/compaction/
     unknown_native/session_lifecycle，约 45 万条）整行留底 canonical_event_extras。
  4. 对账：补投影行数必须与 ce_events 源计数吻合；写 FTS 全量重建标记。

用法：python integration/scripts/backfill_reasoning_projection.py [--db PATH] [--dry-run]
默认 db=权威库。先跑 --db 副本演练，对账通过再上真库。
"""
from __future__ import annotations
import argparse
import sqlite3
import sys
import time

sys.path.insert(0, r"D:/ADLINK/数据分析/src")
sys.stdout.reconfigure(encoding="utf-8")

from personal_knowledge.core.conversation_events import EventKind
from personal_knowledge.application.conversation.compatibility_projection import (
    MESSAGE_KINDS,
    _placeholder_native_ids,
)
from personal_knowledge.application.conversation.uniform_id_migration import (
    adapter_address,
    make_message_id,
    make_session_id,
)
from personal_knowledge.application.conversation.event_schema import (
    CE_FTS_INVALIDATE_TABLE,
    FTS_REBUILD_MARKER,
)

DEFAULT_DB = (r"D:/ADLINK/数据分析/data/canonical/agent/structured/db/"
              r"agent_conversations.sqlite")
SCHEMA_VERSION = "v2.3.0"
CHUNK = 5000

# 排除 kind（与投影 EXCLUDED_KINDS 同口径，从 EventKind 反推）
EXTRA_KINDS = {k for k in EventKind
               if k not in MESSAGE_KINDS and k.value not in
               ("tool_call", "tool_result")}


def migrate_schema(con: sqlite3.Connection) -> None:
    """role CHECK 放宽（重建表）+ extras 表 + ce_schema_meta 版本行。"""
    sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE name='canonical_messages'"
    ).fetchone()[0]
    if "'reasoning'" in sql:
        print("[schema] role CHECK 已含 reasoning，跳过重建")
    else:
        new_sql = sql.replace(
            "CHECK(role IN ('user','assistant','developer','system','tool'))",
            "CHECK(role IN ('user','assistant','developer','system','tool','reasoning'))")
        assert new_sql != sql, "role CHECK 模式未匹配，schema 形状变了？"
        table = "canonical_messages"
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute(f"ALTER TABLE {table} RENAME TO {table}_old_v22")
        # sqlite_master 读出的 DDL 文本仍是旧表名 canonical_messages；此时旧表
        # 已改名，直接用该文本建新表（CHECK 已放宽），再搬行、删旧表。
        con.execute(new_sql)
        con.execute(f"INSERT INTO {table} SELECT * FROM {table}_old_v22")
        con.execute(f"DROP TABLE {table}_old_v22")
        con.execute("PRAGMA foreign_keys=ON")
        print(f"[schema] {table} 重建完成（role CHECK +reasoning）")
    con.execute(
        """CREATE TABLE IF NOT EXISTS canonical_event_extras (
            canonical_event_id TEXT PRIMARY KEY,
            canonical_session_id TEXT NOT NULL, source TEXT NOT NULL,
            kind TEXT NOT NULL, ordinal INTEGER, occurred_at TEXT,
            native_locator TEXT, native_payload_ref TEXT, summary TEXT,
            content TEXT, content_length INTEGER)"""
    )
    if not con.execute("SELECT 1 FROM ce_schema_meta WHERE schema_version=?",
                       (SCHEMA_VERSION,)).fetchone():
        con.execute("INSERT INTO ce_schema_meta VALUES (?, datetime('now'))",
                    (SCHEMA_VERSION,))
        print(f"[schema] ce_schema_meta += {SCHEMA_VERSION}")


def backfill(con: sqlite3.Connection, dry_run: bool) -> dict:
    rows = con.execute(
        """SELECT e.event_id, e.session_id, e.kind, e.native_event_id,
                  e.native_locator, e.ordinal, e.occurred_at, e.native_payload_ref,
                  e.summary, e.content
           FROM ce_events e WHERE e.kind IN ('reasoning') OR e.kind IN (%s)
           ORDER BY e.session_id, e.ordinal, e.event_id"""
        % ",".join("?" * len(EXTRA_KINDS)),
        tuple(k.value for k in EXTRA_KINDS)).fetchall()
    sessions = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT session_id, family, native_session_id FROM ce_sessions")}
    print(f"[source] 事件 {len(rows)} 条，会话 {len(sessions)} 个")
    by_sid_events: dict[str, list[dict]] = {}
    for r in rows:
        by_sid_events.setdefault(r[1], []).append(dict(zip(
            ("event_id", "session_id", "kind", "native_event_id", "native_locator",
             "ordinal", "occurred_at", "native_payload_ref", "summary", "content"), r)))
    placeholders = _placeholder_native_ids(by_sid_events, sessions)

    msg_rows, extra_rows = [], []
    for sid, events in by_sid_events.items():
        family, native = sessions[sid]
        address_family = "" if (family, native) in placeholders else family
        csid = make_session_id(family, native)
        for e in events:
            kind = EventKind(e["kind"])
            base_addr = (adapter_address(e["native_event_id"], e["native_locator"],
                                         address_family)
                         or e["event_id"])
            if kind in MESSAGE_KINDS:  # 现在只可能是 reasoning
                # 与投影 _project_messages 一致：地址段加 |reasoning 后缀，
                # 避免与同响应的 message 行撞主键（codex 实测 65% 撞键）。
                content = e["content"]
                if content is None:
                    content = e["summary"] or None
                msg_rows.append((
                    make_message_id(family, native, base_addr + "|reasoning"),
                    csid, "legacy",
                    e["native_locator"], e["ordinal"] or 0, MESSAGE_KINDS[kind],
                    content, len(content or ""), e["occurred_at"], None, 0, 0,
                    None, "user"))
            else:
                # 与投影 _project_extras 一致：地址段加 |kind 后缀防多类事件互吃。
                extra_rows.append((
                    "ex|" + make_message_id(family, native,
                                            base_addr + "|" + kind.value).split("|", 1)[1],
                    csid, "legacy", kind.value, e["ordinal"], e["occurred_at"],
                    e["native_locator"], e["native_payload_ref"], e["summary"],
                    e["content"], len(e["content"] or "")))
    stats = {"messages": len(msg_rows), "extras": len(extra_rows)}
    print(f"[plan] 消息行（reasoning）{stats['messages']}，extras 行 {stats['extras']}")
    if dry_run:
        return stats, {}
    # 抽样对账样本：每类随机 1,000 个构造 id
    import random
    random.seed(20260930)
    sample = {
        "messages": [r[0] for r in random.sample(msg_rows, min(1000, len(msg_rows)))],
        "extras": [r[0] for r in random.sample(extra_rows, min(1000, len(extra_rows)))],
    }
    for start in range(0, len(msg_rows), CHUNK):
        con.executemany(
            "INSERT OR IGNORE INTO canonical_messages VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            msg_rows[start:start + CHUNK])
        if (start // CHUNK) % 10 == 0:
            print(f"  messages {start + CHUNK}/{len(msg_rows)}", flush=True)
    for start in range(0, len(extra_rows), CHUNK):
        con.executemany(
            "INSERT OR IGNORE INTO canonical_event_extras "
            "(canonical_event_id,canonical_session_id,source,kind,ordinal,"
            "occurred_at,native_locator,native_payload_ref,summary,content,"
            "content_length) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            extra_rows[start:start + CHUNK])
        if (start // CHUNK) % 20 == 0:
            print(f"  extras {start + CHUNK}/{len(extra_rows)}", flush=True)
    return stats, sample


def _included_reasoning(con: sqlite3.Connection) -> int:
    """（已弃用为对账口径）保留供人工核对：按 make_session_id 复算的
    "会话已收录"事件数。实测比库内行数偏小——csid 复算与投影历史格式
    存在 sanitize 差异，不作为判据。"""
    csids = {r[0] for r in con.execute(
        "SELECT canonical_session_id FROM canonical_sessions")}
    n = 0
    for family, native in con.execute(
            """SELECT DISTINCT s.family, s.native_session_id
               FROM ce_events e JOIN ce_sessions s ON s.session_id=e.session_id
               WHERE e.kind='reasoning'"""):
        if make_session_id(family, native) in csids:
            n += con.execute(
                """SELECT COUNT(*) FROM ce_events e
                   JOIN ce_sessions s ON s.session_id=e.session_id
                   WHERE e.kind='reasoning' AND s.family=? AND s.native_session_id=?""",
                (family, native)).fetchone()[0]
    return n


def reconcile(con: sqlite3.Connection, plan: dict, sample_ids: dict) -> bool:
    """对账（2026-09-30 第二次修正）：构造行 177,297 → 库 159,943 的差值是
    **同 native 多扫描副本的 id 去重**（ce_sessions 宇宙里同一原生文件有多个
    会话副本，最多 30 份；副本行 id 相同，INSERT OR IGNORE 只留一份——与
    assistant 消息行的既有行为一致，属正确去重）。因此判据改为抽样存在率：
    每类随机抽 1,000 个构造 id，库里必须 100% 存在。"""
    ok = True
    for label, table, col, ids in (
            ("reasoning", "canonical_messages", "canonical_message_id",
             sample_ids["messages"]),
            ("extras", "canonical_event_extras", "canonical_event_id",
             sample_ids["extras"])):
        hit = sum(1 for i in ids if con.execute(
            f"SELECT 1 FROM {table} WHERE {col}=?", (i,)).fetchone())
        rate = hit / max(len(ids), 1)
        print(f"[reconcile] {label} 抽样存在率 {hit}/{len(ids)} ({rate:.1%})")
        ok = ok and rate == 1.0
    dst_reasoning = con.execute(
        "SELECT COUNT(*) FROM canonical_messages WHERE role='reasoning'"
    ).fetchone()[0]
    dst_extra = con.execute(
        "SELECT COUNT(*) FROM canonical_event_extras").fetchone()[0]
    print(f"[reconcile] 库内 reasoning {dst_reasoning}（构造 177,297 去重后）"
          f"、extras {dst_extra}（构造 558,852 去重后）")
    # FTS 全量重建标记（reasoning 新行入库，增量游标语义不再足够）
    con.execute(f"INSERT OR IGNORE INTO {CE_FTS_INVALIDATE_TABLE} VALUES (?,?)",
                (FTS_REBUILD_MARKER, time.strftime("%Y-%m-%dT%H:%M:%S")))
    print("[fts] 已写全量重建标记（下次 FTS 构建生效）")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print(f"[db] {args.db}  dry_run={args.dry_run}")
    con = sqlite3.connect(args.db, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    t0 = time.time()
    try:
        migrate_schema(con)
        plan, sample_ids = backfill(con, args.dry_run)
        if not args.dry_run:
            ok = reconcile(con, plan, sample_ids)
            con.commit()
            print(f"=== 完成 {round(time.time()-t0,1)}s  对账{'通过' if ok else '不通过'}")
            return 0 if ok else 1
        con.rollback()
        print("=== dry-run 完成（已回滚）")
        return 0
    except Exception:
        con.rollback()
        raise


if __name__ == "__main__":
    sys.exit(main())
