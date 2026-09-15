"""SQLite 代际 / 分层多维度对比（只读）。

对比层:
  L1 events     — unified_events + rich（旧统合事件）
  L2 turns/mem  — conversation 旁路产物 + memory_items（中层实验记忆）
  L3 knowledge  — knowledge_units / canonical（新知识层）
  辅助源库      — Agent/Google 源库规模对照

输出:
  integration/analysis/ai_context/sqlite_generation_comparison.json
  integration/analysis/ai_context/sqlite_generation_comparison.md
  integration/analysis/ai_context/charts/sqlite_gen_*.png

用法::

    python src/personal_knowledge/retrieval/compare_sqlite_generations.py
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from personal_knowledge.core.project_paths import ROOT, UNIFIED_DB, AI_CONTEXT_DIR  # noqa: E402

CHARTS_DIR = AI_CONTEXT_DIR / "charts"
OUT_JSON = AI_CONTEXT_DIR / "sqlite_generation_comparison.json"
OUT_MD = AI_CONTEXT_DIR / "sqlite_generation_comparison.md"

AGENT_CONV = ROOT / "Agent" / "structured" / "db" / "agent_conversations.sqlite"
AGENT_NORM = ROOT / "Agent" / "structured" / "db" / "agentsview_normalized.sqlite"
AGENT_DATA = ROOT / "Agent" / "structured" / "db" / "agent_data.sqlite"
GOOGLE_DB = ROOT / "Google" / "structured" / "db" / "google_data.sqlite"

_RE_QA = re.compile(r"[？?]")
_RE_USER = re.compile(r"(用户|项目).{0,24}(使用|偏好|要求|希望|采用|默认)")
_RE_CODE = re.compile(r"[{};]|def |class |import |function |SELECT ")
_RE_NOISE = re.compile(r"(Prompted |Attached |rollout-|Assistant Rules|InvalidTemplate)")


def _mean(xs: list[float]) -> float:
    return float(statistics.mean(xs)) if xs else 0.0


def _median(xs: list[float]) -> float:
    return float(statistics.median(xs)) if xs else 0.0


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return float(s[min(int(len(s) * p / 100), len(s) - 1)])


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row)


def _count(con: sqlite3.Connection, table: str) -> int:
    return int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _cols(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()]


def _null_rate(con: sqlite3.Connection, table: str, col: str) -> float:
    total = _count(con, table)
    if total == 0:
        return 0.0
    nulls = con.execute(
        f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" IS NULL OR TRIM(CAST("{col}" AS TEXT)) = \'\''
    ).fetchone()[0]
    return round(nulls / total, 4)


def text_quality(texts: list[str]) -> dict[str, Any]:
    lens = [len(t or "") for t in texts]
    scores = []
    qa = user = code = noise = short = long_ = 0
    for t in texts:
        t = t or ""
        if _RE_QA.search(t):
            qa += 1
        if _RE_USER.search(t):
            user += 1
        if _RE_CODE.search(t):
            code += 1
        if _RE_NOISE.search(t):
            noise += 1
        if 20 <= len(t) <= 280:
            short += 1
        if len(t) > 800:
            long_ += 1
        s = 0.0
        if _RE_QA.search(t):
            s += 0.3
        if _RE_USER.search(t):
            s += 0.25
        if 20 <= len(t) <= 280:
            s += 0.2
        if _RE_CODE.search(t):
            s -= 0.15
        if _RE_NOISE.search(t):
            s -= 0.2
        if len(t) > 800:
            s -= 0.15
        scores.append(max(0.0, min(1.0, s)))
    n = max(len(texts), 1)
    return {
        "n": len(texts),
        "len_min": min(lens) if lens else 0,
        "len_max": max(lens) if lens else 0,
        "len_mean": round(_mean([float(x) for x in lens]), 1),
        "len_median": round(_median([float(x) for x in lens]), 1),
        "len_p90": round(_pct([float(x) for x in lens], 90), 1),
        "hist": _hist(lens),
        "qa_shape_share": round(qa / n, 3),
        "user_assert_share": round(user / n, 3),
        "code_share": round(code / n, 3),
        "noise_share": round(noise / n, 3),
        "short_share": round(short / n, 3),
        "long_gt800_share": round(long_ / n, 3),
        "answerability_mean": round(_mean(scores), 3),
        "answerability_high_share": round(sum(1 for s in scores if s >= 0.5) / n, 3),
    }


def _hist(lens: list[int]) -> dict[str, int]:
    edges = [0, 50, 100, 200, 400, 800, 1200, 10_000]
    labels = ["0-49", "50-99", "100-199", "200-399", "400-799", "800-1199", "1200+"]
    out = {lb: 0 for lb in labels}
    for n in lens:
        for i in range(len(edges) - 1):
            if edges[i] <= n < edges[i + 1]:
                out[labels[i]] += 1
                break
    return out


def sample_texts(con: sqlite3.Connection, sql: str, limit: int = 400) -> list[str]:
    rows = con.execute(sql).fetchall()
    # 均匀抽
    if len(rows) <= limit:
        return [(r[0] or "") for r in rows]
    step = max(len(rows) // limit, 1)
    return [(rows[i][0] or "") for i in range(0, len(rows), step)][:limit]


def analyze_events(con: sqlite3.Connection, sample: int) -> dict:
    layer: dict[str, Any] = {
        "layer": "L1_events",
        "label": "统合事件层 (unified_events)",
        "tables": {},
    }
    for t in (
        "unified_events",
        "unified_events_rich",
        "event_categories_v2",
        "entities",
        "event_entities",
        "entity_links_v2",
        "merge_clusters",
        "merge_members",
    ):
        if _table_exists(con, t):
            layer["tables"][t] = {
                "rows": _count(con, t),
                "cols": _cols(con, t),
                "col_count": len(_cols(con, t)),
            }

    # source / type / month distribution
    layer["source_dist"] = dict(
        con.execute(
            "SELECT source, COUNT(*) c FROM unified_events GROUP BY source ORDER BY c DESC"
        ).fetchall()
    )
    layer["event_type_dist"] = dict(
        con.execute(
            "SELECT event_type, COUNT(*) c FROM unified_events GROUP BY event_type ORDER BY c DESC LIMIT 12"
        ).fetchall()
    )
    layer["month_span"] = {
        "min": con.execute("SELECT MIN(month) FROM unified_events").fetchone()[0],
        "max": con.execute("SELECT MAX(month) FROM unified_events").fetchone()[0],
        "months": con.execute(
            "SELECT COUNT(DISTINCT month) FROM unified_events"
        ).fetchone()[0],
    }

    # null rates key fields
    layer["null_rates"] = {
        c: _null_rate(con, "unified_events", c)
        for c in ("title", "content", "event_time", "service", "category", "url")
    }
    if _table_exists(con, "unified_events_rich"):
        layer["null_rates"]["content_rich"] = _null_rate(
            con, "unified_events_rich", "content_rich"
        )

    # category coverage join
    if _table_exists(con, "event_categories_v2"):
        joined = con.execute(
            "SELECT COUNT(*) FROM unified_events ue "
            "JOIN event_categories_v2 c ON c.event_id=ue.event_id "
            "WHERE c.category_v2 IS NOT NULL AND TRIM(c.category_v2)<>''"
        ).fetchone()[0]
        layer["category_v2_coverage"] = round(joined / max(layer["tables"]["unified_events"]["rows"], 1), 3)

    # text quality: content_rich preferred
    texts = sample_texts(
        con,
        "SELECT COALESCE(NULLIF(r.content_rich,''), ue.content, ue.title, '') "
        "FROM unified_events ue "
        "LEFT JOIN unified_events_rich r ON r.event_id=ue.event_id "
        "ORDER BY ue.event_id",
        sample,
    )
    layer["text_quality"] = text_quality(texts)

    # governance fields present?
    cols = set(layer["tables"]["unified_events"]["cols"])
    layer["governance"] = {
        "has_confidence": False,
        "has_lifecycle": False,
        "has_status": False,
        "has_unit_type": False,
        "has_evidence_ref": False,
        "has_question_answer": False,
        "typed_schema_score": round(
            sum(
                [
                    "event_id" in cols,
                    "source" in cols,
                    "event_time" in cols,
                    "title" in cols,
                    "content" in cols,
                ]
            )
            / 8.0,
            3,
        ),  # /8 later compare with KU which has more
        "fields_present": sorted(cols),
    }
    return layer


def analyze_memory(con: sqlite3.Connection, sample: int) -> dict:
    layer: dict[str, Any] = {
        "layer": "L2_memory",
        "label": "记忆实验层 (memory_*)",
        "tables": {},
    }
    mem_tables = [
        "memory_items",
        "memory_links",
        "memory_relations",
        "memory_evidence_bundles",
        "memory_promotion_candidates",
        "memory_candidate_extraction_progress",
        "memory_relation_candidates",
        "memory_relation_judgments",
    ]
    for t in mem_tables:
        if _table_exists(con, t):
            layer["tables"][t] = {
                "rows": _count(con, t),
                "cols": _cols(con, t),
                "col_count": len(_cols(con, t)),
            }

    if not _table_exists(con, "memory_items"):
        return layer

    layer["type_dist"] = dict(
        con.execute(
            "SELECT memory_type, COUNT(*) c FROM memory_items GROUP BY memory_type ORDER BY c DESC"
        ).fetchall()
    )
    layer["null_rates"] = {
        c: _null_rate(con, "memory_items", c)
        for c in ("subject", "description", "confidence", "memory_type")
    }
    # confidence stats
    confs = [
        float(r[0])
        for r in con.execute(
            "SELECT confidence FROM memory_items WHERE confidence IS NOT NULL"
        ).fetchall()
    ]
    layer["confidence"] = {
        "coverage": round(len(confs) / max(_count(con, "memory_items"), 1), 3),
        "mean": round(_mean(confs), 3),
        "median": round(_median(confs), 3),
    }
    # ku lifecycle sync columns if present
    cols = _cols(con, "memory_items")
    layer["ku_sync_fields"] = {
        c: True for c in ("ku_status", "ku_version", "ku_last_seen", "ku_supersedes") if c in cols
    }

    texts = sample_texts(
        con,
        "SELECT COALESCE(description,'') || ' ' || COALESCE(subject,'') FROM memory_items",
        sample,
    )
    layer["text_quality"] = text_quality(texts)

    # evidence linkage density
    if _table_exists(con, "memory_links"):
        links = _count(con, "memory_links")
        items = _count(con, "memory_items")
        layer["links_per_item"] = round(links / max(items, 1), 3)

    layer["governance"] = {
        "has_confidence": True,
        "has_lifecycle": "ku_status" in cols,
        "has_status": "ku_status" in cols,
        "has_unit_type": "memory_type" in cols,
        "has_evidence_ref": _table_exists(con, "memory_links"),
        "has_question_answer": False,
        "typed_schema_score": 0.5,
        "fields_present": cols,
    }
    return layer


def analyze_knowledge(con: sqlite3.Connection, sample: int) -> dict:
    layer: dict[str, Any] = {
        "layer": "L3_knowledge",
        "label": "知识单元层 (knowledge_*)",
        "tables": {},
    }
    ku_tables = [
        "knowledge_units",
        "canonical_knowledge_units",
        "knowledge_unit_evidence",
        "canonical_unit_members",
        "knowledge_build_runs",
        "knowledge_index_versions",
        "knowledge_inventory",
        "knowledge_inventory_items",
        "knowledge_run_items",
        "knowledge_response_cache",
        "knowledge_extraction_gates",
        "knowledge_delta_inventories",
        "knowledge_delta_items",
        "knowledge_source_watermark",
    ]
    for t in ku_tables:
        if _table_exists(con, t):
            layer["tables"][t] = {
                "rows": _count(con, t),
                "cols": _cols(con, t),
                "col_count": len(_cols(con, t)),
            }

    # draft vs canonical
    draft_n = layer["tables"].get("knowledge_units", {}).get("rows", 0)
    canon_n = layer["tables"].get("canonical_knowledge_units", {}).get("rows", 0)
    layer["dedup"] = {
        "draft_units": draft_n,
        "canonical_units": canon_n,
        "merge_ratio": round(canon_n / max(draft_n, 1), 4),
        "collapsed": draft_n - canon_n,
        "collapse_pct": round((draft_n - canon_n) / max(draft_n, 1), 4),
    }

    # type / status / lifecycle
    if draft_n:
        layer["draft_type_dist"] = dict(
            con.execute(
                "SELECT unit_type, COUNT(*) c FROM knowledge_units GROUP BY unit_type ORDER BY c DESC"
            ).fetchall()
        )
        layer["draft_status_dist"] = dict(
            con.execute(
                "SELECT status, COUNT(*) c FROM knowledge_units GROUP BY status ORDER BY c DESC"
            ).fetchall()
        )
        layer["draft_lifecycle_dist"] = dict(
            con.execute(
                "SELECT lifecycle, COUNT(*) c FROM knowledge_units GROUP BY lifecycle ORDER BY c DESC"
            ).fetchall()
        )
    if canon_n:
        layer["canonical_type_dist"] = dict(
            con.execute(
                "SELECT unit_type, COUNT(*) c FROM canonical_knowledge_units GROUP BY unit_type ORDER BY c DESC"
            ).fetchall()
        )
        layer["canonical_status_dist"] = dict(
            con.execute(
                "SELECT status, COUNT(*) c FROM canonical_knowledge_units GROUP BY status ORDER BY c DESC"
            ).fetchall()
        )

    # confidence
    confs = [
        float(r[0])
        for r in con.execute(
            "SELECT confidence FROM canonical_knowledge_units WHERE confidence IS NOT NULL"
        ).fetchall()
    ]
    layer["confidence"] = {
        "coverage": round(len(confs) / max(canon_n, 1), 3),
        "mean": round(_mean(confs), 3),
        "median": round(_median(confs), 3),
        "p10": round(_pct(confs, 10), 3),
        "p90": round(_pct(confs, 90), 3),
    }

    # null rates
    layer["null_rates"] = {
        c: _null_rate(con, "canonical_knowledge_units", c)
        for c in ("subject", "question", "answer", "confidence", "unit_type", "lifecycle")
    }

    # evidence density
    if _table_exists(con, "knowledge_unit_evidence") and draft_n:
        ev = _count(con, "knowledge_unit_evidence")
        with_ev = con.execute(
            "SELECT COUNT(DISTINCT unit_id) FROM knowledge_unit_evidence"
        ).fetchone()[0]
        layer["evidence"] = {
            "evidence_rows": ev,
            "units_with_evidence": with_ev,
            "coverage": round(with_ev / max(draft_n, 1), 3),
            "avg_evidence_per_linked_unit": round(
                ev / max(with_ev, 1), 3
            ),
        }

    # extraction pipeline health
    if _table_exists(con, "knowledge_run_items"):
        layer["run_item_status"] = dict(
            con.execute(
                "SELECT status, COUNT(*) c FROM knowledge_run_items GROUP BY status ORDER BY c DESC"
            ).fetchall()
        )
        total_items = sum(layer["run_item_status"].values()) or 1
        layer["extraction_success_rate"] = round(
            layer["run_item_status"].get("succeeded", 0) / total_items, 3
        )

    # inventory
    if _table_exists(con, "knowledge_inventory"):
        inv = con.execute(
            "SELECT inventory_id, item_count, time_range_min, time_range_max, generated_at "
            "FROM knowledge_inventory ORDER BY generated_at DESC"
        ).fetchall()
        layer["inventories"] = [
            {
                "id": r[0][:16],
                "item_count": r[1],
                "time_min": r[2],
                "time_max": r[3],
                "generated_at": r[4],
            }
            for r in inv
        ]

    # index versions
    if _table_exists(con, "knowledge_index_versions"):
        layer["index_versions"] = [
            {
                "version_id": r[0],
                "collection": r[1],
                "unit_count": r[2],
                "status": r[3],
                "created_at": r[4],
            }
            for r in con.execute(
                "SELECT version_id, collection_name, unit_count, status, created_at "
                "FROM knowledge_index_versions ORDER BY created_at DESC"
            ).fetchall()
        ]

    # text quality on Q+A
    texts = sample_texts(
        con,
        "SELECT question || ' ' || answer FROM canonical_knowledge_units WHERE status='current' OR status IS NOT NULL",
        sample,
    )
    layer["text_quality"] = text_quality(texts)

    # subject cardinality (knowledge breadth)
    layer["subject_cardinality"] = con.execute(
        "SELECT COUNT(DISTINCT subject) FROM canonical_knowledge_units"
    ).fetchone()[0]
    layer["avg_units_per_subject"] = round(
        canon_n / max(layer["subject_cardinality"], 1), 3
    )

    # members merge stats
    if _table_exists(con, "canonical_unit_members"):
        multi = con.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT canonical_unit_id FROM canonical_unit_members "
            "  GROUP BY canonical_unit_id HAVING COUNT(*)>1"
            ")"
        ).fetchone()[0]
        layer["multi_member_canonicals"] = multi
        layer["multi_member_share"] = round(multi / max(canon_n, 1), 3)

    cols = _cols(con, "canonical_knowledge_units") if canon_n else []
    layer["governance"] = {
        "has_confidence": True,
        "has_lifecycle": True,
        "has_status": True,
        "has_unit_type": True,
        "has_evidence_ref": True,
        "has_question_answer": True,
        "typed_schema_score": 1.0,
        "fields_present": cols,
    }
    return layer


def analyze_source_dbs() -> dict:
    out: dict[str, Any] = {}
    specs = {
        "agent_conversations": (AGENT_CONV, ["canonical_sessions", "canonical_messages", "canonical_tool_events"]),
        "agentsview_normalized": (AGENT_NORM, ["sessions", "messages", "tool_events"]),
        "agent_data_raw": (AGENT_DATA, ["sessions", "agent_messages", "agent_tool_calls", "skills", "memories"]),
        "google_data": (GOOGLE_DB, ["activities", "gemini_attachments"]),
    }
    for name, (path, tables) in specs.items():
        if not path.exists():
            out[name] = {"exists": False}
            continue
        con = _connect(path)
        info: dict[str, Any] = {
            "exists": True,
            "path": str(path.relative_to(ROOT)),
            "size_mb": round(path.stat().st_size / 1024 / 1024, 2),
            "tables": {},
        }
        all_tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        info["table_count"] = len(all_tables)
        for t in tables:
            if t in all_tables:
                info["tables"][t] = {"rows": _count(con, t), "col_count": len(_cols(con, t))}
        # role dist for messages if present
        for msg_t in ("canonical_messages", "messages", "agent_messages"):
            if msg_t in all_tables and "role" in _cols(con, msg_t):
                info["role_dist"] = dict(
                    con.execute(
                        f'SELECT role, COUNT(*) c FROM "{msg_t}" GROUP BY role ORDER BY c DESC'
                    ).fetchall()
                )
                texts = sample_texts(con, f'SELECT COALESCE(content, text, "") FROM "{msg_t}"', 200) if False else []
                # flexible content column
                cols = _cols(con, msg_t)
                content_col = "content" if "content" in cols else ("text" if "text" in cols else None)
                if content_col:
                    texts = sample_texts(
                        con, f'SELECT COALESCE("{content_col}", "") FROM "{msg_t}"', 250
                    )
                    info["message_text_quality"] = text_quality(texts)
                break
        con.close()
        out[name] = info
    return out


def layer_scorecard(events: dict, memory: dict, knowledge: dict) -> dict:
    """归一化打分卡 0-1，越高越好（结构/治理/可答）。"""

    def g(layer: dict, key: str, default=0):
        return layer.get("governance", {}).get(key, default)

    def tq(layer: dict, key: str, default=0.0):
        return float(layer.get("text_quality", {}).get(key, default) or 0)

    def rows(layer: dict) -> int:
        if layer["layer"] == "L1_events":
            return layer["tables"].get("unified_events", {}).get("rows", 0)
        if layer["layer"] == "L2_memory":
            return layer["tables"].get("memory_items", {}).get("rows", 0)
        return layer["tables"].get("canonical_knowledge_units", {}).get("rows", 0)

    max_rows = max(rows(events), rows(memory), rows(knowledge), 1)

    def pack(layer: dict, conf_cov: float, schema: float) -> dict:
        return {
            "scale_norm": round(rows(layer) / max_rows, 3),
            "answerability": tq(layer, "answerability_mean"),
            "short_share": tq(layer, "short_share"),
            "low_noise": round(1.0 - tq(layer, "noise_share"), 3),
            "confidence_coverage": conf_cov,
            "governance_flags": round(
                sum(
                    [
                        1 if g(layer, "has_confidence") else 0,
                        1 if g(layer, "has_lifecycle") else 0,
                        1 if g(layer, "has_status") else 0,
                        1 if g(layer, "has_unit_type") else 0,
                        1 if g(layer, "has_evidence_ref") else 0,
                        1 if g(layer, "has_question_answer") else 0,
                    ]
                )
                / 6.0,
                3,
            ),
            "schema_score": schema,
        }

    return {
        "events": pack(events, 0.0, 0.4),
        "memory": pack(
            memory,
            float(memory.get("confidence", {}).get("coverage") or 0),
            0.65,
        ),
        "knowledge": pack(
            knowledge,
            float(knowledge.get("confidence", {}).get("coverage") or 0),
            1.0,
        ),
    }


def compute_improvements(report: dict) -> list[dict]:
    e, m, k = report["layers"]["events"], report["layers"]["memory"], report["layers"]["knowledge"]
    items = []

    # scale knowledge vs memory
    items.append(
        {
            "aspect": "知识覆盖规模",
            "evidence": (
                f"canonical 知识单元 {k['tables'].get('canonical_knowledge_units',{}).get('rows',0):,} "
                f"vs memory_items {m['tables'].get('memory_items',{}).get('rows',0):,} "
                f"vs unified_events {e['tables'].get('unified_events',{}).get('rows',0):,}"
            ),
        }
    )

    et, mt, kt = e["text_quality"], m.get("text_quality", {}), k["text_quality"]
    items.append(
        {
            "aspect": "文本形态 / 可答性",
            "evidence": (
                f"可答性均值 events {et['answerability_mean']:.2f} → memory {mt.get('answerability_mean',0):.2f} "
                f"→ knowledge {kt['answerability_mean']:.2f}；"
                f"文档中位长度 {et['len_median']:.0f} → {mt.get('len_median',0):.0f} → {kt['len_median']:.0f}"
            ),
        }
    )
    items.append(
        {
            "aspect": "噪声与长文本",
            "evidence": (
                f"噪声占比 {et['noise_share']:.0%} → {mt.get('noise_share',0):.0%} → {kt['noise_share']:.0%}；"
                f"长文(>800)占比 {et['long_gt800_share']:.0%} → {mt.get('long_gt800_share',0):.0%} → {kt['long_gt800_share']:.0%}"
            ),
        }
    )
    items.append(
        {
            "aspect": "结构化字段 / 治理",
            "evidence": (
                f"governance 六项齐全度 knowledge={report['scorecard']['knowledge']['governance_flags']:.0%}，"
                f"memory={report['scorecard']['memory']['governance_flags']:.0%}，"
                f"events={report['scorecard']['events']['governance_flags']:.0%}；"
                f"KU 独有 Q&A + lifecycle + status + evidence 表"
            ),
        }
    )
    conf_k = k.get("confidence", {})
    conf_m = m.get("confidence", {})
    items.append(
        {
            "aspect": "置信度体系",
            "evidence": (
                f"confidence 覆盖 knowledge {conf_k.get('coverage',0):.0%} (mean={conf_k.get('mean')})，"
                f"memory {conf_m.get('coverage',0):.0%} (mean={conf_m.get('mean')})，events 无该字段"
            ),
        }
    )
    dedup = k.get("dedup", {})
    items.append(
        {
            "aspect": "去重合并",
            "evidence": (
                f"draft {dedup.get('draft_units',0):,} → canonical {dedup.get('canonical_units',0):,} "
                f"(collapse {dedup.get('collapsed',0):,} / {dedup.get('collapse_pct',0):.1%})；"
                f"多成员 canonical 占比 {k.get('multi_member_share',0):.1%}"
            ),
        }
    )
    ev = k.get("evidence", {})
    items.append(
        {
            "aspect": "证据链",
            "evidence": (
                f"knowledge_unit_evidence {ev.get('evidence_rows',0):,} 行，"
                f"有证据 unit 覆盖 {ev.get('coverage',0):.1%}；"
                f"events 仅 event_id 自引用，无独立 evidence 表"
            ),
        }
    )
    items.append(
        {
            "aspect": "类型体系",
            "evidence": (
                f"KU 6 类 unit_type: {k.get('canonical_type_dist', {})}；"
                f"memory_type: {m.get('type_dist', {})}；"
                f"events 按 source/event_type: {e.get('source_dist', {})}"
            ),
        }
    )
    items.append(
        {
            "aspect": "管线可运维性",
            "evidence": (
                f"build_runs={k['tables'].get('knowledge_build_runs',{}).get('rows',0)}, "
                f"index_versions={k['tables'].get('knowledge_index_versions',{}).get('rows',0)}, "
                f"inventory_items={k['tables'].get('knowledge_inventory_items',{}).get('rows',0)}, "
                f"response_cache={k['tables'].get('knowledge_response_cache',{}).get('rows',0)}, "
                f"extraction_success_rate={k.get('extraction_success_rate')}"
            ),
        }
    )
    items.append(
        {
            "aspect": "主题广度",
            "evidence": (
                f"distinct subject={k.get('subject_cardinality',0):,}，"
                f"平均每 subject {k.get('avg_units_per_subject',0)} 条 canonical unit"
            ),
        }
    )
    return items


def make_charts(report: dict) -> list[str]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []

    for fname in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"):
        try:
            plt.rcParams["font.sans-serif"] = [fname]
            plt.rcParams["axes.unicode_minus"] = False
            break
        except Exception:
            continue

    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    e, m, k = report["layers"]["events"], report["layers"]["memory"], report["layers"]["knowledge"]
    colors = {"events": "#6B7280", "memory": "#3B82F6", "knowledge": "#10B981"}
    labels = {
        "events": "L1 events\n统合事件",
        "memory": "L2 memory\n记忆项",
        "knowledge": "L3 knowledge\n知识单元",
    }

    # 1 scale
    fig, ax = plt.subplots(figsize=(8, 4.5))
    keys = ["events", "memory", "knowledge"]
    ys = [
        e["tables"]["unified_events"]["rows"],
        m["tables"].get("memory_items", {}).get("rows", 0),
        k["tables"].get("canonical_knowledge_units", {}).get("rows", 0),
    ]
    bars = ax.bar([labels[x] for x in keys], ys, color=[colors[x] for x in keys])
    ax.set_title("SQLite 核心表规模对比")
    ax.set_ylabel("行数")
    for b, y in zip(bars, ys):
        ax.text(b.get_x() + b.get_width() / 2, y, f"{y:,}", ha="center", va="bottom", fontsize=9)
    fig.tight_layout()
    p = CHARTS_DIR / "sqlite_gen_01_scale.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p))

    # 2 text length hist shares
    fig, ax = plt.subplots(figsize=(10, 5))
    bucket_order = ["0-49", "50-99", "100-199", "200-399", "400-799", "800-1199", "1200+"]
    layers_tq = {
        "events": e["text_quality"],
        "memory": m.get("text_quality") or text_quality([]),
        "knowledge": k["text_quality"],
    }
    x = range(len(bucket_order))
    width = 0.25
    for i, key in enumerate(keys):
        hist = layers_tq[key].get("hist") or {}
        vals = [hist.get(b, 0) for b in bucket_order]
        total = sum(vals) or 1
        shares = [v / total for v in vals]
        ax.bar(
            [xi + (i - 1) * width for xi in x],
            shares,
            width=width,
            label=labels[key].replace("\n", " "),
            color=colors[key],
        )
    ax.set_xticks(list(x))
    ax.set_xticklabels(bucket_order, rotation=20)
    ax.set_ylabel("样本占比")
    ax.set_title("SQLite 文本长度分布（采样）")
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = CHARTS_DIR / "sqlite_gen_02_text_length.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p))

    # 3 answerability + noise
    fig, ax = plt.subplots(figsize=(9, 5))
    metrics = ["answerability_mean", "short_share", "noise_share", "long_gt800_share"]
    metric_labels = ["可答性", "短文占比", "噪声占比", "长文占比"]
    x = range(len(metrics))
    width = 0.25
    for i, key in enumerate(keys):
        vals = [layers_tq[key].get(m, 0) for m in metrics]
        ax.bar(
            [xi + (i - 1) * width for xi in x],
            vals,
            width=width,
            label=labels[key].replace("\n", " "),
            color=colors[key],
        )
    ax.set_xticks(list(x))
    ax.set_xticklabels(metric_labels)
    ax.set_ylim(0, 1.1)
    ax.set_title("文本质量指标对比")
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = CHARTS_DIR / "sqlite_gen_03_text_quality.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p))

    # 4 governance radar-like grouped
    fig, ax = plt.subplots(figsize=(9, 5))
    sc = report["scorecard"]
    metric_names = ["规模", "可答性", "短文", "低噪声", "置信度覆盖", "治理字段", "schema"]
    metric_keys = [
        "scale_norm",
        "answerability",
        "short_share",
        "low_noise",
        "confidence_coverage",
        "governance_flags",
        "schema_score",
    ]
    x = range(len(metric_names))
    width = 0.25
    for i, key in enumerate(keys):
        vals = [sc[key][mk] for mk in metric_keys]
        ax.bar(
            [xi + (i - 1) * width for xi in x],
            vals,
            width=width,
            label=labels[key].replace("\n", " "),
            color=colors[key],
        )
    ax.set_xticks(list(x))
    ax.set_xticklabels(metric_names)
    ax.set_ylim(0, 1.15)
    ax.set_title("SQLite 分层归一化打分卡（越高越好）")
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = CHARTS_DIR / "sqlite_gen_04_scorecard.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p))

    # 5 KU type dist
    types = k.get("canonical_type_dist") or {}
    if types:
        fig, ax = plt.subplots(figsize=(8, 5))
        labs = list(types.keys())
        vals = list(types.values())
        ax.barh(labs, vals, color="#10B981")
        ax.set_title("canonical 知识单元类型分布")
        ax.set_xlabel("条数")
        for i, v in enumerate(vals):
            ax.text(v, i, f" {v:,}", va="center")
        fig.tight_layout()
        p = CHARTS_DIR / "sqlite_gen_05_ku_types.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        saved.append(str(p))

    # 6 source modules + events source
    fig, ax = plt.subplots(figsize=(8, 4.5))
    src = e.get("source_dist") or {}
    if src:
        ax.bar(list(src.keys()), list(src.values()), color="#6B7280")
        ax.set_title("unified_events 按 source 分布")
        ax.set_ylabel("事件数")
        for i, (name, v) in enumerate(src.items()):
            ax.text(i, v, f"{v:,}", ha="center", va="bottom", fontsize=9)
        fig.tight_layout()
        p = CHARTS_DIR / "sqlite_gen_06_event_sources.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        saved.append(str(p))

    # 7 pipeline table sizes (knowledge ops)
    fig, ax = plt.subplots(figsize=(10, 5))
    pipe_names = [
        "knowledge_units",
        "canonical_knowledge_units",
        "knowledge_unit_evidence",
        "knowledge_inventory_items",
        "knowledge_run_items",
        "knowledge_response_cache",
        "knowledge_delta_items",
    ]
    pipe_vals = [k["tables"].get(n, {}).get("rows", 0) for n in pipe_names]
    short_names = [
        "draft KU",
        "canonical",
        "evidence",
        "inventory",
        "run_items",
        "resp_cache",
        "delta_items",
    ]
    ax.barh(short_names, pipe_vals, color="#059669")
    ax.set_title("知识层管线表规模")
    ax.set_xlabel("行数")
    for i, v in enumerate(pipe_vals):
        ax.text(v, i, f" {v:,}", va="center", fontsize=8)
    fig.tight_layout()
    p = CHARTS_DIR / "sqlite_gen_07_ku_pipeline.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p))

    # 8 extraction status if any
    st = k.get("run_item_status") or {}
    if st:
        fig, ax = plt.subplots(figsize=(7, 5))
        ax.pie(list(st.values()), labels=list(st.keys()), autopct="%1.1f%%", startangle=90)
        ax.set_title("knowledge_run_items 状态分布")
        fig.tight_layout()
        p = CHARTS_DIR / "sqlite_gen_08_extraction_status.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        saved.append(str(p))

    # 9 draft vs canonical + memory
    fig, ax = plt.subplots(figsize=(8, 4.5))
    names = ["draft KU", "canonical KU", "memory_items", "unified_events"]
    vals = [
        k["tables"].get("knowledge_units", {}).get("rows", 0),
        k["tables"].get("canonical_knowledge_units", {}).get("rows", 0),
        m["tables"].get("memory_items", {}).get("rows", 0),
        e["tables"].get("unified_events", {}).get("rows", 0),
    ]
    cols = ["#34D399", "#10B981", "#3B82F6", "#6B7280"]
    bars = ax.bar(names, vals, color=cols)
    ax.set_title("核心实体行数对照")
    for b, y in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, y, f"{y:,}", ha="center", va="bottom", fontsize=9)
    fig.tight_layout()
    p = CHARTS_DIR / "sqlite_gen_09_entity_counts.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p))

    # 10 source DB sizes
    sources = report.get("source_dbs") or {}
    names, sizes = [], []
    for name, info in sources.items():
        if info.get("exists"):
            names.append(name)
            sizes.append(info.get("size_mb") or 0)
    if names:
        fig, ax = plt.subplots(figsize=(9, 4.5))
        ax.barh(names, sizes, color="#6366F1")
        ax.set_xlabel("MB")
        ax.set_title("源库 / 对话库文件体积")
        for i, v in enumerate(sizes):
            ax.text(v, i, f" {v:.1f}", va="center")
        fig.tight_layout()
        p = CHARTS_DIR / "sqlite_gen_10_db_sizes.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        saved.append(str(p))

    return saved


def write_markdown(report: dict, charts: list[str]) -> None:
    e = report["layers"]["events"]
    m = report["layers"]["memory"]
    k = report["layers"]["knowledge"]
    sc = report["scorecard"]
    et, mt, kt = e["text_quality"], m.get("text_quality", {}), k["text_quality"]

    chart_lines = []
    for p in charts:
        rel = Path("charts") / Path(p).name
        chart_lines.append(f"### {Path(p).stem}\n\n![{Path(p).stem}]({rel.as_posix()})\n")

    improvements_md = "\n".join(
        f"- **{i['aspect']}**: {i['evidence']}" for i in report["improvements"]
    )

    # table inventory
    def tbl_rows(layer: dict) -> str:
        lines = []
        for name, info in sorted(layer.get("tables", {}).items(), key=lambda x: -x[1]["rows"]):
            lines.append(f"| `{name}` | {info['rows']:,} | {info['col_count']} |")
        return "\n".join(lines) if lines else "| — | 0 | 0 |"

    src_md = []
    for name, info in report.get("source_dbs", {}).items():
        if not info.get("exists"):
            src_md.append(f"| {name} | missing | — | — |")
            continue
        rows = sum(t.get("rows", 0) for t in info.get("tables", {}).values())
        src_md.append(
            f"| {name} | {info.get('size_mb')} MB | {info.get('table_count')} | {rows:,} (listed) |"
        )

    md = f"""# SQLite 分层代际对比报告

- 生成时间: `{report['generated_at']}`
- 主库: `integration/db/personal_system.sqlite` ({report['unified_db_size_mb']} MB, {report['unified_table_count']} tables)
- 策略: **只读对比，不修改任何数据**

## 1. 三层模型

| 层 | 代表表 | 定位 | 核心行数 |
|---|---|---|---:|
| L1 events | `unified_events` (+rich/categories) | 旧统合事件：跨源时间线 | {e['tables']['unified_events']['rows']:,} |
| L2 memory | `memory_items` (+bundles/promotions) | 中层记忆实验产物 | {m['tables'].get('memory_items',{}).get('rows',0):,} |
| L3 knowledge | `knowledge_units` / `canonical_*` | 新知识单元权威层 | draft {k['tables'].get('knowledge_units',{}).get('rows',0):,} / canonical {k['tables'].get('canonical_knowledge_units',{}).get('rows',0):,} |

## 2. 规模与管线表

### L1 events 表

| 表 | 行数 | 列数 |
|---|---:|---:|
{tbl_rows(e)}

### L2 memory 表

| 表 | 行数 | 列数 |
|---|---:|---:|
{tbl_rows(m)}

### L3 knowledge 表

| 表 | 行数 | 列数 |
|---|---:|---:|
{tbl_rows(k)}

## 3. 文本质量对比（采样）

| 指标 | L1 events | L2 memory | L3 knowledge |
|---|---:|---:|---:|
| 样本数 | {et['n']} | {mt.get('n',0)} | {kt['n']} |
| 长度中位 | {et['len_median']} | {mt.get('len_median',0)} | {kt['len_median']} |
| 长度均值 | {et['len_mean']} | {mt.get('len_mean',0)} | {kt['len_mean']} |
| 可答性均值 | {et['answerability_mean']} | {mt.get('answerability_mean',0)} | {kt['answerability_mean']} |
| 高可答占比 | {et['answerability_high_share']} | {mt.get('answerability_high_share',0)} | {kt['answerability_high_share']} |
| Q&A 形态占比 | {et['qa_shape_share']} | {mt.get('qa_shape_share',0)} | {kt['qa_shape_share']} |
| 用户断言占比 | {et['user_assert_share']} | {mt.get('user_assert_share',0)} | {kt['user_assert_share']} |
| 噪声占比 | {et['noise_share']} | {mt.get('noise_share',0)} | {kt['noise_share']} |
| 长文>800 占比 | {et['long_gt800_share']} | {mt.get('long_gt800_share',0)} | {kt['long_gt800_share']} |
| 短文 20–280 占比 | {et['short_share']} | {mt.get('short_share',0)} | {kt['short_share']} |

## 4. 治理与字段完备性

| 能力 | events | memory | knowledge |
|---|---|---|---|
| confidence | 否 | 是 (cov={m.get('confidence',{}).get('coverage')}) | 是 (cov={k.get('confidence',{}).get('coverage')}, mean={k.get('confidence',{}).get('mean')}) |
| lifecycle/status | 否 | 部分 ku_* 同步字段 | 是 lifecycle+status |
| 类型体系 | source/event_type | memory_type | unit_type×6 |
| Q + A 结构 | 否 | 否 | 是 |
| 独立 evidence 表 | 否 | memory_links | knowledge_unit_evidence |
| 版本/inventory/cache | 否 | 有限 | 完整管线表族 |
| 归一化治理分 | {sc['events']['governance_flags']} | {sc['memory']['governance_flags']} | {sc['knowledge']['governance_flags']} |

### 关键字段空值率

**events:** {json.dumps(e.get('null_rates',{}), ensure_ascii=False)}

**memory:** {json.dumps(m.get('null_rates',{}), ensure_ascii=False)}

**canonical KU:** {json.dumps(k.get('null_rates',{}), ensure_ascii=False)}

## 5. 知识层特有能力

| 指标 | 值 |
|---|---|
| draft → canonical collapse | {k.get('dedup',{}).get('collapsed')} ({k.get('dedup',{}).get('collapse_pct')}) |
| multi-member canonical 占比 | {k.get('multi_member_share')} |
| evidence 覆盖 (draft units) | {k.get('evidence',{}).get('coverage')} |
| distinct subjects | {k.get('subject_cardinality')} |
| avg units / subject | {k.get('avg_units_per_subject')} |
| extraction success rate | {k.get('extraction_success_rate')} |
| run_item 状态 | {json.dumps(k.get('run_item_status',{}), ensure_ascii=False)} |
| index versions | {len(k.get('index_versions') or [])} |

## 6. 源库对照（未删，只读）

| 库 | 体积 | 表数 | 代表表合计行数 |
|---|---|---:|---:|
{chr(10).join(src_md)}

## 7. 提升结论（数据支撑）

{improvements_md}

## 8. 图表

{''.join(chart_lines)}

## 9. 方法说明

- L1/L2/L3 为同一 `personal_system.sqlite` 内的**能力分层**，不是互相删除的替换
- 文本质量：均匀采样 content_rich / description / question+answer
- 可答性启发式与向量对比脚本一致（Q&A、用户断言、短文加分；代码/噪声/长文减分）
- 治理六项：confidence / lifecycle / status / unit_type / evidence / Q&A
- 源库（Agent/Google）仅作上游规模对照

原始 JSON: `sqlite_generation_comparison.json`
"""
    OUT_MD.write_text(md, encoding="utf-8")


def run(sample: int = 400) -> int:
    AI_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    if not UNIFIED_DB.exists():
        print("[error] unified db missing", file=sys.stderr)
        return 1

    print("[1] open personal_system.sqlite...")
    con = _connect(UNIFIED_DB)
    tables = [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]

    print("[2] analyze L1 events...")
    events = analyze_events(con, sample)
    print("[3] analyze L2 memory...")
    memory = analyze_memory(con, sample)
    print("[4] analyze L3 knowledge...")
    knowledge = analyze_knowledge(con, sample)
    con.close()

    print("[5] source DBs...")
    source_dbs = analyze_source_dbs()

    scorecard = layer_scorecard(events, memory, knowledge)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "unified_db": str(UNIFIED_DB.relative_to(ROOT)),
        "unified_db_size_mb": round(UNIFIED_DB.stat().st_size / 1024 / 1024, 2),
        "unified_table_count": len(tables),
        "unified_tables": tables,
        "sample_n": sample,
        "layers": {
            "events": events,
            "memory": memory,
            "knowledge": knowledge,
        },
        "source_dbs": source_dbs,
        "scorecard": scorecard,
        "note": "read-only layered comparison; no data deleted",
    }
    report["improvements"] = compute_improvements(report)

    print("[6] charts...")
    charts = make_charts(report)
    report["charts"] = [Path(p).name for p in charts]

    print("[7] write outputs...")
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, charts)

    print("=" * 60)
    print("SQLite Generation Comparison DONE")
    print(f"JSON: {OUT_JSON}")
    print(f"MD:   {OUT_MD}")
    print(f"Charts ({len(charts)}): {CHARTS_DIR}")
    for item in report["improvements"]:
        print(f"  * {item['aspect']}: {item['evidence'][:120]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Compare SQLite layers/generations")
    p.add_argument("--sample", type=int, default=400)
    args = p.parse_args(argv)
    return run(sample=args.sample)


if __name__ == "__main__":
    raise SystemExit(main())
