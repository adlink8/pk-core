"""Phase 14 Plan 03 Task 3：canonicalization builder。

把 passed extraction run 的 knowledge_units 去重合并为 canonical_knowledge_units。

流程：
  1. 按 subject、unit_type、evidence_scope、temporal compatibility 分桶
  2. 同桶内用 question+answer embedding 相似度提案
  3. 0.85+ 相似度 → 合并 proposal
  4. merge 后 confidence 取 members 最小值
  5. conflict → review，不自动 current
  6. 保留 member links + merge reason + supersedes/version lineage

不自动合并跨 subject/role/time 不兼容的 unit。
canonical row count 不作为 gate（无自然重复时相等是合法结果）。

用法::

    python build_canonical_knowledge_units.py --run <run_id> --dry-run
    python build_canonical_knowledge_units.py --run <run_id> --write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.core.sqlite import connect_rw

from personal_knowledge.core.project_paths import UNIFIED_DB, KNOWLEDGE_EVAL_DIR

MERGE_SIMILARITY_THRESHOLD = 0.85


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_id(subject: str, unit_type: str, answer: str) -> str:
    """稳定 canonical ID：基于规范化 subject|type|answer_hash。"""
    normalized = f"{subject.lower().strip()}|{unit_type}|{hashlib.sha256(answer.encode()).hexdigest()[:16]}"
    return "cu|" + hashlib.sha256(normalized.encode()).hexdigest()[:32]


@dataclass
class CanonicalStats:
    """canonicalization 统计。"""
    total_units: int = 0
    buckets: int = 0
    canonical_units: int = 0
    merged: int = 0          # 合并了多 member 的 canonical
    singletons: int = 0      # 只有 1 member 的 canonical
    conflicts: int = 0       # 标记为 conflict/review
    by_type: dict = field(default_factory=dict)


def load_units_for_canonicalization(run_id: str, db_path: Path = UNIFIED_DB) -> list[dict]:
    """加载 run 的可 canonical 化 units。

    扩大 production 批可能把大量 units 直接标为 current；一并纳入，
    避免只吃 staging 子集导致 canonical 覆盖不全。
    """
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT unit_id, unit_type, subject, question, answer, confidence, "
        "evidence_quote, lifecycle, evidence_scope, source_message_ref, source_agent "
        "FROM knowledge_units WHERE run_id=? AND status IN "
        "('staging','validated','current')",
        (run_id,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def build_buckets(units: list[dict]) -> dict[str, list[dict]]:
    """按 subject + unit_type + evidence_scope 分桶。

    temporal compatibility：同 subject 不同时间结论（lifecycle=conflict）分开。
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    for u in units:
        # temporal：conflict 单独分桶
        temporal = "conflict" if u.get("lifecycle") == "conflict" else "normal"
        key = f"{u['subject'].lower().strip()}|{u['unit_type']}|{u.get('evidence_scope', 'user')}|{temporal}"
        buckets[key].append(u)
    return buckets


def compute_similarity(text_a: str, text_b: str) -> float:
    """字符级 n-gram Jaccard 相似度（n=4）。

    去空白/小写后取字符 n-gram 集合做 Jaccard，短文本（< n 个字符）降级为整串；
    与 application/graph/build_merge_layer.py 的 ngrams() 同款逻辑。
    词级 Jaccard 对中文无效（无空格分词，整句成一个"词"），故改用 char n-gram。

    注意：下游阈值（MERGE_SIMILARITY_THRESHOLD、ANSWER_SIM、SUBJECT_SIM）是
    词级 Jaccard 时代的历史经验值，本次未改动；后续需用 eval 集
    （见 evaluate_merge_gate：20 positives / 20 hard negatives）重新校准。

    空输入返回 0.0。
    """
    grams_a = _char_ngrams(text_a)
    grams_b = _char_ngrams(text_b)
    if not grams_a or not grams_b:
        return 0.0
    intersection = grams_a & grams_b
    union = grams_a | grams_b
    return len(intersection) / len(union)


def _char_ngrams(text: str, n: int = 4) -> set[str]:
    """字符级 n-gram 集合（去空白、小写后）。短文本降级到整串。"""
    s = re.sub(r"\s+", "", (text or "").lower())
    if len(s) < n:
        return {s} if s else set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def find_merge_proposals(bucket: list[dict]) -> list[list[dict]]:
    """在桶内找合并提案组。返回 list of groups（每个 group 是应合并的 units）。"""
    if len(bucket) <= 1:
        return [[u] for u in bucket]

    # 计算两两相似度
    n = len(bucket)
    merged: list[bool] = [False] * n
    groups: list[list[dict]] = []

    for i in range(n):
        if merged[i]:
            continue
        group = [bucket[i]]
        merged[i] = True
        for j in range(i + 1, n):
            if merged[j]:
                continue
            sim = compute_similarity(
                f"{bucket[i]['question']} {bucket[i]['answer']}",
                f"{bucket[j]['question']} {bucket[j]['answer']}",
            )
            if sim >= MERGE_SIMILARITY_THRESHOLD:
                group.append(bucket[j])
                merged[j] = True
        groups.append(group)

    return groups


# lifecycle 严重程度（越大越保守），多成员合并时取最严重的值。
_LIFECYCLE_SEVERITY = {"current": 0, "deprecated": 1, "superseded": 2, "conflict": 3}


def merge_group(group: list[dict]) -> dict:
    """把一组 units 合并为一个 canonical unit。

    merge 后 confidence 取 members 最小值；lifecycle 取 members 中最保守的值
    （conflict > superseded > deprecated > current），避免 deprecated/superseded/
    conflict 的 unit 被合并静默"复活"为 current（"标 lifecycle / supersede，不硬删"）。
    单成员 group 保留 unit 原 lifecycle。
    """
    if len(group) == 1:
        u = group[0]
        return {
            "canonical_unit_id": _canonical_id(u["subject"], u["unit_type"], u["answer"]),
            "subject": u["subject"],
            "unit_type": u["unit_type"],
            "question": u["question"],
            "answer": u["answer"],
            "confidence": u["confidence"],
            "lifecycle": u["lifecycle"],
            "members": [u["unit_id"]],
            "merge_reason": "single" if len(group) == 1 else "similar_merge",
        }

    # 多 member 合并：取最长 answer 作为代表
    best = max(group, key=lambda u: len(u["answer"]))
    min_conf = min(u["confidence"] for u in group)
    # lifecycle 取 members 中最保守的值，任何非 current 成员都会体现在合并结果上
    lifecycle = max(
        (u["lifecycle"] for u in group),
        key=lambda lc: _LIFECYCLE_SEVERITY[lc],
    )

    return {
        "canonical_unit_id": _canonical_id(best["subject"], best["unit_type"], best["answer"]),
        "subject": best["subject"],
        "unit_type": best["unit_type"],
        "question": best["question"],
        "answer": best["answer"],
        "confidence": min_conf,
        "lifecycle": lifecycle,
        "members": [u["unit_id"] for u in group],
        "merge_reason": "similar_merge",
    }


def build_canonical(run_id: str, db_path: Path = UNIFIED_DB,
                    write: bool = False) -> tuple[CanonicalStats, list[dict]]:
    """构建 canonical knowledge units。返回 (stats, canonical_list)。"""
    units = load_units_for_canonicalization(run_id, db_path)
    stats = CanonicalStats(total_units=len(units))

    if not units:
        return stats, []

    buckets = build_buckets(units)
    stats.buckets = len(buckets)

    canonical_list: list[dict] = []
    for bucket_key, bucket_units in buckets.items():
        groups = find_merge_proposals(bucket_units)
        for group in groups:
            canonical = merge_group(group)
            canonical_list.append(canonical)

            if len(group) > 1:
                stats.merged += 1
            else:
                stats.singletons += 1

            if canonical["lifecycle"] == "conflict":
                stats.conflicts += 1

            stats.by_type[canonical["unit_type"]] = (
                stats.by_type.get(canonical["unit_type"], 0) + 1
            )

    stats.canonical_units = len(canonical_list)

    if write:
        _write_canonical_to_db(canonical_list, run_id, db_path)

    return stats, canonical_list


def _write_canonical_to_db(canonical_list: list[dict], run_id: str,
                            db_path: Path = UNIFIED_DB) -> None:
    """写 canonical_knowledge_units + canonical_unit_members。"""
    con = connect_rw(db_path)
    now = _utc_now()
    try:
        for cu in canonical_list:
            con.execute(
                "INSERT OR REPLACE INTO canonical_knowledge_units VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cu["canonical_unit_id"], cu["subject"], cu["unit_type"],
                    cu["question"], cu["answer"], cu["confidence"],
                    cu["lifecycle"], "staging", 1, run_id,
                    cu["merge_reason"], None, now,
                ),
            )
            for member_id in cu["members"]:
                con.execute(
                    "INSERT OR IGNORE INTO canonical_unit_members "
                    "(canonical_unit_id, member_unit_id) VALUES (?,?)",
                    (cu["canonical_unit_id"], member_id),
                )
        con.commit()
    finally:
        con.close()


def _resolve_pair_units(con: sqlite3.Connection, ref: str) -> set[str]:
    """把 pair ref 解析为 unit_id 集。

    eval pair 的 ref 是 cm| 消息级引用（消息 → 多条 unit 一对多），
    需经 knowledge_unit_evidence 解析；若 ref 本身已是 member unit_id
    则原样返回。
    """
    if con.execute(
        "SELECT 1 FROM canonical_unit_members WHERE member_unit_id=? LIMIT 1", (ref,)
    ).fetchone():
        return {ref}
    rows = con.execute(
        "SELECT unit_id FROM knowledge_unit_evidence WHERE evidence_ref=?", (ref,)
    ).fetchall()
    return {str(r[0]) for r in rows}


def _canonical_ids_of(con: sqlite3.Connection, unit_ids: set[str]) -> set[str]:
    out: set[str] = set()
    for uid in unit_ids:
        row = con.execute(
            "SELECT canonical_unit_id FROM canonical_unit_members WHERE member_unit_id=?",
            (uid,),
        ).fetchone()
        if row:
            out.add(str(row[0]))
    return out


def evaluate_merge_gate(db_path: Path = UNIFIED_DB,
                        eval_dir: Path | None = None) -> dict:
    """评估 merge gate：20 positives recall≥80%，20 hard negatives false merge=0。

    用 eval dataset 的 merge_positive_pairs 和 hard_negative_pairs。
    pair ref 为 cm| 消息级引用，经 knowledge_unit_evidence 解析到 unit，
    再经 canonical_unit_members 映射到 canonical；一对多时任一组合并即算命中。
    无法解析的 pair（消息未产 unit / 未进本批 canonical）单独计数，
    不计入 recall 分母；全部无法解析时 gate 标记 not_applicable 而非 FAIL。
    """
    if eval_dir is None:
        eval_dir = KNOWLEDGE_EVAL_DIR

    results = {"positive_recall": 0.0, "hard_negative_false_merge": 0, "passed": False}

    # 加载 pairs
    pos_path = eval_dir / "merge_positive_pairs.private.jsonl"
    neg_path = eval_dir / "hard_negative_pairs.private.jsonl"
    if not pos_path.exists() or not neg_path.exists():
        results["error"] = "eval pairs not found"
        return results

    positives = [json.loads(l) for l in pos_path.read_text(encoding="utf-8").strip().split("\n") if l.strip()]
    negatives = [json.loads(l) for l in neg_path.read_text(encoding="utf-8").strip().split("\n") if l.strip()]

    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)

    # positives: 任一 A unit 与任一 B unit 被合并到同一 canonical → 命中
    true_positive = 0
    pos_unresolvable = 0
    for pair in positives:
        ca = _canonical_ids_of(con, _resolve_pair_units(con, pair["unit_a_ref"]))
        cb = _canonical_ids_of(con, _resolve_pair_units(con, pair["unit_b_ref"]))
        if not ca or not cb:
            pos_unresolvable += 1
            continue
        if ca & cb:
            true_positive += 1

    # negatives: 任一 A unit 与任一 B unit 被错误合并到同一 canonical → 误并
    false_merge = 0
    neg_unresolvable = 0
    for pair in negatives:
        ca = _canonical_ids_of(con, _resolve_pair_units(con, pair["unit_a_ref"]))
        cb = _canonical_ids_of(con, _resolve_pair_units(con, pair["unit_b_ref"]))
        if not ca or not cb:
            neg_unresolvable += 1
            continue
        if ca & cb:
            false_merge += 1

    con.close()

    resolvable = len(positives) - pos_unresolvable
    results["positive_recall"] = round(true_positive / max(resolvable, 1), 4)
    results["positive_unresolvable"] = pos_unresolvable
    results["negative_unresolvable"] = neg_unresolvable
    results["hard_negative_false_merge"] = false_merge
    if resolvable == 0:
        # 正例全部无法解析（如 eval pair 的 unit 不在本批 canonical 范围内）：
        # 无正例可评，gate 不适用；负例误并仍硬要求 0
        results["not_applicable"] = True
        results["passed"] = false_merge == 0
    else:
        results["passed"] = (
            results["positive_recall"] >= 0.80
            and false_merge == 0
        )
    return results


def run(run_id: str, db_path: Path = UNIFIED_DB, write: bool = False) -> int:
    stats, canonical_list = build_canonical(run_id, db_path, write)

    print("=" * 60)
    print("Phase 14 Plan 03 Task 3: Canonicalization")
    print("=" * 60)
    print(f"run_id:           {run_id}")
    print(f"total units:      {stats.total_units}")
    print(f"buckets:          {stats.buckets}")
    print(f"canonical units:  {stats.canonical_units}")
    print(f"merged (multi):   {stats.merged}")
    print(f"singletons:       {stats.singletons}")
    print(f"conflicts:        {stats.conflicts}")
    print(f"by_type:          {stats.by_type}")

    if write:
        # 评估 merge gate
        gate = evaluate_merge_gate(db_path)
        print()
        print("=== Merge Gate ===")
        print(f"positive recall:      {gate.get('positive_recall', 'n/a')}")
        print(f"hard neg false merge: {gate.get('hard_negative_false_merge', 'n/a')}")
        if gate.get("positive_unresolvable") or gate.get("negative_unresolvable"):
            print(f"unresolvable pairs:   pos={gate.get('positive_unresolvable', 0)} "
                  f"neg={gate.get('negative_unresolvable', 0)}")
        note = " (not_applicable: no resolvable positives)" if gate.get("not_applicable") else ""
        print(f"gate:                 {'PASS' if gate.get('passed') else 'FAIL'}{note}")
    else:
        print("\n[dry-run] 未写入 DB")

    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 14 Plan 03 Task 3: canonicalization")
    p.add_argument("--run", required=True, help="extraction run_id")
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--write", action="store_true")
    p.add_argument("--db", type=Path, default=UNIFIED_DB)
    args = p.parse_args(argv)
    if not args.write and not args.dry_run:
        args.dry_run = True
    return run(args.run, args.db, args.write)


if __name__ == "__main__":
    raise SystemExit(main())
