"""记忆深挖准入评估脚本(Phase 05 Wave 5)。

不直接产出深层洞察,只回答一个问题:
当前 memory graph 是否已经具备进入 Phase 06 的证据质量。
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
UNIFIED_DB = ROOT / "integration" / "db" / "personal_system.sqlite"
OUT_FILE = ROOT / "integration" / "analysis" / "ai_context" / "memory_depth_readiness.md"

ITEM_SAMPLE = 12
RELATION_SAMPLE = 8


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


def _days_span(values: list[str]) -> int:
    points = [dt for dt in (_parse_time(v) for v in values) if dt is not None]
    if len(points) < 2:
        return 0
    return (max(points) - min(points)).days


def _load_metadata(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


@dataclass
class SampleResult:
    """单条 item/relation 抽样评估结果。"""
    sample_type: str
    subject: str
    evidence_count: int
    time_span_days: int
    recurrence: int
    relation_strength: float
    contradiction: str
    depth_candidate: bool
    reason: str


def _item_samples(con: sqlite3.Connection) -> list[SampleResult]:
    """抽样评估 memory item。"""
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT memory_id, memory_type, memory_subtype, subject, description, "
        "confidence, evidence_count, metadata "
        "FROM memory_items "
        "ORDER BY evidence_count DESC, confidence DESC, subject "
        "LIMIT ?",
        (ITEM_SAMPLE,),
    ).fetchall()
    out: list[SampleResult] = []
    for row in rows:
        metadata = _load_metadata(row["metadata"])
        evidence_ids = metadata.get("evidence_ids") or []
        relation_strength = con.execute(
            "SELECT COALESCE(MAX(strength), 0.0) "
            "FROM memory_relations WHERE from_memory_id=? OR to_memory_id=?",
            (row["memory_id"], row["memory_id"]),
        ).fetchone()[0]
        evidence_rows = []
        if evidence_ids:
            placeholders = ",".join("?" * len(evidence_ids))
            evidence_rows = con.execute(
                f"SELECT event_time, month FROM unified_events WHERE event_id IN ({placeholders})",
                evidence_ids,
            ).fetchall()
        months = sorted({r["month"][:7] for r in evidence_rows if r["month"]})
        time_values = [str(r["event_time"] or "") for r in evidence_rows]
        time_span_days = _days_span(time_values)
        recurrence = len(months) or min(int(row["evidence_count"] or 0), 1)

        contradiction = ""
        subtype = str(row["memory_subtype"] or "")
        if "declining" in subtype and recurrence < 2:
            contradiction = "declining 但缺少跨月证据"
        elif "continuous" in subtype and recurrence < 2:
            contradiction = "continuous 但缺少跨月重复"
        elif int(row["evidence_count"] or 0) <= 1 and relation_strength >= 0.8:
            contradiction = "关系强但原始证据过少"

        depth_candidate = (
            int(row["evidence_count"] or 0) >= 3
            and recurrence >= 2
            and time_span_days >= 7
            and float(relation_strength or 0) >= 0.6
            and not contradiction
        )
        reason = (
            "具备跨时间重复和关系强度"
            if depth_candidate
            else "证据链、时间跨度或关系强度不足"
        )
        out.append(
            SampleResult(
                sample_type="item",
                subject=f"{row['subject']} [{row['memory_type']}/{row['memory_subtype']}]",
                evidence_count=int(row["evidence_count"] or 0),
                time_span_days=time_span_days,
                recurrence=recurrence,
                relation_strength=float(relation_strength or 0),
                contradiction=contradiction or "无明显冲突",
                depth_candidate=depth_candidate,
                reason=reason,
            )
        )
    return out


def _relation_samples(con: sqlite3.Connection) -> list[SampleResult]:
    """抽样评估 memory relation。"""
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT mr.strength, mr.relation, "
        "src.subject AS from_subject, src.evidence_count AS from_count, src.metadata AS from_metadata, "
        "dst.subject AS to_subject, dst.evidence_count AS to_count, dst.metadata AS to_metadata "
        "FROM memory_relations mr "
        "JOIN memory_items src ON src.memory_id = mr.from_memory_id "
        "JOIN memory_items dst ON dst.memory_id = mr.to_memory_id "
        "ORDER BY mr.strength DESC, src.evidence_count DESC, dst.evidence_count DESC "
        "LIMIT ?",
        (RELATION_SAMPLE,),
    ).fetchall()
    out: list[SampleResult] = []
    for row in rows:
        left = _load_metadata(row["from_metadata"])
        right = _load_metadata(row["to_metadata"])
        evidence_ids = list(dict.fromkeys((left.get("evidence_ids") or []) + (right.get("evidence_ids") or [])))[:20]
        evidence_rows = []
        if evidence_ids:
            placeholders = ",".join("?" * len(evidence_ids))
            evidence_rows = con.execute(
                f"SELECT event_time, month FROM unified_events WHERE event_id IN ({placeholders})",
                evidence_ids,
            ).fetchall()
        months = sorted({r["month"][:7] for r in evidence_rows if r["month"]})
        time_span_days = _days_span([str(r["event_time"] or "") for r in evidence_rows])
        recurrence = len(months) or (1 if evidence_ids else 0)
        evidence_count = min(int(row["from_count"] or 0), int(row["to_count"] or 0))
        contradiction = ""
        if float(row["strength"] or 0) >= 0.8 and evidence_count < 2:
            contradiction = "边权高，但两端证据过薄"
        depth_candidate = (
            evidence_count >= 2
            and recurrence >= 2
            and time_span_days >= 7
            and float(row["strength"] or 0) >= 0.7
            and not contradiction
        )
        out.append(
            SampleResult(
                sample_type="relation",
                subject=f"{row['from_subject']} --{row['relation']}--> {row['to_subject']}",
                evidence_count=evidence_count,
                time_span_days=time_span_days,
                recurrence=recurrence,
                relation_strength=float(row["strength"] or 0),
                contradiction=contradiction or "无明显冲突",
                depth_candidate=depth_candidate,
                reason="关系可进入深挖" if depth_candidate else "关系更像浅层共现/弱关联",
            )
        )
    return out


def build_report() -> Path:
    """生成 readiness 报告。"""
    con = sqlite3.connect(UNIFIED_DB)
    item_samples = _item_samples(con)
    relation_samples = _relation_samples(con)
    samples = item_samples + relation_samples
    con.close()

    candidate_items = [s for s in samples if s.depth_candidate]
    shallow_items = [s for s in samples if not s.depth_candidate]
    avg_strength = statistics.mean([s.relation_strength for s in samples]) if samples else 0.0

    lines = [
        "# Memory Depth Readiness",
        "",
        f"- 抽样总数: {len(samples)}",
        f"- item 抽样: {len(item_samples)}",
        f"- relation 抽样: {len(relation_samples)}",
        f"- 可深挖候选: {len(candidate_items)}",
        f"- 浅层/阻塞候选: {len(shallow_items)}",
        f"- 平均关系强度: {avg_strength:.2f}",
        "",
        "## 可深挖候选主题",
    ]
    if candidate_items:
        for sample in candidate_items:
            lines.append(
                f"- {sample.subject}: 证据 {sample.evidence_count}, 跨时长 {sample.time_span_days} 天, "
                f"复现 {sample.recurrence}, 关系强度 {sample.relation_strength:.2f}"
            )
    else:
        lines.append("- 当前抽样中没有满足深挖门槛的候选。")

    lines.extend(
        [
            "",
            "## 暂不可信的浅层主题",
        ]
    )
    for sample in shallow_items:
        lines.append(
            f"- {sample.subject}: {sample.reason}; 冲突检查={sample.contradiction}; "
            f"证据 {sample.evidence_count}, 时长 {sample.time_span_days} 天, 复现 {sample.recurrence}"
        )

    lines.extend(
        [
            "",
            "## 抽样明细",
            "",
            "| 类型 | 主体 | evidence_count | time_span | recurrence | relation_strength | depth_candidate |",
            "| --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for sample in samples:
        lines.append(
            f"| {sample.sample_type} | {sample.subject} | {sample.evidence_count} | "
            f"{sample.time_span_days} | {sample.recurrence} | {sample.relation_strength:.2f} | "
            f"{'yes' if sample.depth_candidate else 'no'} |"
        )

    lines.extend(
        [
            "",
            "## 缺失的证据字段",
            "- relation 级别缺少直接 evidence_ids，只能借两端 memory 的证据近似评估。",
            "- 缺少显式 contradiction 标记，当前只能做规则式冲突检查。",
            "- 部分 memory item 没有稳定的跨月时间分布，导致 recurrence 只能从 links 反推。",
            "",
            "## Phase 06 输入限制",
            "- 只允许对 readiness 标为可深挖的主题做深层模式挖掘。",
            "- relation strength 高但 evidence_count 低的边，只能作为提示，不应直接写入深层洞察。",
            "- continuous / declining 这类趋势型记忆，必须要求跨月证据，不接受单次事件推断。",
            "- Phase 06 应优先消费 tooling、capability、project 这三类高证据对象。",
        ]
    )

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text("\n".join(lines), encoding="utf-8")
    return OUT_FILE


def main() -> None:
    out = build_report()
    print(f"已生成: {out}")


if __name__ == "__main__":
    main()
