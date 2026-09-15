"""Wave 8.3.1 验证:对比新旧 conversation_summaries 产物。

检查:
1. turn 总数一致(容差 ±2,因段数校验可能微调)
2. session 数一致
3. 瑕疵数对比(`**` 等结构性瑕疵应大幅减少)
4. 抽样对比 narrative 质量(新旧各看几个 turn)

用法:
  python compare_summaries.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
NEW = ROOT / "integration" / "analysis" / "ai_context" / "conversation_summaries.json"
OLD = (
    ROOT
    / "integration"
    / "analysis"
    / "ai_context"
    / "_archive"
    / "conversation"
    / "backup_wave8"
    / "conversation_summaries.pre_wave8.json"
)


def count_defects(summaries: list[dict]) -> tuple[int, int, dict]:
    """返回 (total_turns, defect_count, breakdown)。"""
    total = 0
    defect = 0
    breakdown: dict[str, int] = {}
    defects_set = {"**", "***", "*", "#", "---", "—"}
    for s in summaries:
        for t in s.get("turn_summaries", []):
            total += 1
            n = (t.get("narrative") or "").strip()
            dtype = None
            if not n:
                dtype = "empty"
            elif n in defects_set:
                dtype = "double_star"
            elif n.startswith("*") and len(n) < 8:
                dtype = "double_star"
            elif n.startswith("#") and len(n) < 8:
                dtype = "double_star"
            if dtype:
                defect += 1
                breakdown[dtype] = breakdown.get(dtype, 0) + 1
    return total, defect, breakdown


def main() -> int:
    if not NEW.exists():
        print(f"[error] 新产物不存在: {NEW.name}")
        return 1
    if not OLD.exists():
        print(f"[error] 旧产物不存在: {OLD.name}")
        return 1

    new = json.loads(NEW.read_text(encoding="utf-8"))
    old = json.loads(OLD.read_text(encoding="utf-8"))

    print("=" * 60)
    print("Wave 8.3.1 新旧产物对比")
    print("=" * 60)

    # session 数
    print(f"\nsession 数: 旧 {len(old)} → 新 {len(new)}")
    new_ids = {s["session_id"] for s in new}
    old_ids = {s["session_id"] for s in old}
    only_new = new_ids - old_ids
    only_old = old_ids - new_ids
    if only_new:
        print(f"  仅新产物有: {len(only_new)} 个")
    if only_old:
        print(f"  仅旧产物有: {len(only_old)} 个")

    # turn 数
    old_total, old_defect, old_break = count_defects(old)
    new_total, new_defect, new_break = count_defects(new)
    print(f"\nturn 总数: 旧 {old_total} → 新 {new_total} "
          f"(差 {new_total - old_total}, 容差 ±2)")
    turn_diff_ok = abs(new_total - old_total) <= 2
    print(f"  turn 数一致性: {'✅ 通过' if turn_diff_ok else '❌ 超容差'}")

    # 瑕疵对比
    old_rate = (old_total - old_defect) / old_total * 100 if old_total else 0
    new_rate = (new_total - new_defect) / new_total * 100 if new_total else 0
    print(f"\n瑕疵数: 旧 {old_defect} → 新 {new_defect}")
    print(f"  旧瑕疵分布: {old_break}")
    print(f"  新瑕疵分布: {new_break}")
    print(f"\n正常率: 旧 {old_rate:.2f}% → 新 {new_rate:.2f}%")
    print(f"  门槛: ≥ 98%")
    gate_ok = new_rate >= 98.0
    print(f"  门槛判定: {'✅ PASS' if gate_ok else '❌ FAIL'}")

    # 抽样:看旧产物里有 `**` 瑕疵的 session,在新产物里是否修复
    print(f"\n--- 抽样验证:旧瑕疵 turn 在新产物的状态 ---")
    fixed = 0
    still_bad = 0
    for s in old:
        sid = s["session_id"]
        for i, t in enumerate(s.get("turn_summaries", [])):
            n = (t.get("narrative") or "").strip()
            is_defect = (n in {"**", "***", "*", "#", "---"}) or \
                        (n.startswith("*") and len(n) < 8)
            if not is_defect:
                continue
            # 找新产物同 session 同 turn
            new_s = next((x for x in new if x["session_id"] == sid), None)
            if not new_s or i >= len(new_s.get("turn_summaries", [])):
                still_bad += 1
                continue
            new_n = (new_s["turn_summaries"][i].get("narrative") or "").strip()
            if new_n and new_n not in {"**", "***", "*", "#", "---"} and \
               not (new_n.startswith("*") and len(new_n) < 8):
                fixed += 1
            else:
                still_bad += 1
                if still_bad <= 3:
                    print(f"  [仍瑕疵] {sid[:24]}.. Turn{i}: {new_n[:50]!r}")
    print(f"  旧瑕疵修复: {fixed}/{fixed+still_bad} "
          f"({fixed/(fixed+still_bad)*100:.1f}% 已修复)")

    print("\n" + "=" * 60)
    overall = turn_diff_ok and gate_ok
    print(f"总判定: {'✅ Wave 8.3.1 通过' if overall else '❌ 需排查'}")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
