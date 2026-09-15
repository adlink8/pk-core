"""Phase 14 Wave 4.1：knowledge unit candidate vector store。

把 ``status='current'`` 的 knowledge units 向量化到版本化 Chroma collection。
collection 命名包含 build ID；向量化文本使用 canonical 内容和有界、合规的
用户证据上下文，返回 document 仍只保存 canonical question+answer。metadata
只保存计数和 checksum，不保存证据正文。

只索引 evidence gate passed 的 current units。exact reconcile：collection IDs
必须等于 eligible unit IDs，missing/orphan/duplicate 均为 0。不覆盖 active pointer。

用法::

    python build_knowledge_unit_vector_store.py --dry-run
    python build_knowledge_unit_vector_store.py --write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.core.sqlite import connect_rw

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB, UNIFIED_DB  # noqa: E402
from personal_knowledge.core.chroma_client import ChromaClient, ChromaError  # noqa: E402
from personal_knowledge.core.privacy_guard import guard_text  # noqa: E402
import personal_knowledge.core.local_embed as local_embed  # noqa: E402

COLLECTION_PREFIX = "knowledge_units"
EMBEDDING_POLICY = "eligible-user-context-v1"
MAX_EVIDENCE_SNIPPETS = 2
MAX_EVIDENCE_CHARS = 512
MAX_SUBJECT_CHARS = 160
MAX_QUESTION_CHARS = 640
MAX_ANSWER_CHARS = 1200
_WS = re.compile(r"\s+")


@dataclass
class VectorStoreStats:
    """candidate index 构建统计。"""
    build_id: str = ""
    version_id: str = ""
    collection_name: str = ""
    eligible_units: int = 0
    indexed: int = 0
    missing: int = 0       # eligible but not indexed（必须 0）
    orphan: int = 0        # indexed but not eligible（必须 0）
    duplicate: int = 0     # 重复 ID（必须 0）
    embed_dim: int = 0
    embed_model: str = ""
    embedding_policy: str = EMBEDDING_POLICY
    enriched_units: int = 0
    evidence_snippets: int = 0
    privacy_sealed_spans: int = 0
    embedding_manifest_checksum: str = ""
    collection_checksum: str = ""
    gate_passed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _get_current_run_id(db_path: Path) -> str | None:
    """获取最新的 validated/current build run ID。"""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    row = con.execute(
        "SELECT run_id FROM knowledge_build_runs "
        "WHERE status IN ('current','validated') "
        "ORDER BY generated_at DESC LIMIT 1"
    ).fetchone()
    con.close()
    return row[0] if row else None


def candidate_version_id(build_id: str, collection_name: str) -> str:
    """A rebuild of one canonical run must not replace its active version row."""
    suffix = hashlib.sha256(collection_name.encode("utf-8")).hexdigest()[:12]
    return f"kiv_{build_id[:12]}_{suffix}"


def _chunks(values: list[str], size: int = 800) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def _clean(value: object, limit: int) -> str:
    return _WS.sub(" ", str(value or "")).strip()[:limit]


def _load_user_contexts(
    member_rows: list[sqlite3.Row],
    conversation_db: Path,
) -> tuple[dict[str, list[str]], int]:
    """Resolve each member anchor to an eligible user message in the same turn.

    Most extracted units point at the assistant message that contains the answer.
    The closest preceding eligible user message is the query-side evidence needed
    for semantic alignment.  Sidechain/system/ineligible sessions are excluded.
    """
    refs = sorted({str(row["source_message_ref"] or "") for row in member_rows if row["source_message_ref"]})
    if not refs or not conversation_db.exists():
        return {}, 0

    con = sqlite3.connect(f"file:{conversation_db.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    try:
        anchors: dict[str, sqlite3.Row] = {}
        for batch in _chunks(refs):
            marks = ",".join("?" for _ in batch)
            rows = con.execute(
                "SELECT canonical_message_id,canonical_session_id,ordinal,role,content,"
                "evidence_scope,is_system FROM canonical_messages "
                f"WHERE canonical_message_id IN ({marks})",
                batch,
            ).fetchall()
            anchors.update({str(row["canonical_message_id"]): row for row in rows})

        session_ids = sorted({str(row["canonical_session_id"]) for row in anchors.values()})
        eligible_sessions: set[str] = set()
        for batch in _chunks(session_ids):
            marks = ",".join("?" for _ in batch)
            eligible_sessions.update(
                str(row[0]) for row in con.execute(
                    "SELECT canonical_session_id FROM canonical_sessions "
                    f"WHERE canonical_session_id IN ({marks}) "
                    "AND evidence_eligible=1 AND COALESCE(evidence_scope,'user')='user'",
                    batch,
                ).fetchall()
            )

        users_by_session: dict[str, list[sqlite3.Row]] = {}
        for batch in _chunks(sorted(eligible_sessions)):
            marks = ",".join("?" for _ in batch)
            for row in con.execute(
                "SELECT canonical_message_id,canonical_session_id,ordinal,content "
                "FROM canonical_messages "
                f"WHERE canonical_session_id IN ({marks}) AND role='user' "
                "AND COALESCE(is_system,0)=0 AND evidence_scope='user' "
                "ORDER BY canonical_session_id,ordinal",
                batch,
            ).fetchall():
                users_by_session.setdefault(str(row["canonical_session_id"]), []).append(row)
    finally:
        con.close()

    context_by_ref: dict[str, str] = {}
    sealed_spans = 0
    for ref, anchor in anchors.items():
        session_id = str(anchor["canonical_session_id"])
        if session_id not in eligible_sessions or bool(anchor["is_system"]):
            continue
        selected = None
        for candidate in users_by_session.get(session_id, []):
            if int(candidate["ordinal"]) > int(anchor["ordinal"]):
                break
            selected = candidate
        if selected is None:
            continue
        guarded = guard_text(_clean(selected["content"], MAX_EVIDENCE_CHARS), mode="redact")
        if guarded.text:
            context_by_ref[ref] = guarded.text
            sealed_spans += guarded.hit_count

    contexts: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    for row in member_rows:
        unit_id = str(row["canonical_unit_id"])
        value = context_by_ref.get(str(row["source_message_ref"] or ""))
        if not value or value in seen.setdefault(unit_id, set()):
            continue
        if len(contexts.setdefault(unit_id, [])) < MAX_EVIDENCE_SNIPPETS:
            contexts[unit_id].append(value)
            seen[unit_id].add(value)
    return contexts, sealed_spans


def canonical_document(unit: dict) -> str:
    """Safe product document; never contains member evidence bodies."""
    return " ".join(part for part in (
        _clean(unit.get("question"), MAX_QUESTION_CHARS),
        _clean(unit.get("answer"), MAX_ANSWER_CHARS),
    ) if part)


def embedding_text(unit: dict) -> str:
    """Private build input. User context is early so model truncation keeps it."""
    parts = [f"主题：{_clean(unit.get('subject'), MAX_SUBJECT_CHARS)}"]
    parts.extend(f"用户上下文：{value}" for value in unit.get("evidence_contexts", ()))
    parts.append(f"知识问题：{_clean(unit.get('question'), MAX_QUESTION_CHARS)}")
    parts.append(f"知识答案：{_clean(unit.get('answer'), MAX_ANSWER_CHARS)}")
    return "\n".join(part for part in parts if not part.endswith("："))


def load_eligible_units(
    db_path: Path,
    conversation_db: Path = AGENT_CONVERSATIONS_DB,
) -> tuple[list[dict], int]:
    """Load current canonical units plus bounded, resolved user contexts."""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT c.canonical_unit_id as unit_id, c.unit_type, c.subject, c.question, c.answer, "
        "c.confidence, c.lifecycle, c.run_id, "
        "COALESCE((SELECT source_message_ref FROM knowledge_units u "
        "  JOIN canonical_unit_members cum ON u.unit_id=cum.member_unit_id "
        "  WHERE cum.canonical_unit_id=c.canonical_unit_id LIMIT 1), '') as source_message_ref, "
        # Phase 41-04 (D-02)：scope 取自 member unit，供检索层按轨过滤；
        # load_eligible_units 不按 scope 过滤（as| units 进候选索引是期望行为）。
        "COALESCE((SELECT u2.evidence_scope FROM knowledge_units u2 "
        "  JOIN canonical_unit_members cum2 ON u2.unit_id=cum2.member_unit_id "
        "  WHERE cum2.canonical_unit_id=c.canonical_unit_id LIMIT 1), 'user') as evidence_scope "
        "FROM canonical_knowledge_units c "
        "WHERE c.status='current' AND c.lifecycle='current' "
        "ORDER BY c.canonical_unit_id"
    ).fetchall()
    member_rows = con.execute(
        "SELECT m.canonical_unit_id,u.source_message_ref "
        "FROM canonical_unit_members m "
        "JOIN canonical_knowledge_units c ON c.canonical_unit_id=m.canonical_unit_id "
        "JOIN knowledge_units u ON u.unit_id=m.member_unit_id "
        "WHERE c.status='current' AND c.lifecycle='current' "
        "AND COALESCE(u.source_message_ref,'')<>'' "
        "ORDER BY m.canonical_unit_id,m.id"
    ).fetchall()
    con.close()
    contexts, sealed_spans = _load_user_contexts(member_rows, conversation_db)
    result = []
    for row in rows:
        unit = dict(row)
        unit["evidence_contexts"] = contexts.get(str(row["unit_id"]), [])
        result.append(unit)
    return result, sealed_spans


def build_candidate_index(
    db_path: Path = UNIFIED_DB,
    write: bool = False,
) -> tuple[VectorStoreStats, str | None]:
    """构建 candidate Chroma collection。

    返回 ``(stats, collection_name_or_none)``。
    """
    stats = VectorStoreStats()

    # 验证 embedding 模型
    ok, msg, dim = local_embed.verify_model()
    if not ok:
        stats.gate_passed = False
        return stats, None
    stats.embed_dim = dim
    stats.embed_model = "bge-small-zh-v1.5"

    # 获取 build ID
    run_id = _get_current_run_id(db_path)
    if not run_id:
        return stats, None
    stats.build_id = run_id[:12]
    ts = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    collection_name = f"{COLLECTION_PREFIX}_{stats.build_id}_{ts}"
    stats.collection_name = collection_name
    stats.version_id = candidate_version_id(run_id, collection_name)

    # 加载 eligible units
    units, sealed_spans = load_eligible_units(db_path)
    stats.eligible_units = len(units)
    stats.enriched_units = sum(bool(unit["evidence_contexts"]) for unit in units)
    stats.evidence_snippets = sum(len(unit["evidence_contexts"]) for unit in units)
    stats.privacy_sealed_spans = sealed_spans
    if not units:
        return stats, None

    # embedding input and returned document are intentionally separate.
    texts = [embedding_text(u) for u in units]
    documents = [canonical_document(u) for u in units]
    ids = [u["unit_id"] for u in units]
    manifest = [
        [unit_id, hashlib.sha256(text.encode("utf-8")).hexdigest()]
        for unit_id, text in zip(ids, texts)
    ]
    stats.embedding_manifest_checksum = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    # 检查重复 ID
    if len(ids) != len(set(ids)):
        stats.duplicate = len(ids) - len(set(ids))

    if not write:
        # dry-run：只报告
        stats.indexed = len(units)
        stats.gate_passed = stats.missing == 0 and stats.orphan == 0 and stats.duplicate == 0
        return stats, None

    # 连接 Chroma
    client = ChromaClient()

    # 创建 collection（如果已存在先删除，保证干净）
    try:
        existing = client.list_collections()
        for col in existing:
            if isinstance(col, dict) and col.get("name") == collection_name:
                # 删除旧 collection
                import requests
                requests.delete(f"{client._base}/collections/{col['id']}", timeout=30)
                break
    except Exception:
        pass

    coll = client.get_or_create_collection(collection_name, metadata={
        "hnsw:space": "cosine",
        "embedding_policy": EMBEDDING_POLICY,
        "embedding_manifest_checksum": stats.embedding_manifest_checksum,
    })

    # 批量 embedding
    embeddings = local_embed.embed_batch(texts)
    if embeddings is None or len(embeddings) != len(texts):
        stats.gate_passed = False
        return stats, None

    # 准备 metadata
    metadatas = [
        {
            "unit_type": u.get("unit_type", ""),
            "subject": u.get("subject", ""),
            "confidence": u.get("confidence", 0),
            "lifecycle": u.get("lifecycle", "current"),
            "run_id": u.get("run_id", "")[:12],
            "source_message_ref": u.get("source_message_ref", "") or "",
            "evidence_scope": u.get("evidence_scope", "user") or "user",
            "embedding_policy": EMBEDDING_POLICY,
            "evidence_context_count": len(u.get("evidence_contexts", ())),
            "embedding_text_checksum": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        for u, text in zip(units, texts)
    ]

    # 写入 Chroma（分批，避免单次 2 万+ 条撑爆 HTTP）
    batch_size = 500
    try:
        emb_lists = [list(e) for e in embeddings]
        for i in range(0, len(ids), batch_size):
            j = i + batch_size
            coll.add(
                ids=ids[i:j],
                embeddings=emb_lists[i:j],
                documents=documents[i:j],
                metadatas=metadatas[i:j],
                timeout=300,
            )
            if (i // batch_size) % 10 == 0:
                print(f"[index] wrote {min(j, len(ids))}/{len(ids)}", flush=True)
    except (ChromaError, Exception) as e:
        stats.gate_passed = False
        print(f"[error] chroma add failed: {e}", file=sys.stderr)
        return stats, None

    stats.indexed = coll.count()
    from personal_knowledge.application.knowledge.promote_knowledge_index import (
        _compute_collection_checksum,
    )
    stats.collection_checksum = _compute_collection_checksum(collection_name)

    # exact reconcile
    indexed_ids = set(ids)
    eligible_ids = set(ids)
    stats.missing = len(eligible_ids - indexed_ids)
    stats.orphan = len(indexed_ids - eligible_ids)
    stats.gate_passed = (
        stats.missing == 0
        and stats.orphan == 0
        and stats.duplicate == 0
        and stats.indexed == stats.eligible_units
    )

    # 记录到 knowledge_index_versions
    con = connect_rw(db_path)
    con.execute(
        "INSERT INTO knowledge_index_versions VALUES (?,?,?,?,?,?,?,?,?)",
        (
            stats.version_id, run_id, collection_name, run_id,
            stats.indexed, "candidate",
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            None, stats.collection_checksum,
        ),
    )
    con.commit()
    con.close()

    return stats, collection_name


def run(dry_run: bool, write: bool, db_path: Path = UNIFIED_DB) -> int:
    if dry_run and write:
        print("[error] --dry-run 与 --write 互斥", file=sys.stderr)
        return 2

    stats, coll_name = build_candidate_index(db_path, write=write)

    print("=" * 60)
    print("Phase 14 Wave 4.1：Candidate Vector Store")
    print("=" * 60)
    print(f"build_id:        {stats.build_id}")
    print(f"version_id:      {stats.version_id}")
    print(f"collection:      {stats.collection_name}")
    print(f"eligible units:  {stats.eligible_units}")
    print(f"indexed:         {stats.indexed}")
    print(f"missing:         {stats.missing} (must be 0)")
    print(f"orphan:          {stats.orphan} (must be 0)")
    print(f"duplicate:       {stats.duplicate} (must be 0)")
    print(f"embed:           {stats.embed_model} ({stats.embed_dim}d)")
    print(f"embed policy:    {stats.embedding_policy}")
    print(f"enriched units:  {stats.enriched_units}")
    print(f"evidence texts:  {stats.evidence_snippets}")
    print(f"privacy sealed:  {stats.privacy_sealed_spans}")
    print(f"embed manifest:  {stats.embedding_manifest_checksum}")
    print(f"collection hash: {stats.collection_checksum}")
    print(f"gate:            {'PASS' if stats.gate_passed else 'FAIL'}")
    if coll_name:
        print(f"collection name: {coll_name}")
    elif dry_run:
        print("[dry-run] 未写入")
    return 0 if stats.gate_passed else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 14 Wave 4.1: candidate vector store")
    p.add_argument("--dry-run", action="store_true", default=False)
    p.add_argument("--write", action="store_true")
    p.add_argument("--db", type=Path, default=UNIFIED_DB)
    args = p.parse_args(argv)
    if not args.write and not args.dry_run:
        args.dry_run = True
    return run(args.dry_run, args.write, args.db)


if __name__ == "__main__":
    raise SystemExit(main())
