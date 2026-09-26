# -*- coding: utf-8 -*-
"""P4 清理（2026-09-26，用户授权）：canonical 内容去重 + shadow 冻结世代删除。

两项目标，全部单事务、全量计数、报告落盘：

1. canonical_messages 内容去重——同 (canonical_session_id, content_hash) 的
   多行只留一份。content_hash 相同即同正文；保留优先级：原生地址（uuid，非
   av/非 #dup）> av 地址 > #dup 变体；同级取 content_length 最大、ordinal
   最小（确定性）。保留行的 role/时间戳随保留行走（原生行的分类经过
   P1-10 修复，优先于 av 时代行）。

2. ce 层 shadow-cohort-0716eb642516 冻结世代删除——旧双轨时代 staged 而从未
   激活的世代（112 万事件），用户 2026-09-25/26 明确授权。按 FK 顺序清空
   全部 generation-scoped 表后删除世代行本身。

明确不删（差分守卫否决）：
* cs|legacy| 孤儿会话 1,224 个——实测其内容在 live ce 层零覆盖，
  是本系统内唯一副本，等原生采集覆盖后自然让位或作为遗产保留。

运行前提：已做全库备份。运行方式：
    python ops/p4_cleanup_20260926.py --db <authority.sqlite> --apply
    （不带 --apply 只出报告不写）
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SHADOW_GENERATION = "shadow-cohort-0716eb642516"

# generation-scoped 表的删除顺序（按 PRAGMA foreign_key_list 实测的依赖图：
# delta_events→deltas→generations，relations/dispositions→events→sessions→
# generations；版本表无外键随时可删）。
SHADOW_TABLES = (
    "ce_generation_delta_events",
    "ce_generation_deltas",
    "ce_adapter_runs",
    "ce_activation_bindings",
    "ce_activation_log",
    "ce_generation_authority",
    "ce_event_versions",
    "ce_session_versions",
    "ce_relation_versions",
    "ce_disposition_versions",
    "ce_ingest_quarantine",
    "ce_event_relations",
    "ce_field_dispositions",
    "ce_events",
    "ce_sessions",
    "ce_event_generations",
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def dedup_messages(con: sqlite3.Connection) -> dict:
    """同 (session, content_hash) 只留一份，原生优先，返回计数与明细。"""
    groups = con.execute(
        "SELECT canonical_session_id, content_hash, COUNT(*) c "
        "FROM canonical_messages WHERE content_hash IS NOT NULL "
        "GROUP BY canonical_session_id, content_hash HAVING c > 1"
    ).fetchall()

    kept_native = kept_av = kept_dup = deleted = 0
    by_family: dict[str, int] = {}
    for session_id, _hash, _c in groups:
        rows = con.execute(
            "SELECT m.canonical_message_id, m.ordinal, m.content_length, "
            "s.canonical_session_id IS NOT NULL AS sid_exists "
            "FROM canonical_messages m "
            "LEFT JOIN canonical_sessions s "
            " ON s.canonical_session_id = m.canonical_session_id "
            "WHERE m.canonical_session_id=? AND m.content_hash=?",
            (session_id, _hash),
        ).fetchall()
        if len(rows) < 2:
            continue  # 并发护栏：分组与删除间状态不应变化，防御性跳过

        def rank(row) -> tuple:
            mid = row[0]
            # 地址层级：原生 uuid(0) > av(1) > #dup 变体(2)
            if "#dup-" in mid:
                tier = 2
            elif "|av" in mid:
                tier = 1
            else:
                tier = 0
            return (tier, -(row[2] or 0), row[1] or 0, mid)

        rows.sort(key=rank)
        keep_id = rows[0][0]
        if keep_id.startswith(("cs|", "cm|")) or "#dup-" not in keep_id:
            if "|av" not in keep_id:
                kept_native += 1
            else:
                kept_av += 1
        else:
            kept_dup += 1
        family = session_id.split("|")[1] if "|" in session_id else "?"
        for row in rows[1:]:
            con.execute(
                "DELETE FROM canonical_messages WHERE canonical_message_id=?",
                (row[0],),
            )
            deleted += 1
            by_family[family] = by_family.get(family, 0) + 1
    return {
        "duplicate_groups": len(groups),
        "rows_deleted": deleted,
        "kept_native_address": kept_native,
        "kept_av_address": kept_av,
        "by_family": by_family,
    }


def delete_shadow_generation(con: sqlite3.Connection) -> dict:
    """删除 shadow 冻结世代的全部 generation-scoped 行。"""
    counts: dict[str, int] = {}
    for table in SHADOW_TABLES:
        if table == "ce_generation_delta_events":
            # 该表无 generation_id 列，经 delta_id 间接归属世代。
            cur = con.execute(
                "DELETE FROM ce_generation_delta_events WHERE delta_id IN "
                "(SELECT delta_id FROM ce_generation_deltas WHERE generation_id=?)",
                (SHADOW_GENERATION,),
            )
        else:
            cur = con.execute(
                f"DELETE FROM {table} WHERE generation_id=?",
                (SHADOW_GENERATION,),
            )
        counts[table] = max(cur.rowcount, 0)
    return counts


def verify(con: sqlite3.Connection) -> list[str]:
    problems: list[str] = []
    if con.execute(
        "SELECT COUNT(*) FROM ce_events WHERE generation_id=?",
        (SHADOW_GENERATION,),
    ).fetchone()[0]:
        problems.append("shadow events remain")
    # 引用完整性：消息不得指向不存在的会话
    orphans = con.execute(
        "SELECT COUNT(*) FROM canonical_messages m WHERE NOT EXISTS "
        "(SELECT 1 FROM canonical_sessions s WHERE s.canonical_session_id "
        "= m.canonical_session_id)"
    ).fetchone()[0]
    if orphans:
        problems.append(f"{orphans} orphan canonical_messages")
    # 去重后同 session+hash 不应再有重复
    dup = con.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM canonical_messages "
        "WHERE content_hash IS NOT NULL GROUP BY canonical_session_id, "
        "content_hash HAVING COUNT(*)>1)"
    ).fetchone()[0]
    if dup:
        problems.append(f"{dup} duplicate groups remain")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    con = sqlite3.connect(args.db, timeout=60)
    con.execute("PRAGMA foreign_keys=ON")
    try:
        before = {
            "canonical_messages": con.execute(
                "SELECT COUNT(*) FROM canonical_messages").fetchone()[0],
            "canonical_sessions": con.execute(
                "SELECT COUNT(*) FROM canonical_sessions").fetchone()[0],
            "ce_events": con.execute(
                "SELECT COUNT(*) FROM ce_events").fetchone()[0],
            "ce_sessions": con.execute(
                "SELECT COUNT(*) FROM ce_sessions").fetchone()[0],
        }
        report: dict = {
            "started_at": _now(),
            "db": str(args.db),
            "apply": bool(args.apply),
            "before": before,
            "shadow_generation": SHADOW_GENERATION,
        }
        if args.apply:
            con.execute("BEGIN IMMEDIATE")
            try:
                report["dedup"] = dedup_messages(con)
                report["shadow_deleted"] = delete_shadow_generation(con)
                con.commit()
            except Exception:
                con.rollback()
                raise
            after = {
                "canonical_messages": con.execute(
                    "SELECT COUNT(*) FROM canonical_messages").fetchone()[0],
                "canonical_sessions": con.execute(
                    "SELECT COUNT(*) FROM canonical_sessions").fetchone()[0],
                "ce_events": con.execute(
                    "SELECT COUNT(*) FROM ce_events").fetchone()[0],
                "ce_sessions": con.execute(
                    "SELECT COUNT(*) FROM ce_sessions").fetchone()[0],
            }
            report["after"] = after
            report["verify_problems"] = verify(con)
        else:
            dup_groups = con.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM canonical_messages "
                "WHERE content_hash IS NOT NULL GROUP BY canonical_session_id, "
                "content_hash HAVING COUNT(*)>1)"
            ).fetchone()[0]
            report["dry_run"] = {"duplicate_groups": dup_groups}
        report["finished_at"] = _now()
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        print(payload)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(payload, encoding="utf-8")
        if args.apply and report.get("verify_problems"):
            print("VERIFY FAILED", file=__import__("sys").stderr)
            return 2
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
