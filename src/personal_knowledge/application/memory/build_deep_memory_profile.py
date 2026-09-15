"""深层记忆画像构建scripts(Phase 06 Wave 3/4)。

消费 deep_memory_mining.json，输出适合 AI 注入的深层 profile，
并可选生成浅层 vs 深层对比评估。

输出:
  integration/analysis/ai_context/deep_memory_insights.json
  integration/analysis/ai_context/deep_memory_insights.md
  integration/analysis/ai_context/deep_memory_profile.md
  integration/analysis/ai_context/deep_profile_evaluation.md (--evaluate)

运行:
  python integration\\scripts\\build_deep_memory_profile.py
  python integration\\scripts\\build_deep_memory_profile.py --evaluate
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from personal_knowledge.core.common import ensure_dirs, write_json


ROOT = Path(__file__).resolve().parents[4]
AI_DIR = ROOT / "integration" / "analysis" / "ai_context"
MINING_JSON = AI_DIR / "deep_memory_mining.json"
SHALLOW_PROFILE = AI_DIR / "person_profile_v2.md"
OUT_JSON = AI_DIR / "deep_memory_insights.json"
OUT_MD = AI_DIR / "deep_memory_insights.md"
OUT_PROFILE = AI_DIR / "deep_memory_profile.md"
OUT_EVAL = AI_DIR / "deep_profile_evaluation.md"

SECTION_ORDER = [
    ("Long-term patterns", {"stable_preference", "decaying_interest"}),
    ("Tool and workflow evolution", {"tool_migration", "decaying_interest", "contradiction_or_tension"}),
    ("Capability formation paths", {"capability_path"}),
    ("Project/theme clusters", {"project_cluster"}),
    ("Contradictions and stale memories", {"contradiction_or_tension"}),
]


def load_payload(path: Path = MINING_JSON) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"深挖输入不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def split_insights(payload: dict) -> tuple[list[dict], list[dict], list[dict]]:
    insights = payload.get("insights") or []
    include = [item for item in insights if item.get("profile_action") == "include"]
    review = [item for item in insights if item.get("profile_action") == "review"]
    exclude = [item for item in insights if item.get("profile_action") == "exclude"]
    return include, review, exclude


def render_insights_md(include: list[dict], review: list[dict], exclude: list[dict]) -> str:
    lines = [
        "# Deep Memory Insights",
        "",
        f"- 生成时间: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"- include: {len(include)}",
        f"- review: {len(review)}",
        f"- exclude: {len(exclude)}",
        "",
        "## Included Insights",
    ]
    for item in include:
        lines.append(f"- [{item['confidence_level']}] {item['title']} ({item['insight_type']})")
        lines.append(f"  - claim: {item['claim']}")
        lines.append(
            f"  - evidence: {item['evidence_count']} | "
            f"time: {item['time_window']['start']} -> {item['time_window']['end']} | "
            f"relation_avg: {item['relation_strength_avg']:.2f}"
        )
        if item.get("contradictions"):
            lines.append(f"  - contradictions: {'; '.join(item['contradictions'])}")
    lines.extend(["", "## Review List"])
    for item in review:
        lines.append(f"- [{item['confidence_level']}] {item['title']} -> review")
    lines.extend(["", "## Excluded Insights"])
    for item in exclude:
        lines.append(f"- [{item['confidence_level']}] {item['title']} -> exclude")
    return "\n".join(lines)


def render_profile_md(include: list[dict], review: list[dict], exclude: list[dict]) -> str:
    lines = [
        "# Deep Memory Profile",
        "",
        f"- 生成时间: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        "- 用途: 给 agent prompt 注入比 person_profile_v2 更深的模式、演化和反例约束。",
        "- 只包含 strong/moderate 洞察；weak/unsupported 不直接进入正文结论。",
        "",
    ]
    rendered_ids: set[str] = set()
    for title, types in SECTION_ORDER:
        section_rows = [
            item
            for item in include
            if item["insight_type"] in types and item["insight_id"] not in rendered_ids
        ]
        if not section_rows:
            continue
        lines.append(f"## {title}")
        for item in section_rows:
            rendered_ids.add(item["insight_id"])
            lines.append(
                f"- [{item['confidence_level']}] {item['title']}: {item['claim']}"
            )
            lines.append(
                f"  证据 {item['evidence_count']} | 时间 {item['time_window']['start']} -> {item['time_window']['end']} | "
                f"关系强度 {item['relation_strength_avg']:.2f}"
            )
            lines.append(f"  Why it matters: {item['why_it_matters']}")
            if item.get("contradictions"):
                lines.append(f"  限制/反例: {'; '.join(item['contradictions'])}")
            if item.get("evidence_items"):
                refs = []
                for ev in item["evidence_items"][:3]:
                    refs.append(
                        f"{ev.get('source','?')} {str(ev.get('event_time',''))[:10]} {str(ev.get('title',''))[:30]}"
                    )
                lines.append(f"  证据摘要: {'; '.join(refs)}")
        lines.append("")

    lines.append("## Do not over-infer")
    if review:
        for item in review:
            lines.append(f"- {item['title']}: {item['claim']}")
    else:
        lines.append("- 当前没有需要额外 review 的弱洞察。")

    if exclude:
        lines.append("")
        lines.append("## Excluded from Profile")
        for item in exclude:
            lines.append(f"- {item['title']}: {item['claim']}")
    return "\n".join(lines).strip() + "\n"


def evaluate_against_shallow(include: list[dict]) -> str:
    shallow_text = SHALLOW_PROFILE.read_text(encoding="utf-8") if SHALLOW_PROFILE.exists() else ""
    rows = []
    for item in include:
        title = item["title"]
        claim = item["claim"]
        has_pattern = "长期" in claim or "持续" in claim or "衰减" in claim or "路径" in claim or "主题簇" in claim
        has_time = bool(item["time_window"].get("start") and item["time_window"].get("end"))
        has_relation = float(item.get("relation_strength_avg", 0.0)) > 0
        has_limit = bool(item.get("contradictions"))
        evidence_chain = bool(item.get("evidence_items"))
        shallow_overlap = "yes" if title.split()[0] in shallow_text else "partial"
        rows.append(
            {
                "title": title,
                "shallow_overlap": shallow_overlap,
                "upgraded_to_pattern": "yes" if has_pattern else "no",
                "adds_time_evolution": "yes" if has_time else "no",
                "adds_relation_strength": "yes" if has_relation else "no",
                "adds_limits": "yes" if has_limit else "no",
                "keeps_evidence_chain": "yes" if evidence_chain else "no",
            }
        )

    lines = [
        "# Deep Profile Evaluation",
        "",
        f"- 生成时间: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"- 对比对象: {SHALLOW_PROFILE.name} vs {OUT_PROFILE.name}",
        f"- 深层 include 洞察: {len(include)}",
        "",
        "## 对比表",
        "",
        "| 洞察 | shallow_overlap | 升级为 pattern | 增加时间演化 | 增加关系强度 | 增加反例/限制 | 保留证据链 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['title']} | {row['shallow_overlap']} | {row['upgraded_to_pattern']} | "
            f"{row['adds_time_evolution']} | {row['adds_relation_strength']} | "
            f"{row['adds_limits']} | {row['keeps_evidence_chain']} |"
        )

    lines.extend(
        [
            "",
            "## Summary",
            f"- 至少包含 pattern/evolution/contradiction 的洞察数: "
            f"{sum(1 for row in rows if row['upgraded_to_pattern'] == 'yes' or row['adds_limits'] == 'yes')}",
            "- deep profile 只保留 strong/moderate，weak/unsupported 留在 review 或 exclude。",
            "- 若 shallow_overlap 为 yes/partial，但 deep 侧新增了时间演化、关系强度或限制，则不算简单扩写。",
        ]
    )
    return "\n".join(lines)


def build_outputs(evaluate: bool = False) -> list[Path]:
    payload = load_payload()
    include, review, exclude = split_insights(payload)

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "summary": payload.get("summary") or {},
        "include": include,
        "review": review,
        "exclude": exclude,
    }

    ensure_dirs([AI_DIR])
    write_json(OUT_JSON, summary)
    OUT_MD.write_text(render_insights_md(include, review, exclude), encoding="utf-8")
    OUT_PROFILE.write_text(render_profile_md(include, review, exclude), encoding="utf-8")

    outputs = [OUT_JSON, OUT_MD, OUT_PROFILE]
    if evaluate:
        OUT_EVAL.write_text(evaluate_against_shallow(include), encoding="utf-8")
        outputs.append(OUT_EVAL)
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="构建深层记忆画像")
    parser.add_argument("--evaluate", action="store_true", help="额外生成浅层 vs 深层对比评估")
    args = parser.parse_args()

    outputs = build_outputs(evaluate=args.evaluate)
    for path in outputs:
        print(f"已生成: {path}")


if __name__ == "__main__":
    main()
