"""Phase 41 Plan 03：source × role × pass 覆盖矩阵（D-06，EXT-02）。

每个 (source, role) 组合报告：
- eligible_count：41-01 ``compute_eligible_messages`` 唯一口径的分母；
- covered_count / grandfathered_count：已单元化分子（证据并集口径——
  ``knowledge_unit_evidence`` ∪ ``knowledge_units.source_message_ref``，
  按 evidence_ref 去重；salvage 后 unit 合法持多 ref，必须用并集）。
  ``ku|`` 世代命中标 grandfathered（D-04 豁免），不计入未覆盖；
- 未覆盖三分类：abstained / terminal_failed / not_queued
  （对 eligible ∖ 已单元化的 ref 集，取该 ref 最新 run item status；
  从未出现在任何 run items → not_queued）。

守恒：covered + grandfathered + abstained + terminal_failed + not_queued
== eligible_count（对每行成立）。

行级分级（快照比对）：
- previous_snapshot 无该 source → level='info'（新 source 首现）；
- 有历史且该 (source, role) 上次与本次已单元化均为 0 → level='warn'
  （连续零覆盖）；
- 其余 level='ok'。

隐私安全：返回 dict 全 count/hash-only，不含任何消息原文或
evidence_ref 清单；快照文件可直接落盘。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.application.knowledge.eligibility import (
    compute_eligible_messages,
)
from personal_knowledge.core.project_paths import (
    AGENT_CONVERSATIONS_DB,
    UNIFIED_DB,
)

# ku 世代（D-04 grandfather 豁免的 pass 族前缀）。
# 实测存量前缀为 ``ku_``（14,928 条，unit_id 形如 ku_0000299c…），
# ``ku|`` 一并列入以防历史/未来行混用。
GRANDFATHERED_PASS_FAMILIES = frozenset({"ku_", "ku|"})

# pass 族口径与 StagingPublisher 一致：substr(unit_id, 1, 3)
_PASS_FAMILY_SQL = "substr(unit_id, 1, 3)"


def _table_names(con: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _load_coverage_families(con: sqlite3.Connection) -> dict[str, set[str]]:
    """evidence_ref -> 覆盖它的 pass 族集合（证据并集口径）。"""
    tables = _table_names(con)
    queries: list[str] = []
    if {"knowledge_unit_evidence", "knowledge_units"} <= tables:
        queries.append(
            "SELECT e.evidence_ref, " + _PASS_FAMILY_SQL.replace("unit_id", "u.unit_id") + " "
            "FROM knowledge_unit_evidence e "
            "JOIN knowledge_units u ON u.unit_id = e.unit_id"
        )
    if "knowledge_units" in tables:
        queries.append(
            f"SELECT source_message_ref, {_PASS_FAMILY_SQL} FROM knowledge_units "
            "WHERE source_message_ref IS NOT NULL AND source_message_ref != ''"
        )
    families: dict[str, set[str]] = {}
    for sql in queries:
        for ref, family in con.execute(sql):
            families.setdefault(str(ref), set()).add(str(family))
    return families


def _load_latest_run_status(con: sqlite3.Connection) -> dict[str, str]:
    """evidence_ref -> 最新 run item status（按自增 id 取最新）。"""
    if "knowledge_run_items" not in _table_names(con):
        return {}
    rows = con.execute(
        "SELECT evidence_ref, status FROM knowledge_run_items "
        "WHERE id IN (SELECT MAX(id) FROM knowledge_run_items GROUP BY evidence_ref)"
    ).fetchall()
    return {str(ref): str(status) for ref, status in rows}


def _load_dead_refs(con: sqlite3.Connection) -> set[str]:
    """knowledge_dead_refs 已记录的 evidence_ref（表不存在时按空集——
    此时 terminal_failed 全部计入 dead_ref_missing，失败不静默）。"""
    if "knowledge_dead_refs" not in _table_names(con):
        return set()
    return {
        str(row[0])
        for row in con.execute("SELECT DISTINCT evidence_ref FROM knowledge_dead_refs")
    }


def compute_coverage_matrix(
    unified_db: Path = UNIFIED_DB,
    canonical_db: Path = AGENT_CONVERSATIONS_DB,
    *,
    previous_snapshot: dict | None = None,
) -> dict:
    """计算覆盖矩阵。返回 count/hash-only 的 dict（可直接 JSON 落盘）。"""
    items, stats = compute_eligible_messages(canonical_db)
    eligible_by_row: dict[tuple[str, str], list[str]] = {}
    for item in items:
        eligible_by_row.setdefault((item.source, item.role), []).append(
            item.evidence_ref
        )

    families: dict[str, set[str]] = {}
    latest_status: dict[str, str] = {}
    dead_refs: set[str] = set()
    if Path(unified_db).exists():
        con = sqlite3.connect(
            f"file:{Path(unified_db).resolve().as_posix()}?mode=ro", uri=True
        )
        try:
            families = _load_coverage_families(con)
            latest_status = _load_latest_run_status(con)
            dead_refs = _load_dead_refs(con)
        finally:
            con.close()

    prev_rows: dict[tuple[str, str], dict] = {}
    if previous_snapshot:
        for row in previous_snapshot.get("rows") or []:
            prev_rows[(str(row.get("source")), str(row.get("role")))] = row
    prev_sources = {source for source, _role in prev_rows}

    rows: list[dict] = []
    for (source, role), refs in sorted(eligible_by_row.items()):
        eligible_count = len(refs)
        covered = 0
        grandfathered = 0
        abstained = 0
        terminal_failed = 0
        not_queued = 0
        dead_ref_missing = 0
        by_pass: dict[str, int] = {}
        for ref in refs:
            fams = families.get(ref)
            if fams:
                for fam in sorted(fams):
                    by_pass[fam] = by_pass.get(fam, 0) + 1
                if fams - GRANDFATHERED_PASS_FAMILIES:
                    covered += 1
                else:
                    # 仅 ku 世代命中：D-04 grandfather 豁免，不计未覆盖
                    grandfathered += 1
                continue
            status = latest_status.get(ref)
            if status == "abstained":
                abstained += 1
            elif status == "terminal_failed":
                terminal_failed += 1
                if ref not in dead_refs:
                    dead_ref_missing += 1
            else:
                not_queued += 1

        unitized = covered + grandfathered
        if source not in prev_sources:
            level = "info"  # 新 source 首现
        else:
            prev = prev_rows.get((source, role))
            prev_unitized = 0
            if prev is not None:
                prev_unitized = int(prev.get("covered_count") or 0) + int(
                    prev.get("grandfathered_count") or 0
                )
            if prev_unitized == 0 and unitized == 0:
                level = "warn"  # 已知 source 连续零覆盖
            else:
                level = "ok"

        rows.append(
            {
                "source": source,
                "role": role,
                "eligible_count": eligible_count,
                "covered_count": covered,
                "grandfathered_count": grandfathered,
                "abstained_count": abstained,
                "terminal_failed_count": terminal_failed,
                "not_queued_count": not_queued,
                "dead_ref_missing_count": dead_ref_missing,
                "by_pass": by_pass,
                "level": level,
            }
        )

    totals: dict[str, int] = {"rows": len(rows)}
    for key in (
        "eligible_count",
        "covered_count",
        "grandfathered_count",
        "abstained_count",
        "terminal_failed_count",
        "not_queued_count",
        "dead_ref_missing_count",
    ):
        totals[key] = sum(int(row[key]) for row in rows)
    totals["warn_rows"] = sum(1 for row in rows if row["level"] == "warn")
    totals["info_rows"] = sum(1 for row in rows if row["level"] == "info")

    return {
        "generated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "source_checksum": str(stats.get("source_checksum") or ""),
        "rows": rows,
        "totals": totals,
    }
