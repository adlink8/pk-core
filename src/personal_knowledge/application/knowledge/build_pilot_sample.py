"""Phase 14 Plan 03 Task 1：冻结分层 pilot sample。

从 authoritative inventory 按 source、agent、time bucket、message length、
system-injection presence 做确定性分层采样 300-500 项。

manifest 记录 sample/inventory/source/model/prompt/schema/config hashes、
最大调用数、并发/retry 参数与预计预算，不含原文。

用法::

    python build_pilot_sample.py --inventory <id> --size 400
    python build_pilot_sample.py --inspect
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import UNIFIED_DB, AI_CONTEXT_DIR  # noqa: E402

PILOT_MANIFEST = AI_CONTEXT_DIR / "knowledge_unit_pilot_manifest.json"


def _hash(data: object) -> str:
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False) if not isinstance(data, str) else data
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def build_stratified_sample(
    inventory_id: str,
    target_size: int = 400,
    db_path: Path = UNIFIED_DB,
) -> dict:
    """确定性分层采样。返回 pilot manifest。"""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # 读 inventory 元信息
    inv = con.execute(
        "SELECT * FROM knowledge_inventory WHERE inventory_id=?", (inventory_id,)
    ).fetchone()
    if not inv:
        return {"error": f"inventory 不存在: {inventory_id}"}

    # 读全部 items
    items = [dict(r) for r in con.execute(
        "SELECT position, evidence_ref, content_hash, source, agent, "
        "time_bucket, length_bucket, has_injection "
        "FROM knowledge_inventory_items WHERE inventory_id=? ORDER BY position",
        (inventory_id,),
    )]
    con.close()

    if not items:
        return {"error": "inventory 无 items"}

    # 分层：agent × length × injection
    strata: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        key = f"{item['agent']}|{item['length_bucket']}|{item['has_injection']}"
        strata[key].append(item)

    # 按 strata 比例分配（每层至少 1 条，如果该层有数据）
    total = len(items)
    sample: list[dict] = []
    for strata_key, strata_items in sorted(strata.items()):
        # 按比例分配，但确保每层至少 1 条
        alloc = max(1, round(len(strata_items) / total * target_size))
        alloc = min(alloc, len(strata_items))
        # 确定性采样：取均匀间隔
        step = max(1, len(strata_items) // alloc)
        sampled = strata_items[::step][:alloc]
        sample.extend(sampled)

    # 按 position 排序（恢复用）
    sample.sort(key=lambda x: x["position"])

    # 限制到 target_size
    if len(sample) > target_size:
        sample = sample[:target_size]

    # sample hash（有序 content_hash 拼接）
    ordered_hashes = "|".join(s["content_hash"] for s in sample)
    sample_hash = _hash(ordered_hashes)

    # strata 覆盖统计
    sample_strata = defaultdict(int)
    for s in sample:
        sample_strata[f"{s['agent']}|{s['length_bucket']}|{s['has_injection']}"] += 1

    manifest = {
        "pilot_id": _hash(f"{inventory_id}|{sample_hash}|{target_size}"),
        "inventory_id": inventory_id,
        "inventory_dataset_hash": inv["dataset_hash"],
        "source_checksum": inv["source_checksum"],
        "sample_size": len(sample),
        "target_size": target_size,
        "sample_hash": sample_hash,
        "sample_positions": [s["position"] for s in sample],
        "strata_coverage": dict(sorted(sample_strata.items())),
        "strata_count": len(sample_strata),
        "max_paid_calls": len(sample),
        "concurrency": 1,
        "retry_max": 4,
        "base_backoff": 2.0,
        "max_backoff": 60.0,
        "actual_model_id": None,  # 待 preflight 填充
        "prompt_hash": None,  # 待 preflight 填充
        "schema_hash": "v1_extra_forbid",
        "config_hash": None,  # 待 preflight 填充
        "estimated_cost_usd": None,  # 待 preflight 填充
        "created_at": _utc_now(),
    }
    return manifest


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(inventory_id: str, target_size: int = 400, db_path: Path = UNIFIED_DB,
        write: bool = False) -> int:
    manifest = build_stratified_sample(inventory_id, target_size, db_path)
    if "error" in manifest:
        print(f"[error] {manifest['error']}")
        return 1

    print("=" * 60)
    print("Phase 14 Plan 03 Task 1: Stratified Pilot Sample")
    print("=" * 60)
    print(f"pilot_id:          {manifest['pilot_id']}")
    print(f"inventory_id:      {manifest['inventory_id']}")
    print(f"sample_size:       {manifest['sample_size']}")
    print(f"target_size:       {manifest['target_size']}")
    print(f"sample_hash:       {manifest['sample_hash']}")
    print(f"strata_count:      {manifest['strata_count']}")
    print(f"max_paid_calls:    {manifest['max_paid_calls']}")
    print(f"actual_model_id:   {manifest['actual_model_id']} (待 preflight)")
    print()
    print("strata coverage:")
    for k, v in manifest["strata_coverage"].items():
        print(f"  {k:40} {v}")

    if write:
        PILOT_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        # 不写 sample_positions 到 manifest（太大），写单独文件
        manifest_copy = {k: v for k, v in manifest.items()}
        manifest_copy["sample_positions"] = manifest["sample_positions"]  # 保留
        PILOT_MANIFEST.write_text(
            json.dumps(manifest_copy, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n[ok] manifest 已写入: {PILOT_MANIFEST}")
    else:
        print("\n[dry-run] 未写入，加 --write 写入")

    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 14 Plan 03 Task 1: stratified pilot sample")
    p.add_argument("--inventory", required=True, help="inventory ID")
    p.add_argument("--size", type=int, default=400, help="target sample size (300-500)")
    p.add_argument("--write", action="store_true")
    p.add_argument("--db", type=Path, default=UNIFIED_DB)
    args = p.parse_args(argv)
    if not 300 <= args.size <= 500:
        print("[error] --size 必须在 300-500", file=sys.stderr)
        return 2
    return run(args.inventory, args.size, args.db, args.write)


if __name__ == "__main__":
    raise SystemExit(main())
