"""Wave 8.1: 压缩叙述摘要的客观质量评估。

读取 conversation_summaries.json,从四个维度量化质量,产出 JSON + MD 报告,
并按硬门槛(正常率 ≥ 98%、回溯链覆盖率 100%)判定 PASS/FAIL。

评估维度:
1. 完整度(瑕疵 turn 占比):识别 `**`/`***`/空/占位符等结构性瑕疵。
   注意区分"真瑕疵"(LLM 输出异常)和"原文本就短"(用户只发了一两个字)——
   后者不算瑕疵,摘要是忠实的。
2. 信息密度(长度分布):统计 narrative 字符长度分布,识别异常短/异常长。
3. 回溯链完整率(source_refs 覆盖率):每个 turn 是否带 source_refs。
4. 因果完整性(瑕疵是否造成 turn 链断裂):瑕疵 turn 是否导致相邻 turn 错位。

用法:
  python evaluate_conversation_quality.py              # dry-run,只打印
  python evaluate_conversation_quality.py --write      # 落盘报告
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
IN_JSON = ROOT / "integration" / "analysis" / "ai_context" / "conversation_summaries.json"
OUT_JSON = ROOT / "integration" / "analysis" / "ai_context" / "conversation_quality_report.json"
OUT_MD = ROOT / "integration" / "analysis" / "ai_context" / "conversation_quality_report.md"

# ---- 质量门槛(Wave 8 acceptance criteria) ----
GATE_NORMAL_RATE = 0.98      # 正常率 ≥ 98%(不含"原文本就短"的真忠实例)
GATE_TRACE_RATE = 1.00       # 回溯链覆盖率 100%

# ---- 瑕疵识别阈值 ----
MIN_MEANINGFUL_LEN = 15      # 短于此且不含实质内容视为可疑(需结合原文判断)
STRUCTURAL_DEFECTS = {"**", "***", "*", "#", "---", "—", "•", "-"}


@dataclass
class TurnDefect:
    """单个 turn 的瑕疵记录。"""
    session_id: str
    turn_index: int          # 0-based
    turn_id: str | None
    defect_type: str         # double_star / empty / placeholder / suspicious_short
    narrative_preview: str
    source_refs_count: int


@dataclass
class QualityReport:
    total_sessions: int = 0
    total_turns: int = 0
    # 完整度
    defect_count: int = 0                 # 真瑕疵数(不含忠实短摘要)
    normal_rate: float = 0.0              # 正常率 = (total - defect) / total
    defects: list[TurnDefect] = field(default_factory=list)
    defect_breakdown: dict = field(default_factory=dict)
    # 信息密度
    length_min: int = 0
    length_max: int = 0
    length_mean: float = 0.0
    length_p50: int = 0
    length_p95: int = 0
    short_narratives: list[TurnDefect] = field(default_factory=list)  # 忠实短摘要(非瑕疵)
    # 回溯链
    turns_with_refs: int = 0
    trace_coverage: float = 0.0
    turns_missing_refs: list[TurnDefect] = field(default_factory=list)
    # 因果完整性(瑕疵 turn 的相邻错位检测)
    chain_break_count: int = 0
    # 门槛判定
    gate_passed: bool = False
    gate_reasons: list[str] = field(default_factory=list)


def classify_narrative(narrative: str) -> str | None:
    """判断单个 narrative 是否为真瑕疵。返回瑕疵类型或 None(正常)。

    真瑕疵 = LLM 输出异常,非忠实短摘要。
    - double_star:只剩 markdown 装饰符(`**`/`***` 等),内容被吞
    - empty:空字符串
    - placeholder:含占位符文本(摘要缺失/(无)
    - suspicious_short:极短且无实质内容(如单个标点)
    """
    n = (narrative or "").strip()
    if not n:
        return "empty"
    if n in STRUCTURAL_DEFECTS:
        return "double_star"
    # 以装饰符开头且整体极短(如 "**" 后跟一两个字符)
    if n.startswith("*") and len(n) < 8:
        return "double_star"
    if n.startswith("#") and len(n) < 8:
        return "double_star"
    if "摘要缺失" in n or n.startswith("(无"):
        return "placeholder"
    # 单字符或纯标点
    if len(n) <= 3 and not any(c.isalnum() for c in n):
        return "suspicious_short"
    return None


def is_faithful_short(narrative: str) -> bool:
    """判断是否为"忠实的短摘要"(原文用户只发了一两个字)。

    这类不是瑕疵——摘要忠实反映了原文。特征:有完整句式但内容少,
    典型如"用户再次发送"hi""。区分标准:长度 4-40 且含中文/字母(有语义)。
    """
    n = (narrative or "").strip()
    if not n or len(n) > 40:
        return False
    # 有中文或字母,且不是纯装饰符
    has_alnum = any(c.isalnum() for c in n)
    return has_alnum and len(n) >= 4


def evaluate(summaries: list[dict]) -> QualityReport:
    """对 conversation_summaries 做客观质量评估。"""
    report = QualityReport(
        total_sessions=len(summaries),
        total_turns=0,
        defect_count=0,
    )

    all_lengths: list[int] = []
    defect_types: dict[str, int] = {}

    for s in summaries:
        sid = s["session_id"]
        turn_sums = s.get("turn_summaries", [])
        for i, t in enumerate(turn_sums):
            report.total_turns += 1
            narrative = t.get("narrative", "")
            refs = t.get("source_refs", [])
            all_lengths.append(len(narrative))

            # 瑕疵分类
            dtype = classify_narrative(narrative)
            if dtype:
                report.defect_count += 1
                defect_types[dtype] = defect_types.get(dtype, 0) + 1
                report.defects.append(TurnDefect(
                    session_id=sid, turn_index=i, turn_id=t.get("turn_id"),
                    defect_type=dtype,
                    narrative_preview=narrative[:60],
                    source_refs_count=len(refs),
                ))
            elif is_faithful_short(narrative):
                # 忠实短摘要,记录但不计入瑕疵
                report.short_narratives.append(TurnDefect(
                    session_id=sid, turn_index=i, turn_id=t.get("turn_id"),
                    defect_type="faithful_short",
                    narrative_preview=narrative[:60],
                    source_refs_count=len(refs),
                ))

            # 回溯链
            if refs:
                report.turns_with_refs += 1
            else:
                report.turns_missing_refs.append(TurnDefect(
                    session_id=sid, turn_index=i, turn_id=t.get("turn_id"),
                    defect_type="missing_refs",
                    narrative_preview=narrative[:60],
                    source_refs_count=0,
                ))

    # 完整度
    report.defect_breakdown = defect_types
    report.normal_rate = (
        (report.total_turns - report.defect_count) / report.total_turns
        if report.total_turns else 0.0
    )

    # 信息密度
    if all_lengths:
        sl = sorted(all_lengths)
        report.length_min = sl[0]
        report.length_max = sl[-1]
        report.length_mean = round(statistics.mean(all_lengths), 1)
        report.length_p50 = sl[len(sl) // 2]
        report.length_p95 = sl[int(len(sl) * 0.95)]

    # 回溯链
    report.trace_coverage = (
        report.turns_with_refs / report.total_turns
        if report.total_turns else 0.0
    )

    # 因果完整性:瑕疵 turn 是否造成相邻 turn 错位
    # 简化检测:同一 session 内连续 ≥2 个 double_star,视为链断裂
    by_session: dict[str, list[TurnDefect]] = {}
    for d in report.defects:
        if d.defect_type in ("double_star", "empty"):
            by_session.setdefault(d.session_id, []).append(d)
    for sid, defs in by_session.items():
        idxs = sorted(d.turn_index for d in defs)
        # 连续索引(差为1)的数量
        consecutive = sum(1 for a, b in zip(idxs, idxs[1:]) if b - a == 1)
        if consecutive > 0:
            report.chain_break_count += consecutive

    # 门槛判定
    report.gate_passed = True
    if report.normal_rate < GATE_NORMAL_RATE:
        report.gate_passed = False
        gap = (GATE_NORMAL_RATE - report.normal_rate) * 100
        report.gate_reasons.append(
            f"正常率 {report.normal_rate*100:.2f}% < 门槛 {GATE_NORMAL_RATE*100:.0f}% "
            f"(差 {gap:.2f}%，需修复 {int(gap/100*report.total_turns)+1} 个瑕疵)"
        )
    if report.trace_coverage < GATE_TRACE_RATE:
        report.gate_passed = False
        report.gate_reasons.append(
            f"回溯链覆盖率 {report.trace_coverage*100:.2f}% < 门槛 {GATE_TRACE_RATE*100:.0f}% "
            f"(缺 {len(report.turns_missing_refs)} 个 turn 的 source_refs)"
        )
    if report.chain_break_count > 0:
        report.gate_passed = False
        report.gate_reasons.append(
            f"因果链断裂 {report.chain_break_count} 处(连续瑕疵 turn)"
        )

    return report


def to_serializable(report: QualityReport) -> dict:
    """转可序列化 dict(含门槛常量)。"""
    d = asdict(report)
    d["gate_thresholds"] = {
        "normal_rate": GATE_NORMAL_RATE,
        "trace_coverage": GATE_TRACE_RATE,
    }
    return d


def write_markdown(report: QualityReport) -> str:
    """生成 Markdown 报告。"""
    status = "✅ PASS" if report.gate_passed else "❌ FAIL"
    lines = [
        "# 压缩叙述摘要质量评估报告", "",
        f"**门槛判定: {status}**", "",
        f"- 评估时间产物: conversation_summaries.json",
        f"- 总 session 数: {report.total_sessions}",
        f"- 总 turn 数: {report.total_turns}",
        "",
        "## 1. 完整度(瑕疵 turn 占比)", "",
        f"| 指标 | 值 |",
        f"|---|---|",
        f"| 真瑕疵数 | {report.defect_count} |",
        f"| **正常率** | **{report.normal_rate*100:.2f}%** |",
        f"| 门槛 | ≥ {GATE_NORMAL_RATE*100:.0f}% |",
        "",
    ]
    if report.defect_breakdown:
        lines.append("瑕疵类型分布:")
        lines.append("")
        lines.append("| 类型 | 数量 | 说明 |")
        lines.append("|---|---|---|")
        desc = {
            "double_star": "只剩 markdown 装饰符(`**`),内容被吞",
            "empty": "空字符串",
            "placeholder": "占位符文本(摘要缺失/(无)",
            "suspicious_short": "极短且无实质内容",
        }
        for k, v in sorted(report.defect_breakdown.items(), key=lambda x: -x[1]):
            lines.append(f"| {k} | {v} | {desc.get(k, '')} |")
        lines.append("")
    if report.defects:
        lines.append("瑕疵 turn 明细(全部):")
        lines.append("")
        lines.append("| session | turn# | 类型 | narrative 预览 |")
        lines.append("|---|---|---|---|")
        for d in report.defects:
            preview = d.narrative_preview.replace("|", "\\|").replace("\n", " ")[:40]
            lines.append(f"| {d.session_id[:24]}.. | {d.turn_index} | {d.defect_type} | `{preview}` |")
        lines.append("")
    if report.short_narratives:
        lines.append(f"忠实短摘要 {len(report.short_narratives)} 个(原文用户消息本就短,**非瑕疵**):")
        lines.append("")
        for d in report.short_narratives[:5]:
            preview = d.narrative_preview.replace("|", "\\|")[:40]
            lines.append(f"- `{d.session_id[:20]}..` Turn{d.turn_index}: `{preview}`")
        if len(report.short_narratives) > 5:
            lines.append(f"- ... 还有 {len(report.short_narratives)-5} 个")
        lines.append("")

    lines += [
        "## 2. 信息密度(narrative 长度分布)", "",
        f"| 指标 | 值(字符) |",
        f"|---|---|",
        f"| min | {report.length_min} |",
        f"| max | {report.length_max} |",
        f"| mean | {report.length_mean} |",
        f"| P50 | {report.length_p50} |",
        f"| P95 | {report.length_p95} |",
        "",
        "## 3. 回溯链完整率(source_refs 覆盖率)", "",
        f"| 指标 | 值 |",
        f"|---|---|",
        f"| 带 source_refs 的 turn | {report.turns_with_refs}/{report.total_turns} |",
        f"| **覆盖率** | **{report.trace_coverage*100:.2f}%** |",
        f"| 门槛 | = {GATE_TRACE_RATE*100:.0f}% |",
        "",
        "## 4. 因果完整性(瑕疵造成的 turn 链断裂)", "",
        f"- 连续瑕疵 turn(链断裂)处数: **{report.chain_break_count}**",
        "",
        "## 门槛判定", "",
    ]
    if report.gate_passed:
        lines.append(f"**{status}** — 所有门槛通过,可进入 Wave 9/10。")
    else:
        lines.append(f"**{status}** — 未通过:")
        for r in report.gate_reasons:
            lines.append(f"- {r}")
        lines.append("")
        lines.append("> 未通过门槛前,不向 unified_events / 向量库灌新数据。"
                     "需回到 Wave 8.2 修复根因后重跑复评。")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Wave 8.1 压缩叙述质量评估")
    p.add_argument("--write", action="store_true", help="落盘 JSON + MD 报告")
    p.add_argument("--input", type=str, default=str(IN_JSON),
                   help=f"输入 JSON(默认 {IN_JSON.name})")
    args = p.parse_args(argv)

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"[error] 输入文件不存在: {in_path}", file=sys.stderr)
        return 1
    summaries = json.loads(in_path.read_text(encoding="utf-8"))
    report = evaluate(summaries)

    # 控制台摘要
    status = "PASS" if report.gate_passed else "FAIL"
    print(f"[quality] {report.total_sessions} session, {report.total_turns} turn")
    print(f"  正常率: {report.normal_rate*100:.2f}% "
          f"(瑕疵 {report.defect_count}, 门槛 {GATE_NORMAL_RATE*100:.0f}%)")
    print(f"  回溯链覆盖率: {report.trace_coverage*100:.2f}% "
          f"(门槛 {GATE_TRACE_RATE*100:.0f}%)")
    print(f"  信息密度: mean {report.length_mean} / P50 {report.length_p50} / P95 {report.length_p95}")
    print(f"  链断裂: {report.chain_break_count} 处")
    if report.defect_breakdown:
        print(f"  瑕疵分布: {report.defect_breakdown}")
    print(f"  门槛判定: {status}")
    if not report.gate_passed:
        for r in report.gate_reasons:
            print(f"    - {r}")

    if args.write:
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(
            json.dumps(to_serializable(report), ensure_ascii=False, indent=2),
            encoding="utf-8")
        OUT_MD.write_text(write_markdown(report), encoding="utf-8")
        print(f"\n  {OUT_JSON.relative_to(ROOT)}")
        print(f"  {OUT_MD.relative_to(ROOT)}")

    # 未通过门槛退出非 0(可被 CI/流水线捕获)
    return 0 if report.gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
