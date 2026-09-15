"""Phase 14 Plan 02 Task 1：冻结 production inventory。

从 canonical conversation store 生成权威有序 inventory，记录每条 evidence 的
evidence_ref、content_hash、session/source/agent/time bucket、eligibility、position。
inventory hash 由完整有序 content hash 派生，用于 resume 时 drift 检测。

输出隐私安全报告（只含 count/hash，不含原文）。

用法::

    python build_knowledge_inventory.py --inspect
    python build_knowledge_inventory.py --write
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import UNIFIED_DB, AGENT_CONVERSATIONS_DB, AI_CONTEXT_DIR  # noqa: E402
from personal_knowledge.core.sqlite import connect_rw  # noqa: E402
from personal_knowledge.application.knowledge.eligibility import (  # noqa: E402
    SYSTEM_INJECTION_PATTERNS,  # noqa: F401  (re-export，兼容旧 import 路径)
    strip_system_injections,
    compute_content_hash,
    compute_source_checksum,
    compute_eligible_messages,
)

INVENTORY_JSON = AI_CONTEXT_DIR / "knowledge_unit_inventory.json"
INVENTORY_MD = AI_CONTEXT_DIR / "knowledge_unit_inventory.md"

# 旧名字保留为 eligibility 实现的别名（兼容既有 import 路径）
_strip_injections = strip_system_injections
_content_hash = compute_content_hash
_source_checksum = compute_source_checksum


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_inventory(canonical_db: Path = AGENT_CONVERSATIONS_DB) -> dict:
    """构建冻结 inventory（不写 DB，只返回数据结构）。

    eligible 判定收编到 eligibility.compute_eligible_messages（D-05 唯一口径），
    本函数只做 time/length 分桶与统计汇总。
    """
    if not canonical_db.exists():
        return {"error": f"canonical DB 不存在: {canonical_db}"}

    eligible_items, estats = compute_eligible_messages(canonical_db)
    source_checksum = estats["source_checksum"]
    coarse_count = estats["coarse_count"]
    cleaned_len = estats["cleaned_len"]

    items: list[dict] = []
    for msg in eligible_items:
        started = msg.started_at
        time_bucket = started[:7] if started else "unknown"
        clen = cleaned_len[msg.evidence_ref]
        if clen < 100:
            length_bucket = "short"
        elif clen < 500:
            length_bucket = "mid"
        else:
            length_bucket = "long"

        items.append({
            "evidence_ref": msg.evidence_ref,
            "content_hash": msg.content_hash,
            "role": msg.role,
            "session_id": msg.session_id,
            "source": msg.source,
            "agent": msg.agent,
            "time_bucket": time_bucket,
            "length_bucket": length_bucket,
            "has_injection": int(msg.has_injection),
            "eligibility": "eligible",
        })

    # 有序 dataset hash（Merkle-like：所有 content_hash 的有序拼接）
    dataset_hash = estats["dataset_hash"]
    inventory_id = estats["inventory_id"]

    # 时间范围
    time_min = min((item["time_bucket"] for item in items if item["time_bucket"] != "unknown"), default="")
    time_max = max((item["time_bucket"] for item in items if item["time_bucket"] != "unknown"), default="")

    # 统计（隐私安全，无原文）
    by_agent: dict[str, int] = {}
    by_length: dict[str, int] = {"short": 0, "mid": 0, "long": 0}
    by_source: dict[str, int] = {}
    injection_count = 0
    for item in items:
        by_agent[item["agent"]] = by_agent.get(item["agent"], 0) + 1
        by_length[item["length_bucket"]] += 1
        by_source[item["source"]] = by_source.get(item["source"], 0) + 1
        injection_count += item["has_injection"]

    return {
        "inventory_id": inventory_id,
        "source_db_path": str(canonical_db),
        "source_checksum": source_checksum,
        "coarse_count": coarse_count,
        "authoritative_count": len(items),
        "dataset_hash": dataset_hash,
        "time_range_min": time_min,
        "time_range_max": time_max,
        "excluded": {
            "short_after_cleaning": estats["excluded_short"],
            "duplicate_content_hash": estats["excluded_dup"],
            "injection_only": estats["excluded_injection_only"],
            "excluded_tool": estats["excluded_tool"],
        },
        "stats": {
            "by_agent": dict(sorted(by_agent.items(), key=lambda x: -x[1])),
            "by_length": by_length,
            "by_source": by_source,
            "with_injection": injection_count,
        },
        "items": items,
    }


def write_inventory_to_db(inventory: dict, db_path: Path = UNIFIED_DB) -> None:
    """把 inventory 写入 knowledge_inventory + knowledge_inventory_items 表。

    knowledge_inventory_items 需要 role 列（Phase 41 迁移）；写入前 PRAGMA
    探测，缺列直接抛错提示跑迁移——fail closed，不静默丢 role。
    """
    con = connect_rw(db_path)
    try:
        item_cols = {
            r[1] for r in con.execute("PRAGMA table_info(knowledge_inventory_items)")
        }
        if "role" not in item_cols:
            raise RuntimeError(
                "knowledge_inventory_items 缺少 role 列，请先运行迁移："
                "python tools/migrations/add_inventory_items_role_column.py --write"
            )
        con.execute(
            "INSERT OR REPLACE INTO knowledge_inventory VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                inventory["inventory_id"],
                _utc_now(),
                inventory["source_db_path"],
                inventory["source_checksum"],
                inventory["authoritative_count"],
                inventory["coarse_count"],
                inventory["dataset_hash"],
                inventory["time_range_min"],
                inventory["time_range_max"],
                json.dumps({
                    "excluded": inventory["excluded"],
                    "stats": inventory["stats"],
                }, ensure_ascii=False),
            ),
        )
        for pos, item in enumerate(inventory["items"]):
            con.execute(
                "INSERT OR REPLACE INTO knowledge_inventory_items VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    None,  # autoincrement
                    inventory["inventory_id"],
                    pos,
                    item["evidence_ref"],
                    item["content_hash"],
                    item["session_id"],
                    item["source"],
                    item["agent"],
                    item["time_bucket"],
                    item["length_bucket"],
                    item["has_injection"],
                    item["eligibility"],
                    item["role"],
                ),
            )
        con.commit()
    finally:
        con.close()


def write_report(inventory: dict, json_path: Path = INVENTORY_JSON, md_path: Path = INVENTORY_MD) -> None:
    """写隐私安全报告（不含原文）。"""
    json_path.parent.mkdir(parents=True, exist_ok=True)

    # JSON：不含 items 的 evidence_ref 映射到原文（只含 hash）
    report = {k: v for k, v in inventory.items() if k != "items"}
    report["item_count"] = len(inventory["items"])
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # Markdown
    lines = [
        "# Knowledge Unit Production Inventory",
        "",
        f"- 生成时间: {_utc_now()}",
        f"- inventory_id: `{inventory['inventory_id']}`",
        f"- source: `{inventory['source_db_path']}`",
        f"- source_checksum: `{inventory['source_checksum']}`",
        f"- dataset_hash: `{inventory['dataset_hash']}`",
        "",
        "## Count 解释",
        "",
        f"| 阶段 | Count |",
        f"|------|-------|",
        f"| SQL 粗筛 (eligible user+assistant >20字) | {inventory['coarse_count']} |",
        f"| 清洗去重后 (authoritative) | **{inventory['authoritative_count']}** |",
        f"| 排除-清洗后过短 | {inventory['excluded']['short_after_cleaning']} |",
        f"| 排除-content_hash 重复 | {inventory['excluded']['duplicate_content_hash']} |",
        f"| 排除-仅注入内容 | {inventory['excluded']['injection_only']} |",
        f"| 排除-assistant 工具前缀 | {inventory['excluded']['excluded_tool']} |",
        "",
        "> 报告不含 message content、evidence quote、token 或完整 prompt。",
        "",
        "## 分布统计",
        "",
        "### 按 agent",
        "",
        "| Agent | Count |",
        "|-------|-------|",
    ]
    for agent, count in inventory["stats"]["by_agent"].items():
        lines.append(f"| {agent} | {count} |")
    lines += [
        "",
        "### 按长度",
        "",
        "| Bucket | Count |",
        "|--------|-------|",
    ]
    for bucket, count in inventory["stats"]["by_length"].items():
        lines.append(f"| {bucket} | {count} |")
    lines += [
        "",
        f"### 含系统注入: {inventory['stats']['with_injection']}",
        "",
        f"### 时间范围: {inventory['time_range_min']} → {inventory['time_range_max']}",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")


def run(canonical_db: Path = AGENT_CONVERSATIONS_DB, db_path: Path = UNIFIED_DB,
        write: bool = False) -> int:
    inventory = build_inventory(canonical_db)
    if "error" in inventory:
        print(f"[error] {inventory['error']}")
        return 1

    write_report(inventory)

    print("=" * 60)
    print("Phase 14 Plan 02 Task 1: Production Inventory")
    print("=" * 60)
    print(f"inventory_id:       {inventory['inventory_id']}")
    print(f"source_checksum:    {inventory['source_checksum']}")
    print(f"dataset_hash:       {inventory['dataset_hash']}")
    print(f"coarse count:       {inventory['coarse_count']}")
    print(f"authoritative:      {inventory['authoritative_count']}")
    print(f"excluded short:     {inventory['excluded']['short_after_cleaning']}")
    print(f"excluded dup:       {inventory['excluded']['duplicate_content_hash']}")
    print(f"excluded injection: {inventory['excluded']['injection_only']}")
    print(f"excluded tool:      {inventory['excluded']['excluded_tool']}")
    print(f"time range:         {inventory['time_range_min']} → {inventory['time_range_max']}")
    print(f"with injection:     {inventory['stats']['with_injection']}")
    print()
    print(f"报告: {INVENTORY_MD}")

    if write:
        write_inventory_to_db(inventory, db_path)
        print(f"[ok] inventory 已写入 DB ({inventory['authoritative_count']} items)")
    else:
        print("[dry-run] 未写入 DB，加 --write 写入")

    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 14 Plan 02 Task 1: freeze production inventory")
    p.add_argument("--inspect", action="store_true", help="只报告不写")
    p.add_argument("--write", action="store_true", help="写入 DB")
    p.add_argument("--canonical-db", type=Path, default=AGENT_CONVERSATIONS_DB)
    p.add_argument("--db", type=Path, default=UNIFIED_DB)
    args = p.parse_args(argv)
    return run(args.canonical_db, args.db, write=args.write)


if __name__ == "__main__":
    raise SystemExit(main())
