"""深层记忆图谱挖掘scripts(Phase 06 Wave 1/2)。

只消费通过 readiness gate 的候选主题，输出带证据链、时间窗口、
关系强度和冲突检查的深层洞察候选。

输出:
  integration/analysis/ai_context/deep_memory_mining.json
  integration/analysis/ai_context/deep_memory_mining.md

运行:
  python integration\\scripts\\mine_deep_memory_graph.py --dry-run
  python integration\\scripts\\mine_deep_memory_graph.py --output-json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from personal_knowledge.core.common import ensure_dirs, sha256_text, write_json


ROOT = Path(__file__).resolve().parents[4]
UNIFIED_DB = ROOT / "integration" / "db" / "personal_system.sqlite"
READINESS_MD = ROOT / "integration" / "analysis" / "ai_context" / "memory_depth_readiness.md"
OUT_DIR = ROOT / "integration" / "analysis" / "ai_context"
OUT_JSON = OUT_DIR / "deep_memory_mining.json"
OUT_MD = OUT_DIR / "deep_memory_mining.md"


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


def _load_metadata(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _score_to_level(score: float) -> str:
    if score >= 3.4:
        return "strong"
    if score >= 2.5:
        return "moderate"
    if score >= 1.6:
        return "weak"
    return "unsupported"


@dataclass
class ReadyTopic:
    topic_type: str
    raw_subject: str
    evidence_count: int
    time_span_days: int
    recurrence: int
    relation_strength: float


@dataclass
class Candidate:
    candidate_id: str
    candidate_type: str
    raw_subject: str
    memory_ids: list[str]
    memory_types: list[str]
    subjects: list[str]
    relation: str
    evidence_ids: list[str]
    related_entities: list[dict]
    time_window: dict
    relation_weights: list[float]
    contradictions: list[str]
    metrics: dict


@dataclass
class Insight:
    insight_id: str
    insight_type: str
    title: str
    claim: str
    confidence_level: str
    confidence_score: float
    evidence_items: list[dict]
    evidence_count: int
    related_entities: list[dict]
    time_window: dict
    relation_count: int
    relation_strength_avg: float
    contradiction_count: int
    contradictions: list[str]
    why_it_matters: str
    profile_action: str
    source_candidate: str


def parse_readiness(path: Path = READINESS_MD) -> tuple[list[ReadyTopic], list[str]]:
    """从 readiness Markdown 抽取可深挖候选和阻塞主题。"""
    if not path.exists():
        raise FileNotFoundError(f"readiness 报告不存在: {path}")

    candidates: list[ReadyTopic] = []
    blocked: list[str] = []
    section = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if not line.startswith("- "):
            continue
        text = line[2:].strip()
        if section == "可深挖候选主题":
            subject, _, metrics = text.partition(":")
            parsed = {
                "evidence_count": 0,
                "time_span_days": 0,
                "recurrence": 0,
                "relation_strength": 0.0,
            }
            for part in metrics.split(","):
                part = part.strip()
                if part.startswith("证据 "):
                    parsed["evidence_count"] = int(part[3:].strip())
                elif part.startswith("跨时长 "):
                    parsed["time_span_days"] = int(part[4:].strip().split()[0])
                elif part.startswith("复现 "):
                    parsed["recurrence"] = int(part[3:].strip())
                elif part.startswith("关系强度 "):
                    parsed["relation_strength"] = float(part[5:].strip())
            candidates.append(
                ReadyTopic(
                    topic_type="relation" if " --" in subject else "item",
                    raw_subject=subject.strip(),
                    evidence_count=parsed["evidence_count"],
                    time_span_days=parsed["time_span_days"],
                    recurrence=parsed["recurrence"],
                    relation_strength=parsed["relation_strength"],
                )
            )
        elif section == "暂不可信的浅层主题":
            blocked.append(text)
    return candidates, blocked


def _load_max_event_time(con: sqlite3.Connection) -> datetime | None:
    row = con.execute("SELECT MAX(event_time) FROM unified_events").fetchone()
    return _parse_time(row[0] if row else None)


def _fetch_events(con: sqlite3.Connection, evidence_ids: list[str]) -> list[dict]:
    if not evidence_ids:
        return []
    placeholders = ",".join("?" * len(evidence_ids))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"SELECT event_id, source, event_time, month, title "
        f"FROM unified_events WHERE event_id IN ({placeholders}) "
        f"ORDER BY event_time DESC",
        evidence_ids,
    ).fetchall()
    return [dict(r) for r in rows]


def _build_time_window(events: list[dict]) -> dict:
    points = [dt for dt in (_parse_time(e.get("event_time")) for e in events) if dt is not None]
    months = sorted({str(e.get("month") or "")[:7] for e in events if e.get("month")})
    if not points:
        return {"start": "", "end": "", "days": 0, "months": months}
    return {
        "start": min(points).strftime("%Y-%m-%d"),
        "end": max(points).strftime("%Y-%m-%d"),
        "days": (max(points) - min(points)).days,
        "months": months,
    }


def _neighbor_entities(con: sqlite3.Connection, memory_ids: list[str]) -> list[dict]:
    if not memory_ids:
        return []
    placeholders = ",".join("?" * len(memory_ids))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT mr.relation, mr.strength, "
        "src.memory_id AS from_memory_id, src.subject AS from_subject, src.memory_type AS from_type, "
        "dst.memory_id AS to_memory_id, dst.subject AS to_subject, dst.memory_type AS to_type "
        "FROM memory_relations mr "
        "JOIN memory_items src ON src.memory_id = mr.from_memory_id "
        "JOIN memory_items dst ON dst.memory_id = mr.to_memory_id "
        f"WHERE mr.from_memory_id IN ({placeholders}) OR mr.to_memory_id IN ({placeholders}) "
        "ORDER BY mr.strength DESC, mr.relation, src.subject",
        memory_ids + memory_ids,
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        other_id = row["to_memory_id"] if row["from_memory_id"] in memory_ids else row["from_memory_id"]
        other_subject = row["to_subject"] if row["from_memory_id"] in memory_ids else row["from_subject"]
        other_type = row["to_type"] if row["from_memory_id"] in memory_ids else row["from_type"]
        out.append(
            {
                "memory_id": other_id,
                "subject": other_subject,
                "memory_type": other_type,
                "relation": row["relation"],
                "strength": float(row["strength"] or 0),
            }
        )
    # 去重保序
    seen: set[str] = set()
    deduped: list[dict] = []
    for row in out:
        key = f"{row['memory_id']}|{row['relation']}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _collect_item_candidate(con: sqlite3.Connection, topic: ReadyTopic, max_event_time: datetime | None) -> Candidate:
    con.row_factory = sqlite3.Row
    subject, _, right = topic.raw_subject.partition(" [")
    type_part = right.rstrip("]")
    memory_type, _, subtype = type_part.partition("/")
    row = con.execute(
        "SELECT memory_id, memory_type, memory_subtype, subject, description, metadata, evidence_count "
        "FROM memory_items WHERE subject=? AND memory_type=? AND memory_subtype=?",
        (subject.strip(), memory_type.strip(), subtype.strip()),
    ).fetchone()
    if not row:
        raise ValueError(f"未找到 readiness 候选 memory item: {topic.raw_subject}")
    metadata = _load_metadata(row["metadata"])
    evidence_ids = list(metadata.get("evidence_ids") or [])
    events = _fetch_events(con, evidence_ids)
    time_window = _build_time_window(events)
    related_entities = _neighbor_entities(con, [row["memory_id"]])
    relation_weights = [float(item["strength"]) for item in related_entities]
    contradictions: list[str] = []
    if "declining" in subtype and related_entities:
        strong_neighbors = [item for item in related_entities if item["strength"] >= 0.8]
        if strong_neighbors:
            contradictions.append("图谱仍保留强 workflow 关联，不能把衰减误读为完全停用")
    if max_event_time and time_window["end"]:
        end_dt = _parse_time(time_window["end"])
        if end_dt and (max_event_time - end_dt).days >= 90:
            contradictions.append("最近 90 天缺少新证据，可能已经过时")
    metrics = {
        "evidence_count": len(evidence_ids) or int(row["evidence_count"] or 0),
        "time_span_days": time_window["days"],
        "recurrence_count": len(time_window["months"]),
        "relation_count": len(related_entities),
        "relation_strength_avg": round(sum(relation_weights) / len(relation_weights), 4) if relation_weights else 0.0,
        "contradiction_count": len(contradictions),
    }
    return Candidate(
        candidate_id=sha256_text(f"candidate|{topic.raw_subject}"),
        candidate_type="item",
        raw_subject=topic.raw_subject,
        memory_ids=[row["memory_id"]],
        memory_types=[row["memory_type"]],
        subjects=[row["subject"]],
        relation="",
        evidence_ids=evidence_ids,
        related_entities=related_entities,
        time_window=time_window,
        relation_weights=relation_weights,
        contradictions=contradictions,
        metrics=metrics,
    )


def _collect_relation_candidate(con: sqlite3.Connection, topic: ReadyTopic, max_event_time: datetime | None) -> Candidate:
    con.row_factory = sqlite3.Row
    left, _, tail = topic.raw_subject.partition(" --")
    relation, _, right = tail.partition("--> ")
    relation = relation.strip()
    right = right.strip()
    row = con.execute(
        "SELECT mr.from_memory_id, mr.to_memory_id, mr.strength, "
        "src.subject AS from_subject, src.memory_type AS from_type, src.metadata AS from_metadata, "
        "dst.subject AS to_subject, dst.memory_type AS to_type, dst.metadata AS to_metadata "
        "FROM memory_relations mr "
        "JOIN memory_items src ON src.memory_id = mr.from_memory_id "
        "JOIN memory_items dst ON dst.memory_id = mr.to_memory_id "
        "WHERE src.subject=? AND mr.relation=? AND dst.subject=? "
        "ORDER BY mr.strength DESC LIMIT 1",
        (left.strip(), relation, right),
    ).fetchone()
    if not row:
        raise ValueError(f"未找到 readiness 候选 relation: {topic.raw_subject}")
    left_md = _load_metadata(row["from_metadata"])
    right_md = _load_metadata(row["to_metadata"])
    evidence_ids = list(
        dict.fromkeys((left_md.get("evidence_ids") or []) + (right_md.get("evidence_ids") or []))
    )[:50]
    events = _fetch_events(con, evidence_ids)
    time_window = _build_time_window(events)
    memory_ids = [row["from_memory_id"], row["to_memory_id"]]
    related_entities = _neighbor_entities(con, memory_ids)
    relation_weights = [float(item["strength"]) for item in related_entities]
    contradictions: list[str] = []
    if relation == "same_subject":
        type_pair = {row["from_type"], row["to_type"]}
        if type_pair != {"fact", "project"}:
            contradictions.append("same_subject 目前更像跨类对齐，不一定代表稳定主题")
    if max_event_time and time_window["end"]:
        end_dt = _parse_time(time_window["end"])
        if end_dt and (max_event_time - end_dt).days >= 120:
            contradictions.append("最近 120 天缺少新证据，关系可能已经陈旧")
    metrics = {
        "evidence_count": len(evidence_ids),
        "time_span_days": time_window["days"],
        "recurrence_count": len(time_window["months"]),
        "relation_count": len(related_entities),
        "relation_strength_avg": round(sum(relation_weights) / len(relation_weights), 4) if relation_weights else 0.0,
        "contradiction_count": len(contradictions),
        "edge_strength": float(row["strength"] or 0),
    }
    return Candidate(
        candidate_id=sha256_text(f"candidate|{topic.raw_subject}"),
        candidate_type="relation",
        raw_subject=topic.raw_subject,
        memory_ids=memory_ids,
        memory_types=[row["from_type"], row["to_type"]],
        subjects=[row["from_subject"], row["to_subject"]],
        relation=relation,
        evidence_ids=evidence_ids,
        related_entities=related_entities,
        time_window=time_window,
        relation_weights=relation_weights,
        contradictions=contradictions,
        metrics=metrics,
    )


def load_candidates(con: sqlite3.Connection, topics: list[ReadyTopic]) -> list[Candidate]:
    """把 readiness 候选解析成内部深挖输入结构。"""
    max_event_time = _load_max_event_time(con)
    out: list[Candidate] = []
    for topic in topics:
        if topic.topic_type == "item":
            out.append(_collect_item_candidate(con, topic, max_event_time))
        else:
            out.append(_collect_relation_candidate(con, topic, max_event_time))
    return out


def _top_evidence(events: list[dict], limit: int = 5) -> list[dict]:
    out = []
    for row in events[:limit]:
        out.append(
            {
                "event_id": row["event_id"],
                "source": row["source"],
                "event_time": row["event_time"],
                "title": row["title"] or "(无标题)",
            }
        )
    return out


def _insight_score(candidate: Candidate, boost: float = 0.0, penalty: float = 0.0) -> tuple[float, str]:
    metrics = candidate.metrics
    score = 0.0
    evidence_count = int(metrics.get("evidence_count", 0))
    if evidence_count >= 100:
        score += 1.0
    elif evidence_count >= 10:
        score += 0.8
    elif evidence_count >= 3:
        score += 0.5

    span = int(metrics.get("time_span_days", 0))
    if span >= 90:
        score += 1.0
    elif span >= 30:
        score += 0.8
    elif span >= 7:
        score += 0.5

    recurrence = int(metrics.get("recurrence_count", 0))
    if recurrence >= 4:
        score += 1.0
    elif recurrence >= 2:
        score += 0.7
    elif recurrence >= 1:
        score += 0.3

    rel_avg = float(metrics.get("relation_strength_avg", 0.0))
    if rel_avg >= 0.85:
        score += 1.0
    elif rel_avg >= 0.7:
        score += 0.7
    elif rel_avg >= 0.5:
        score += 0.4

    score += boost
    score -= penalty + min(int(metrics.get("contradiction_count", 0)), 2) * 0.35
    score = round(max(score, 0.0), 2)
    return score, _score_to_level(score)


def _make_insight(
    candidate: Candidate,
    insight_type: str,
    title: str,
    claim: str,
    why_it_matters: str,
    boost: float = 0.0,
    penalty: float = 0.0,
) -> Insight:
    con = sqlite3.connect(UNIFIED_DB)
    events = _fetch_events(con, candidate.evidence_ids)
    con.close()
    score, level = _insight_score(candidate, boost=boost, penalty=penalty)
    action = "include" if level in {"strong", "moderate"} else ("review" if level == "weak" else "exclude")
    return Insight(
        insight_id=sha256_text(f"insight|{candidate.raw_subject}|{insight_type}|{title}"),
        insight_type=insight_type,
        title=title,
        claim=claim,
        confidence_level=level,
        confidence_score=score,
        evidence_items=_top_evidence(events, limit=5),
        evidence_count=int(candidate.metrics.get("evidence_count", 0)),
        related_entities=candidate.related_entities[:8],
        time_window=candidate.time_window,
        relation_count=int(candidate.metrics.get("relation_count", 0)),
        relation_strength_avg=float(candidate.metrics.get("relation_strength_avg", 0.0)),
        contradiction_count=int(candidate.metrics.get("contradiction_count", 0)),
        contradictions=list(candidate.contradictions),
        why_it_matters=why_it_matters,
        profile_action=action,
        source_candidate=candidate.raw_subject,
    )


def mine_insights(candidates: list[Candidate]) -> list[Insight]:
    """根据候选主题生成深层洞察候选。"""
    out: list[Insight] = []
    for candidate in candidates:
        metrics = candidate.metrics
        evidence = int(metrics.get("evidence_count", 0))
        months = candidate.time_window.get("months", [])

        if candidate.candidate_type == "item":
            subtype = candidate.raw_subject.split("[", 1)[-1].rstrip("]")
            subject = candidate.subjects[0]
            if "tooling/declining_primary" in subtype:
                out.append(
                    _make_insight(
                        candidate,
                        insight_type="decaying_interest",
                        title=f"{subject} 从主力工具转入衰减阶段",
                        claim=(
                            f"{subject} 不是一次性掉线，而是跨 {len(months)} 个活跃月、"
                            f"{candidate.time_window.get('days', 0)} 天后进入持续衰减。"
                        ),
                        why_it_matters="回答工具偏好和工作流迁移时，不应再把它当成当前主力默认项。",
                        boost=0.5,
                    )
                )
                out.append(
                    _make_insight(
                        candidate,
                        insight_type="contradiction_or_tension",
                        title=f"{subject} 使用衰减但 workflow 关联仍在",
                        claim=(
                            f"{subject} 的直接使用在衰减，但图谱里仍保留 {len(candidate.related_entities)} 条相关工作流连接，"
                            "说明它更像旧主力而非完全废弃。"
                        ),
                        why_it_matters="AI 回答时应把它视为历史路径的一部分，而不是彻底排除。",
                        boost=0.2,
                    )
                )

        elif candidate.relation == "same_subject":
            subject = candidate.subjects[0]
            out.append(
                _make_insight(
                    candidate,
                    insight_type="project_cluster",
                    title=f"{subject} 同时是环境事实和项目对象",
                    claim=(
                        f"{subject} 跨 fact/project 两类记忆对齐，说明它不是零散关键词，"
                        "而是长期系统中的稳定锚点。"
                    ),
                    why_it_matters="AI 在解释知识管理或项目上下文时，可以把它视为长期结构，而不是一次性话题。",
                    boost=0.35,
                )
            )
            out.append(
                _make_insight(
                    candidate,
                    insight_type="stable_preference",
                    title=f"{subject} 呈现跨类型稳定偏好",
                    claim=(
                        f"{subject} 在 {candidate.time_window.get('days', 0)} 天内重复出现，并同时落在项目与环境层，"
                        "说明它具备稳定偏好属性。"
                    ),
                    why_it_matters="这类对象适合进入个性化上下文，但仍要附带证据约束。",
                    boost=0.15,
                )
            )

        elif candidate.relation == "relates_to_topic":
            project = candidate.subjects[0]
            topic = candidate.subjects[1]
            out.append(
                _make_insight(
                    candidate,
                    insight_type="project_cluster",
                    title=f"{project} 长期归属于 {topic} 主题簇",
                    claim=(
                        f"{project} 在 {len(months)} 个活跃月里稳定落到 {topic}，"
                        "这不是单次共现，而是持续主题归属。"
                    ),
                    why_it_matters="AI 在回忆项目、推荐后续任务或构造领域上下文时，可以把它并入稳定主题簇。",
                    boost=0.35,
                )
            )
            out.append(
                _make_insight(
                    candidate,
                    insight_type="capability_path",
                    title=f"{project} 体现开发能力形成路径",
                    claim=(
                        f"{project} 与 {topic} 的连接跨 {candidate.time_window.get('days', 0)} 天复现，"
                        "说明它更像能力积累路径中的长期节点，而不是一次性小项目。"
                    ),
                    why_it_matters="这有助于 AI 解释用户为什么会反复回到某一类工程主题。",
                    boost=0.2,
                )
            )

        # 兜底：证据足够但没触发专门模板时，至少保留一条 review 洞察
        if evidence >= 3 and not any(item.source_candidate == candidate.raw_subject for item in out):
            out.append(
                _make_insight(
                    candidate,
                    insight_type="project_cluster",
                    title=f"{candidate.raw_subject} 具备进一步深挖价值",
                    claim="该候选已通过 readiness gate，但暂未命中更具体模板，建议保留 review。",
                    why_it_matters="避免把已通过准入的主题静默丢弃。",
                    penalty=0.4,
                )
            )
    return out


def render_markdown(payload: dict) -> str:
    lines = [
        "# Deep Memory Mining",
        "",
        f"- 生成时间: {payload['generated_at']}",
        f"- readiness 候选: {payload['summary']['ready_topics']}",
        f"- 阻塞主题: {payload['summary']['blocked_topics']}",
        f"- 洞察候选: {payload['summary']['insight_count']}",
        "",
        "## 候选主题",
    ]
    for item in payload["candidates"]:
        lines.append(
            f"- {item['raw_subject']}: 证据 {item['metrics']['evidence_count']}, "
            f"时长 {item['metrics']['time_span_days']} 天, "
            f"复现 {item['metrics']['recurrence_count']}, "
            f"关系 {item['metrics']['relation_strength_avg']:.2f}, "
            f"冲突 {item['metrics']['contradiction_count']}"
        )
    lines.extend(["", "## 深层洞察候选"])
    for insight in payload["insights"]:
        lines.append(
            f"- [{insight['confidence_level']}] {insight['title']} ({insight['insight_type']})"
        )
        lines.append(f"  - claim: {insight['claim']}")
        lines.append(
            f"  - evidence: {insight['evidence_count']} | "
            f"time_window: {insight['time_window']['start']} -> {insight['time_window']['end']} | "
            f"relation_avg: {insight['relation_strength_avg']:.2f}"
        )
        if insight["contradictions"]:
            lines.append(f"  - contradictions: {'; '.join(insight['contradictions'])}")
        lines.append(f"  - action: {insight['profile_action']}")
    lines.extend(["", "## 跳过的浅层主题"])
    for item in payload["blocked_topics"]:
        lines.append(f"- {item}")
    return "\n".join(lines)


def build_payload() -> dict:
    topics, blocked = parse_readiness()
    con = sqlite3.connect(UNIFIED_DB)
    candidates = load_candidates(con, topics)
    con.close()
    insights = mine_insights(candidates)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "summary": {
            "ready_topics": len(topics),
            "blocked_topics": len(blocked),
            "candidate_count": len(candidates),
            "insight_count": len(insights),
            "include_count": sum(1 for item in insights if item.profile_action == "include"),
            "review_count": sum(1 for item in insights if item.profile_action == "review"),
            "exclude_count": sum(1 for item in insights if item.profile_action == "exclude"),
        },
        "candidates": [asdict(item) for item in candidates],
        "insights": [asdict(item) for item in insights],
        "blocked_topics": blocked,
    }
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="深层记忆图谱挖掘")
    parser.add_argument("--dry-run", action="store_true", help="只打印准入候选和跳过原因，不写文件")
    parser.add_argument("--output-json", action="store_true", help="写出 JSON/Markdown 结果")
    args = parser.parse_args()

    payload = build_payload()
    if args.dry_run:
        print("Phase 06 准入候选:")
        for item in payload["candidates"]:
            print(
                f"  [OK] {item['raw_subject']} | evidence={item['metrics']['evidence_count']} "
                f"| span={item['metrics']['time_span_days']}d | recurrence={item['metrics']['recurrence_count']} "
                f"| rel_avg={item['metrics']['relation_strength_avg']:.2f}"
            )
        print("\n跳过的浅层主题:")
        for item in payload["blocked_topics"][:12]:
            print(f"  [SKIP] {item}")
        print(f"\n洞察候选: {payload['summary']['insight_count']}")
        return

    ensure_dirs([OUT_DIR])
    write_json(OUT_JSON, payload)
    OUT_MD.write_text(render_markdown(payload), encoding="utf-8")
    print(f"已生成: {OUT_JSON}")
    print(f"已生成: {OUT_MD}")


if __name__ == "__main__":
    main()
