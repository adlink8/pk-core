"""Phase 13.5 Wave 1.2：AgentView 会话导入 inventory（只读 dry-run）。

默认 ``--dry-run``：通过 :mod:`personal_knowledge.adapters.agentsview` 的 backup 快照
读取源库，生成 inventory 报告。**不创建 normalized DB**（那是 Wave 2 的职责）。

报告内容（隐私安全，绝不包含 message content / thinking / 邮箱 /
tool input·result / secret match）：

  - snapshot counts（sessions / messages / tool_calls / tool_result_events /
    usage_events / secret_findings / excluded_sessions）
  - agent 分布、source（agent 字段）分布
  - 缺失 timestamp 的 message 数
  - parent/subagent 关系数
  - secret / excluded / deleted 计数
  - legacy ``agent_data.sqlite`` 的 file_hash 重叠数（lineage 去重预热）

预检 gate（任一失败只写 blocked report）：

  - 源库 ``integrity_check`` 必须为 ``ok``
  - ``sessions`` ↔ ``messages`` 外键孤儿数 = 0
  - ``(session_id, ordinal)`` 重复数 = 0

产物：

  - ``var/reports/analysis/ai_context/agentsview_import_inventory.json``
  - ``var/reports/analysis/ai_context/agentsview_import_inventory.md``

用法::

    python import_agentsview_sessions.py --dry-run
    python import_agentsview_sessions.py --dry-run --no-snapshot   # 直读源(快但非一致)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.core.project_paths import (
    AGENTSVIEW_DB,
    AGENT_DB,
    AI_CONTEXT_DIR,
)
from personal_knowledge.adapters.agentsview import (
    AgentViewAdapter,
    SchemaGateError,
    SnapshotManifest,
)

# 默认 inventory 报告输出目录
DEFAULT_OUT_DIR = AI_CONTEXT_DIR


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open_read_only(path: Path) -> sqlite3.Connection:
    """只读连接（快照或源库都用 mode=ro + query_only）。"""
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    con.row_factory = sqlite3.Row
    return con


def _preflight_checks(con: sqlite3.Connection) -> dict:
    """三项 pre-flight gate：integrity / 外键孤儿 / ordinal 重复。

    返回 ``{"passed": bool, "checks": {...}}``。
    """
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]

    # messages.session_id 指向不存在的 session
    orphan_msgs = con.execute(
        "SELECT COUNT(*) FROM messages m "
        "WHERE m.session_id NOT IN (SELECT id FROM sessions)"
    ).fetchone()[0]

    # (session_id, ordinal) 唯一性
    dup_ordinal = con.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT session_id, ordinal, COUNT(*) c FROM messages "
        "  GROUP BY session_id, ordinal HAVING c > 1)"
    ).fetchone()[0]

    passed = (integrity == "ok" and orphan_msgs == 0 and dup_ordinal == 0)
    return {
        "passed": passed,
        "checks": {
            "integrity_check": integrity,
            "messages_orphan_session_id": orphan_msgs,
            "duplicate_session_ordinal": dup_ordinal,
        },
    }


def _collect_inventory(con: sqlite3.Connection) -> dict:
    """收集隐私安全的 inventory 统计。不含任何正文/邮箱/secret match。"""
    inv: dict = {}

    # 基础计数
    for t in (
        "sessions", "messages", "tool_calls", "tool_result_events",
        "usage_events", "secret_findings", "excluded_sessions",
    ):
        try:
            inv[f"count_{t}"] = con.execute(
                f"SELECT COUNT(*) FROM {t}"
            ).fetchone()[0]
        except sqlite3.Error:
            inv[f"count_{t}"] = -1

    # agent 分布
    inv["agents"] = {
        r["agent"] or "(none)": r["c"]
        for r in con.execute(
            "SELECT agent, COUNT(*) c FROM sessions GROUP BY agent ORDER BY c DESC"
        )
    }

    # 父子/subagent 关系
    inv["parent_child_relations"] = con.execute(
        "SELECT COUNT(*) FROM sessions WHERE parent_session_id IS NOT NULL "
        "AND parent_session_id != ''"
    ).fetchone()[0]
    inv["relationship_types"] = {
        r["relationship_type"] or "(none)": r["c"]
        for r in con.execute(
            "SELECT relationship_type, COUNT(*) c FROM sessions "
            "WHERE parent_session_id IS NOT NULL AND parent_session_id != '' "
            "GROUP BY relationship_type ORDER BY c DESC"
        )
    }

    # 缺失 timestamp 的 message 数
    inv["messages_missing_timestamp"] = con.execute(
        "SELECT COUNT(*) FROM messages "
        "WHERE timestamp IS NULL OR timestamp = ''"
    ).fetchone()[0]

    # role 分布
    inv["message_roles"] = {
        r["role"] or "(none)": r["c"]
        for r in con.execute(
            "SELECT role, COUNT(*) c FROM messages GROUP BY role ORDER BY c DESC"
        )
    }

    # system / sidechain 消息计数（默认排除出用户事实证据）
    inv["messages_is_system"] = con.execute(
        "SELECT COUNT(*) FROM messages WHERE is_system = 1"
    ).fetchone()[0]
    inv["messages_is_sidechain"] = con.execute(
        "SELECT COUNT(*) FROM messages WHERE is_sidechain = 1"
    ).fetchone()[0]

    # 隐私相关计数（不取 match 正文）
    inv["sessions_with_secret_leak"] = con.execute(
        "SELECT COUNT(*) FROM sessions WHERE secret_leak_count > 0"
    ).fetchone()[0]
    inv["distinct_secret_sessions"] = con.execute(
        "SELECT COUNT(DISTINCT session_id) FROM secret_findings"
    ).fetchone()[0]
    inv["secret_rule_counts"] = {
        r["rule_name"] or "(none)": r["c"]
        for r in con.execute(
            "SELECT rule_name, COUNT(*) c FROM secret_findings "
            "GROUP BY rule_name ORDER BY c DESC"
        )
    }
    inv["excluded_session_count"] = con.execute(
        "SELECT COUNT(*) FROM excluded_sessions"
    ).fetchone()[0]
    inv["sessions_deleted"] = con.execute(
        "SELECT COUNT(*) FROM sessions WHERE deleted_at IS NOT NULL "
        "AND deleted_at != ''"
    ).fetchone()[0]

    # file_hash 分布（lineage 去重预热，只取 hash 不取内容）
    inv["sessions_with_file_hash"] = con.execute(
        "SELECT COUNT(*) FROM sessions WHERE file_hash IS NOT NULL "
        "AND file_hash != ''"
    ).fetchone()[0]
    inv["distinct_file_hashes"] = con.execute(
        "SELECT COUNT(DISTINCT file_hash) FROM sessions "
        "WHERE file_hash IS NOT NULL AND file_hash != ''"
    ).fetchone()[0]

    # 时间范围（min/max started_at，不含正文）
    row = con.execute(
        "SELECT MIN(started_at), MAX(started_at) FROM sessions "
        "WHERE started_at IS NOT NULL AND started_at != ''"
    ).fetchone()
    inv["sessions_time_range"] = {
        "min_started_at": row[0],
        "max_started_at": row[1],
    }

    return inv


def _legacy_hash_overlap(legacy_db: Path, snapshot_con: sqlite3.Connection) -> dict:
    """计算 AgentView file_hash 与 legacy agent_data source_files.sha256 重叠。

    legacy 库可能不存在或无 source_files 表，此时返回 ``available=False``。
    """
    if not legacy_db.exists():
        return {"available": False, "reason": "legacy db not found"}
    try:
        lcon = sqlite3.connect(str(legacy_db))
        lcon.row_factory = sqlite3.Row
        try:
            legacy_hashes = {
                r["sha256"]
                for r in lcon.execute(
                    "SELECT DISTINCT sha256 FROM source_files "
                    "WHERE sha256 IS NOT NULL AND sha256 != ''"
                )
            }
        except sqlite3.Error:
            return {"available": False, "reason": "source_files table missing"}
        finally:
            lcon.close()
    except sqlite3.Error as exc:
        return {"available": False, "reason": str(exc)}

    av_hashes = {
        r["file_hash"]
        for r in snapshot_con.execute(
            "SELECT DISTINCT file_hash FROM sessions "
            "WHERE file_hash IS NOT NULL AND file_hash != ''"
        )
    }
    overlap = av_hashes & legacy_hashes
    return {
        "available": True,
        "agentsview_distinct_hashes": len(av_hashes),
        "legacy_distinct_hashes": len(legacy_hashes),
        "overlap": len(overlap),
    }


def _build_report(
    manifest: SnapshotManifest | None,
    preflight: dict,
    inventory: dict,
    legacy_overlap: dict,
    blocked_reason: str | None,
) -> dict:
    return {
        "generated_at": _utc_now_iso(),
        "phase": "13.5",
        "wave": "1.2",
        "status": "blocked" if blocked_reason else "ok",
        "blocked_reason": blocked_reason,
        "manifest": manifest.to_dict() if manifest else None,
        "preflight": preflight,
        "inventory": inventory,
        "legacy_hash_overlap": legacy_overlap,
    }


def _write_report(report: dict, out_dir: Path) -> tuple[Path, Path]:
    """写 JSON + Markdown。返回 (json_path, md_path)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "agentsview_import_inventory.json"
    md_path = out_dir / "agentsview_import_inventory.md"

    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Markdown 人类可读版
    lines: list[str] = []
    lines.append("# AgentView 会话导入 Inventory（Phase 13.5 Wave 1.2）")
    lines.append("")
    lines.append(f"- 生成时间: {report['generated_at']}")
    lines.append(f"- 状态: **{report['status']}**")
    if blocked := report.get("blocked_reason"):
        lines.append(f"- 阻塞原因: {blocked}")
    lines.append("")

    m = report.get("manifest") or {}
    if m:
        lines.append("## Snapshot Manifest")
        lines.append(f"- run_id: `{m.get('run_id')}`")
        lines.append(f"- source: `{m.get('source_path')}`")
        lines.append(f"- user_version: {m.get('source_user_version')}")
        lines.append(f"- integrity: {m.get('source_integrity')}")
        lines.append(f"- schema_hash: `{m.get('schema_hash')}`")
        lines.append("")

    pf = report.get("preflight", {})
    lines.append("## Pre-flight Gate")
    lines.append(f"- passed: **{pf.get('passed')}**")
    for k, v in (pf.get("checks") or {}).items():
        lines.append(f"- {k}: {v}")
    lines.append("")

    inv = report.get("inventory", {})
    lines.append("## Snapshot Counts")
    for key in sorted(k for k in inv if k.startswith("count_")):
        lines.append(f"- {key}: {inv[key]}")
    lines.append("")

    if inv.get("agents"):
        lines.append("## Agent 分布")
        for a, c in inv["agents"].items():
            lines.append(f"- {a}: {c}")
        lines.append("")

    if inv.get("message_roles"):
        lines.append("## Message Role 分布")
        for r, c in inv["message_roles"].items():
            lines.append(f"- {r}: {c}")
        lines.append("")

    lines.append("## 关系与缺失")
    lines.append(f"- parent/child 关系数: {inv.get('parent_child_relations', 'n/a')}")
    if inv.get("relationship_types"):
        for rt, c in inv["relationship_types"].items():
            lines.append(f"  - {rt}: {c}")
    lines.append(f"- 缺 timestamp 的 message: {inv.get('messages_missing_timestamp', 'n/a')}")
    lines.append(f"- system message: {inv.get('messages_is_system', 'n/a')}")
    lines.append(f"- sidechain message: {inv.get('messages_is_sidechain', 'n/a')}")
    lines.append("")

    lines.append("## 隐私与生命周期计数（不含正文）")
    lines.append(f"- secret_leak session 数: {inv.get('sessions_with_secret_leak', 'n/a')}")
    lines.append(f"- distinct secret session: {inv.get('distinct_secret_sessions', 'n/a')}")
    if inv.get("secret_rule_counts"):
        for rn, c in inv["secret_rule_counts"].items():
            lines.append(f"  - rule `{rn}`: {c}")
    lines.append(f"- excluded session: {inv.get('excluded_session_count', 'n/a')}")
    lines.append(f"- deleted session: {inv.get('sessions_deleted', 'n/a')}")
    lines.append("")

    lines.append("## Lineage（file_hash，去重预热）")
    lines.append(f"- 带 file_hash 的 session: {inv.get('sessions_with_file_hash', 'n/a')}")
    lines.append(f"- distinct file_hash: {inv.get('distinct_file_hashes', 'n/a')}")
    ov = report.get("legacy_hash_overlap", {})
    if ov.get("available"):
        lines.append(
            f"- legacy 重叠: {ov.get('overlap')} / "
            f"AgentView {ov.get('agentsview_distinct_hashes')} vs "
            f"legacy {ov.get('legacy_distinct_hashes')}"
        )
    else:
        lines.append(f"- legacy overlap 不可用: {ov.get('reason')}")
    lines.append("")

    tr = inv.get("sessions_time_range") or {}
    if tr:
        lines.append("## 时间范围")
        lines.append(f"- min started_at: {tr.get('min_started_at')}")
        lines.append(f"- max started_at: {tr.get('max_started_at')}")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def run(
    source_db: Path = AGENTSVIEW_DB,
    out_dir: Path = DEFAULT_OUT_DIR,
    use_snapshot: bool = True,
    legacy_db: Path = AGENT_DB,
) -> int:
    """生成 inventory 报告。返回退出码（0=ok, 1=blocked, 2=gate error）。"""
    adapter = AgentViewAdapter(source_db)

    # probe
    probe = adapter.probe()
    if not probe.ok:
        blocked = (
            f"schema gate failed: integrity={probe.integrity_check}, "
            f"missing_tables={probe.required_tables_missing}, "
            f"missing_columns={probe.missing_columns}"
        )
        report = _build_report(None, {"passed": False, "checks": {}}, {}, {},
                                blocked_reason=blocked)
        jp, mp = _write_report(report, out_dir)
        print(f"[blocked] schema gate 失败，已写 blocked report:")
        print(f"  {jp}")
        print(f"  {mp}")
        return 1

    manifest: SnapshotManifest | None = None
    snapshot_path: Path | None = None
    try:
        if use_snapshot:
            try:
                manifest, snapshot_path = adapter.snapshot(probe=probe)
                read_con = _open_read_only(snapshot_path)
            except SchemaGateError as exc:
                report = _build_report(None, {"passed": False, "checks": {}}, {}, {},
                                        blocked_reason=str(exc))
                jp, mp = _write_report(report, out_dir)
                print(f"[blocked] {jp}")
                return 1
        else:
            read_con = _open_read_only(source_db)

        # pre-flight gate
        preflight = _preflight_checks(read_con)
        if not preflight["passed"]:
            report = _build_report(
                manifest, preflight, {}, {},
                blocked_reason=f"preflight gate failed: {preflight['checks']}",
            )
            jp, mp = _write_report(report, out_dir)
            print(f"[blocked] pre-flight gate 失败:")
            print(f"  {jp}")
            print(f"  {mp}")
            return 1

        inventory = _collect_inventory(read_con)
        legacy_overlap = _legacy_hash_overlap(legacy_db, read_con)
        report = _build_report(manifest, preflight, inventory, legacy_overlap,
                                blocked_reason=None)
        jp, mp = _write_report(report, out_dir)
        print(f"[ok] inventory 报告已生成:")
        print(f"  {jp}")
        print(f"  {mp}")
        print(f"  sessions={inventory.get('count_sessions')}, "
              f"messages={inventory.get('count_messages')}, "
              f"secret_sessions={inventory.get('sessions_with_secret_leak')}")
        return 0
    finally:
        try:
            read_con.close()
        except Exception:
            pass
        # 清理自己创建的临时快照
        if snapshot_path is not None and snapshot_path.exists():
            try:
                snapshot_path.unlink()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="AgentView 会话导入 inventory（Phase 13.5 Wave 1.2，默认 dry-run）"
    )
    p.add_argument("--dry-run", action="store_true", default=True,
                    help="只生成 inventory 报告，不创建 normalized DB（默认）")
    p.add_argument("--no-snapshot", action="store_true",
                    help="直读源库（快但不保证跨查询一致性），默认用 backup 快照")
    p.add_argument("--source", type=Path, default=AGENTSVIEW_DB,
                    help="源库路径（默认 ~/.agentsview/sessions.db）")
    p.add_argument("--legacy", type=Path, default=AGENT_DB,
                    help="legacy agent_data.sqlite 路径")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help="报告输出目录")
    args = p.parse_args(argv)
    return run(
        source_db=args.source,
        out_dir=args.out_dir,
        use_snapshot=not args.no_snapshot,
        legacy_db=args.legacy,
    )


if __name__ == "__main__":
    raise SystemExit(main())
