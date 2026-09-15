"""把正式知识层（KU）按会话卡实体做主题键控，物化 wiki 统合页面正文。

数据流（全部确定性，无 LLM、无网络）::

    personal_system.sqlite knowledge_units (lifecycle='current')
        --source_session_id (v2|cs|<hex>)--> semantic_mvp_v3.sqlite session_cards
        --card_json.entities--> 归一化实体 = 主题（subject topic）
    knowledge_unit_evidence (v2|cm|<hex>) --> 每条 claim 的 evidence_refs

主题键控规则（KU -> 主题绑定）
---------------------------------------------------------------------
1. 主题来源：session_cards.card_json 的 ``entities`` 数组（每张卡一个数组）。
2. 实体归一化：trim -> 去路径前缀（先去结尾路径分隔符，再按 ``/`` 与 ``\\``
   分割取最后一段，即"主干"）-> lowercase。归一化后为空、或无法通过
   ``TopicKey("subject", ...)`` 校验（含 ``:``/``/``/``\\``/控制字符等）的
   实体直接丢弃。
3. 绑定：KU 的 ``source_session_id`` 等于某张卡的 ``session_id`` 时，该 KU
   绑定到这张卡归一化后的每个实体主题（一条 KU 可出现在多个页面）。
4. topic_type 固定 ``subject``——这是契约要求而非可选项：
   ``topic_key.TopicKey`` 文档明确 subject 是"Phase 4 从 KU 桶统合的页面，
   无 personal/decision authority 背书，仅经 page store 解析"；而
   ``project:{scope}`` 等键在 ``topic_get._resolve`` 里必须有 personal
   state 断言匹配，否则 topic_not_found，物化的页面永远读不到。
   ``topic.list`` 目录项也固定按 subject 合并 page store 页面。
5. 噪声阈值：绑定 current KU 数 ``< --min-claims``（默认 5）的主题不建页。
6. 排序/限流：``--limit-topics N`` 按（claims 降序, 主题名升序）取前 N 个。

页面正文（服从既有契约，与 application/wiki/consolidate_wiki.py 同形）
---------------------------------------------------------------------
``wiki_page_body_v1``：``{schema, topic, subject, aggregation, claims,
evidence_refs, source_fingerprint}``。正文是聚合结果（claims + 证据引用），
永不含原始对话正文；且不含任何时间戳——同一输入产生同一 page_checksum，
时间戳只存在版本/页面行里。claims 为扁平列表，每条自带 ``unit_type``；
任务稿中 claim_sections 的意图由 per-claim ``unit_type`` +
``aggregation.unit_type_counts`` 覆盖（读侧 ``_page_first_get`` 只透传
topic/subject/aggregation/claims/evidence_refs 这几个契约字段）。

写入与幂等（既有 API）
---------------------------------------------------------------------
- 版本行经 ``WikiMaterializer.materialize``：自动 ``pv_N`` 递增、登记
  projection_checksum / 依赖 manifest（每个主题一条
  ``knowledge_unit`` 依赖，expected_checksum = 主题源指纹）。
- 页面行经 ``derived_store.insert_page``：主键 (topic_id, projection_version)
  唯一，INSERT OR REPLACE，永不产生同版本重复行。
- 幂等语义（与 consolidate_wiki 一致）：重跑时与最新存储页
  page_checksum 相同的主题整体跳过（不新增版本、行数不变）；只有源内容
  变化才追加新的不可变版本——重跑产生新版本而非重复行。
- 唯一可写库是 var/db/personal_wiki_projection.sqlite（可丢弃、再生物）；
  personal_system.sqlite / semantic_mvp_v3.sqlite 只读打开。

用法（仓库根目录）::

    python tools/semantic/materialize_wiki.py --dry-run
    python tools/semantic/materialize_wiki.py --limit-topics 10
    python tools/semantic/materialize_wiki.py --min-claims 8
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from personal_knowledge.wiki.derived_store import (  # noqa: E402
    ProjectionDependency,
    ProjectionPage,
    connect_rw,
    insert_page,
    latest_page,
)
from personal_knowledge.wiki.materialization import WikiMaterializer  # noqa: E402
from personal_knowledge.wiki.page_reader import (  # noqa: E402
    PAGE_BODY_SCHEMA,
    subject_topic_id,
    subject_topic_key,
)
from personal_knowledge.wiki.topic_key import TopicKey, TopicProjectionError  # noqa: E402

KU_DB = "var/db/personal_system.sqlite"
CARDS_DB = "var/db/semantic_mvp_v3.sqlite"
WIKI_STORE = "var/db/personal_wiki_projection.sqlite"

AUTHORITY_ID = "a.knowledge_unit"
DEFAULT_MIN_CLAIMS = 5
MAX_CLAIMS_PER_PAGE = 200
MAX_EVIDENCE_REFS = 200
MAX_EVIDENCE_REFS_PER_CLAIM = 8

_PATH_SPLIT = re.compile(r"[\\/]+")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _checksum(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 主题键控：实体归一化 + KU 绑定
# ---------------------------------------------------------------------------

def normalize_entity(raw: Any) -> str | None:
    """实体归一化：去路径前缀取主干 + lowercase；无效返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    segments = [seg for seg in _PATH_SPLIT.split(text) if seg]
    stem = (segments[-1] if segments else text).strip().lower()
    return stem or None


def valid_topic_name(normalized: str) -> bool:
    """实体能否构成合法的 ``subject:{name}`` TopicKey。"""
    try:
        TopicKey("subject", (normalized,))
    except TopicProjectionError:
        return False
    return True


def load_card_entities(db_path: Path | str = CARDS_DB) -> dict[str, list[str]]:
    """session_id -> 排序去重后的合法归一化实体列表（无可用实体则为空列表）。"""
    con = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT session_id, card_json FROM session_cards").fetchall()
    finally:
        con.close()
    out: dict[str, list[str]] = {}
    for row in rows:
        session_id = str(row["session_id"] or "")
        if not session_id:
            continue
        entities: set[str] = set()
        try:
            card = json.loads(row["card_json"])
        except (TypeError, ValueError):
            card = None
        if isinstance(card, Mapping):
            raw_entities = card.get("entities")
            if isinstance(raw_entities, list):
                for raw in raw_entities:
                    normalized = normalize_entity(raw)
                    if normalized and valid_topic_name(normalized):
                        entities.add(normalized)
        out[session_id] = sorted(entities)
    return out


def load_current_units(db_path: Path | str = KU_DB) -> list[dict[str, Any]]:
    """正式层 current KU（只读）。"""
    con = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT unit_id, unit_type, subject, question, answer,
                      confidence, lifecycle, version, source_session_id
               FROM knowledge_units
               WHERE lifecycle='current' AND status='current'
               ORDER BY unit_id""",
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def load_evidence_refs(db_path: Path | str = KU_DB) -> dict[str, list[str]]:
    """unit_id -> 排序去重证据引用（v2|cm|...）。"""
    con = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT unit_id, evidence_ref FROM knowledge_unit_evidence ORDER BY unit_id, evidence_ref",
        ).fetchall()
    finally:
        con.close()
    out: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        ref = str(row["evidence_ref"] or "")
        if ref:
            out[str(row["unit_id"])].append(ref)
    return {unit_id: sorted(dict.fromkeys(refs)) for unit_id, refs in out.items()}


def bind_topics(
    units: Iterable[Mapping[str, Any]],
    entities_by_session: Mapping[str, list[str]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    """KU -> 主题分桶。

    返回 (全部主题桶 {topic: [unit...] 主题名升序}, 未绑定计数)。
    """
    buckets: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    unbound = {"no_session_id": 0, "session_without_card": 0, "card_without_entities": 0}
    for unit in units:
        session_id = str(unit.get("source_session_id") or "")
        if not session_id:
            unbound["no_session_id"] += 1
            continue
        entities = entities_by_session.get(session_id)
        if entities is None:
            unbound["session_without_card"] += 1
            continue
        if not entities:
            unbound["card_without_entities"] += 1
            continue
        for topic in entities:
            buckets[topic][str(unit["unit_id"])] = dict(unit)
    topics = {topic: list(units_by_id.values()) for topic, units_by_id in sorted(buckets.items())}
    return topics, unbound


# ---------------------------------------------------------------------------
# 页面正文（确定性、只含聚合结果）
# ---------------------------------------------------------------------------

def _confidence(value: Any) -> float | None:
    try:
        confidence = float(value)  # 库里可能是 REAL 或 TEXT '0.9'
    except (TypeError, ValueError):
        return None
    return None if math.isnan(confidence) else confidence


def _claim_from_unit(unit: Mapping[str, Any], evidence_refs: list[str]) -> dict[str, Any]:
    return {
        "claim_type": "knowledge_unit",
        "unit_id": unit.get("unit_id"),
        "version": unit.get("version"),
        "unit_type": unit.get("unit_type"),
        "subject": unit.get("subject"),
        "question": unit.get("question") or "",
        "answer": unit.get("answer"),
        "confidence": _confidence(unit.get("confidence")),
        "lifecycle": unit.get("lifecycle"),
        "authority_ref": {
            "authority_id": AUTHORITY_ID,
            "record_type": "knowledge_unit",
            "record_id": unit.get("unit_id"),
            "checksum": None,
        },
        "evidence_refs": [{"ref": ref} for ref in evidence_refs[:MAX_EVIDENCE_REFS_PER_CLAIM]],
    }


def build_page_body(
    topic: str,
    units: list[dict[str, Any]],
    evidence_refs_by_unit: Mapping[str, list[str]],
) -> dict[str, Any]:
    """确定性统合页面正文：聚合 claims + 证据引用，无时间戳、无原始对话文本。"""
    unit_type_counts: dict[str, int] = defaultdict(int)
    lifecycle_counts: dict[str, int] = defaultdict(int)
    confidence_values: list[float] = []
    claims: list[dict[str, Any]] = []
    for unit in sorted(units, key=lambda item: str(item.get("unit_id"))):
        unit_type = str(unit.get("unit_type") or "unknown")
        lifecycle = str(unit.get("lifecycle") or "unknown")
        unit_type_counts[unit_type] += 1
        lifecycle_counts[lifecycle] += 1
        confidence = _confidence(unit.get("confidence"))
        if confidence is not None:
            confidence_values.append(confidence)
        claims.append(_claim_from_unit(unit, evidence_refs_by_unit.get(str(unit.get("unit_id")), [])))
        if len(claims) >= MAX_CLAIMS_PER_PAGE:
            break

    evidence_refs = sorted(dict.fromkeys(
        ref
        for claim in claims
        for ref in (row.get("ref") for row in claim.get("evidence_refs", ()) if isinstance(row, Mapping))
    ))[:MAX_EVIDENCE_REFS]
    source_fingerprint = _checksum({
        "units": [
            {
                "unit_id": unit.get("unit_id"),
                "lifecycle": unit.get("lifecycle"),
                "version": unit.get("version"),
                "answer": unit.get("answer"),
                "source_session_id": unit.get("source_session_id"),
            }
            for unit in sorted(units, key=lambda item: str(item.get("unit_id")))
        ]
    })
    return {
        "schema": PAGE_BODY_SCHEMA,
        "topic": {
            "topic_id": subject_topic_id(topic),
            "topic_type": "subject",
            "canonical_key": subject_topic_key(topic),
            "display_label": f"subject:{topic}",
        },
        "subject": topic,
        "aggregation": {
            "unit_count": len(units),
            "unit_type_counts": dict(sorted(unit_type_counts.items())),
            "lifecycle_counts": dict(sorted(lifecycle_counts.items())),
            "avg_confidence": round(sum(confidence_values) / len(confidence_values), 4) if confidence_values else None,
            "session_count": len({str(unit.get("source_session_id")) for unit in units if unit.get("source_session_id")}),
            "binding": "session_card_entities",
        },
        "claims": claims,
        "evidence_refs": evidence_refs,
        "source_fingerprint": source_fingerprint,
    }


# ---------------------------------------------------------------------------
# 写入（版本行 + 页面行；幂等：同 checksum 跳过，变化才追加新版本）
# ---------------------------------------------------------------------------

def write_topic_page(store_path: Path | str, topic: str, body: dict[str, Any]) -> str:
    """物化单个主题页面。返回 "written" 或 "skipped"（最新页已是同 checksum）。

    版本行经 WikiMaterializer（pv_N 递增 + 依赖登记），页面行经
    insert_page（(topic_id, projection_version) 唯一，无重复行）。
    """
    store = Path(store_path)
    topic_id = subject_topic_id(topic)
    page_body = _canonical_json(body)
    page_checksum = hashlib.sha256(page_body.encode("utf-8")).hexdigest()
    latest = latest_page(store, topic_id)
    if latest is not None and latest.page_checksum == page_checksum:
        return "skipped"

    deps = [
        ProjectionDependency(
            authority="knowledge_unit",
            stable_ref=topic,
            expected_checksum=str(body.get("source_fingerprint") or page_checksum),
            order_key=f"knowledge_unit:{topic}",
        )
    ]
    version = WikiMaterializer(store).materialize(
        TopicKey("subject", (topic.strip().lower(),)),
        snapshot_bindings={"knowledge_unit": topic},
        dependencies=deps,
        source_refs={
            "authority_ids": [AUTHORITY_ID],
            "consolidation": PAGE_BODY_SCHEMA,
            "bucketing": "session_card_entities",
        },
        freshness_status="fresh",
    )
    con = connect_rw(store)
    try:
        insert_page(con, ProjectionPage(
            topic_id=topic_id,
            topic_type="subject",
            projection_version=version.projection_version,
            page_body=page_body,
            page_checksum=page_checksum,
            generated_at=version.generated_at,
            snapshot_bindings={"knowledge_unit": topic},
        ))
    finally:
        con.close()
    return "written"


def _page_stored_checksum(store: Path, topic: str) -> str | None:
    """读取某主题当前最新页 checksum（dry-run 用，只读）。"""
    try:
        latest = latest_page(store, subject_topic_id(topic))
    except (OSError, sqlite3.Error):
        return None
    return latest.page_checksum if latest is not None else None


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

def select_topics(
    topics: Mapping[str, list[dict[str, Any]]],
    *,
    min_claims: int,
    limit_topics: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """阈值过滤 + 排序截断：默认主题名升序全量；limit 按（claims 降序, 主题名升序）取前 N。"""
    qualified = {topic: units for topic, units in sorted(topics.items()) if len(units) >= max(1, int(min_claims))}
    if limit_topics is not None:
        ranked = sorted(qualified.items(), key=lambda pair: (-len(pair[1]), pair[0]))
        return dict(ranked[: max(0, int(limit_topics))])
    return qualified


def materialize(
    *,
    db_path: Path | str = KU_DB,
    cards_path: Path | str = CARDS_DB,
    store_path: Path | str = WIKI_STORE,
    write: bool = False,
    min_claims: int = DEFAULT_MIN_CLAIMS,
    limit_topics: int | None = None,
) -> dict[str, Any]:
    """主题键控 + （可选）物化。write=False 为 dry-run，不触碰任何库的写路径。"""
    stats: dict[str, Any] = {"errors": []}
    try:
        units = load_current_units(db_path)
        evidence = load_evidence_refs(db_path)
        entities_by_session = load_card_entities(cards_path)
    except (sqlite3.Error, OSError) as exc:
        stats["errors"].append(f"source_db_unavailable: {exc}")
        return stats

    topics, unbound = bind_topics(units, entities_by_session)
    qualified = select_topics(topics, min_claims=min_claims)
    selected = select_topics(qualified, min_claims=1, limit_topics=limit_topics)
    store = Path(store_path)

    stats.update({
        "units_loaded": len(units),
        "unbound": unbound,
        "cards_loaded": len(entities_by_session),
        "cards_with_entities": sum(1 for ents in entities_by_session.values() if ents),
        "topics_all": len(topics),
        "topics_below_threshold": len(topics) - len(qualified),
        "topics_selected": len(selected),
        "claims_selected_total": sum(len(uns) for uns in selected.values()),
    })

    written = skipped = errors = 0
    for topic, topic_units in sorted(selected.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        body = build_page_body(topic, topic_units, evidence)
        if not write:
            stored = _page_stored_checksum(store, topic)
            if stored is not None and stored == _checksum(body):
                skipped += 1
            else:
                written += 1
            continue
        try:
            if write_topic_page(store, topic, body) == "written":
                written += 1
            else:
                skipped += 1
        except (sqlite3.Error, OSError, ValueError) as exc:
            errors += 1
            stats["errors"].append(f"topic_write_error:{topic}: {exc}")
    stats["pages_written"] = written
    stats["pages_skipped"] = skipped
    stats["pages_errors"] = errors
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按会话卡实体主题键控物化 KU wiki 页面")
    parser.add_argument("--dry-run", action="store_true", help="只报告分布，不写库（默认真物化）")
    parser.add_argument("--min-claims", type=int, default=DEFAULT_MIN_CLAIMS, help="建页噪声阈值（默认 5 条 current KU）")
    parser.add_argument("--limit-topics", type=int, default=None, help="按 claims 降序只处理前 N 个主题")
    parser.add_argument("--db", default=KU_DB, help="正式 KU 库（只读）")
    parser.add_argument("--cards", default=CARDS_DB, help="会话卡库（只读）")
    parser.add_argument("--store", default=WIKI_STORE, help="wiki 派生库（唯一可写）")
    args = parser.parse_args(argv)

    stats = materialize(
        db_path=Path(args.db),
        cards_path=Path(args.cards),
        store_path=Path(args.store),
        write=not args.dry_run,
        min_claims=args.min_claims,
        limit_topics=args.limit_topics,
    )
    mode = "dry-run" if args.dry_run else "write"
    print("=" * 60)
    print("KU -> Wiki 页面物化（会话卡实体主题键控）")
    print("=" * 60)
    print(f"mode:                 {mode}")
    print(f"units loaded:         {stats.get('units_loaded', 0)}")
    print(f"unbound:              {stats.get('unbound', {})}")
    print(f"cards loaded:         {stats.get('cards_loaded', 0)} (with entities: {stats.get('cards_with_entities', 0)})")
    print(f"topics all:           {stats.get('topics_all', 0)}")
    print(f"topics below {args.min_claims} claims: {stats.get('topics_below_threshold', 0)}")
    print(f"topics selected:      {stats.get('topics_selected', 0)}")
    print(f"claims bound (sum):   {stats.get('claims_selected_total', 0)}（一条 KU 可绑定多个主题）")
    print(f"pages written:        {stats.get('pages_written', 0)}")
    print(f"pages skipped:        {stats.get('pages_skipped', 0)}")
    if stats.get("errors"):
        print("errors:")
        for error in stats["errors"][:10]:
            print(f"  - {error}")
    return 1 if stats.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
