"""Wave 9.1: 基于 conversation_turns 生成图关系候选。

输入:
- integration/analysis/ai_context/conversation_summaries.json
- Chroma collection `conversation_turns`

输出:
- SQLite 表 graph_relation_candidates

规则:
- semantic_candidate: 对每个 turn 用向量库召回 top-k 近邻
- temporal_candidate: 同 session 相邻 turn 固定加入
- 过滤 self pair / 重复 pair / source_refs 缺失 pair / 低相似度 pair
- 执行前先做 collection-summary 一致性门禁,避免用过期向量库生成候选

用法:
  python -m personal_knowledge.application.graph.build_graph_relation_candidates --dry-run
  python -m personal_knowledge.application.graph.build_graph_relation_candidates --write --limit 100
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from personal_knowledge.core.chroma_client import ChromaClient

ROOT = Path(__file__).resolve().parents[4]
SUMMARIES_JSON = ROOT / 'integration' / 'analysis' / 'ai_context' / 'conversation_summaries.json'
SQLITE_DB = ROOT / 'integration' / 'db' / 'personal_system.sqlite'
COLLECTION_NAME = 'conversation_turns'
DEFAULT_TOP_K = 8
DEFAULT_LIMIT = 0
MIN_SEMANTIC_SCORE = 0.78
SAMPLE_REPORT = ROOT / 'integration' / 'analysis' / 'ai_context' / 'graph_relation_candidates_report.json'

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_relation_candidates (
    candidate_id TEXT PRIMARY KEY,
    source_node_id TEXT NOT NULL,
    target_node_id TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    source_turn_id TEXT,
    target_session_id TEXT NOT NULL,
    target_turn_id TEXT,
    similarity REAL,
    candidate_reason TEXT NOT NULL,
    candidate_type TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grc_source_node ON graph_relation_candidates(source_node_id);
CREATE INDEX IF NOT EXISTS idx_grc_target_node ON graph_relation_candidates(target_node_id);
CREATE INDEX IF NOT EXISTS idx_grc_type ON graph_relation_candidates(candidate_type);
CREATE INDEX IF NOT EXISTS idx_grc_source_session ON graph_relation_candidates(source_session_id);
CREATE INDEX IF NOT EXISTS idx_grc_target_session ON graph_relation_candidates(target_session_id);
"""


def make_node_id(session_id: str, turn_id: str | None, turn_no: int) -> str:
    return f"{session_id}#{turn_id or f't{turn_no}'}"


def canonical_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def candidate_id_for(a: str, b: str, candidate_type: str) -> str:
    left, right = canonical_pair(a, b)
    digest = hashlib.sha1(f"{candidate_type}|{left}|{right}".encode('utf-8')).hexdigest()[:16]
    return f"grc:{candidate_type}:{digest}"


def load_turn_index() -> tuple[dict[str, dict], list[str]]:
    if not SUMMARIES_JSON.exists():
        raise FileNotFoundError(f'缺少 summary 产物: {SUMMARIES_JSON}')
    data = json.loads(SUMMARIES_JSON.read_text(encoding='utf-8'))
    turn_map: dict[str, dict] = {}
    order: list[str] = []
    for session in data:
        sid = session['session_id']
        main_topic = session.get('main_topic', '')
        source = session.get('meta', {}).get('source', '')
        turns = session.get('turn_summaries', [])
        for turn_no, turn in enumerate(turns, 1):
            node_id = make_node_id(sid, turn.get('turn_id'), turn_no)
            refs = list(dict.fromkeys(turn.get('source_refs') or []))
            turn_map[node_id] = {
                'node_id': node_id,
                'session_id': sid,
                'turn_id': turn.get('turn_id') or '',
                'turn_no': turn_no,
                'main_topic': main_topic,
                'source': source,
                'narrative': (turn.get('narrative') or '').strip(),
                'source_refs': refs,
            }
            order.append(node_id)
    return turn_map, order


def preflight_vector_sync(turn_map: dict[str, dict]) -> dict:
    client = ChromaClient()
    coll_info = client._find_collection_by_name(COLLECTION_NAME)
    if not coll_info:
        raise RuntimeError(
            'conversation_turns collection 不存在; 先运行 "python -m personal_knowledge.application.conversation.build_conversation_vector_store --write"'
        )
    coll = client.get_or_create_collection(COLLECTION_NAME)
    actual_count = coll.count()
    expected_count = len(turn_map)
    sample_limit = min(200, actual_count)
    raw = coll.get(limit=sample_limit, include=['metadatas']) if sample_limit > 0 else {'metadatas': []}
    sample_metas = raw.get('metadatas') or []
    sample_missing_required = 0
    for meta in sample_metas:
        if not str((meta or {}).get('session_id') or '').strip() or not str((meta or {}).get('event_type') or '').strip():
            sample_missing_required += 1
    report = {
        'collection': COLLECTION_NAME,
        'expected_count': expected_count,
        'actual_count': actual_count,
        'count_match': actual_count == expected_count,
        'sample_size': sample_limit,
        'sample_missing_required': sample_missing_required,
        'dimension': coll_info.get('dimension'),
    }
    if actual_count != expected_count:
        raise RuntimeError(
            f'conversation_turns 过期: expected={expected_count}, actual={actual_count}; '
            '先重跑 "python -m personal_knowledge.application.conversation.build_conversation_vector_store --write"'
        )
    if sample_missing_required > 0:
        raise RuntimeError(
            f'conversation_turns metadata 不完整: sample_missing_required={sample_missing_required}'
        )
    return report


def load_embeddings(limit: int = 0) -> list[dict]:
    client = ChromaClient()
    coll = client.get_or_create_collection(COLLECTION_NAME)
    total = coll.count()
    fetch = total if limit <= 0 else min(limit, total)
    batch = 200
    out: list[dict] = []
    for offset in range(0, fetch, batch):
        raw = coll.get(limit=min(batch, fetch - offset), offset=offset,
                       include=['documents', 'metadatas', 'embeddings'])
        ids = raw.get('ids') or []
        docs = raw.get('documents') or []
        metas = raw.get('metadatas') or []
        embs = raw.get('embeddings') or []
        for idx, node_id in enumerate(ids):
            out.append({
                'node_id': node_id,
                'document': docs[idx] if idx < len(docs) else '',
                'metadata': metas[idx] if idx < len(metas) else {},
                'embedding': embs[idx] if idx < len(embs) else None,
            })
    return out


def build_temporal_candidates(turn_map: dict[str, dict], order: list[str]) -> list[dict]:
    out = []
    for node_id in order:
        cur = turn_map[node_id]
        target = next((v for v in turn_map.values()
                       if v['session_id'] == cur['session_id'] and v['turn_no'] == cur['turn_no'] + 1), None)
        if not target:
            continue
        out.append({
            'source_node_id': cur['node_id'],
            'target_node_id': target['node_id'],
            'source_session_id': cur['session_id'],
            'source_turn_id': cur['turn_id'],
            'target_session_id': target['session_id'],
            'target_turn_id': target['turn_id'],
            'similarity': 1.0,
            'candidate_reason': 'adjacent_turn',
            'candidate_type': 'temporal_candidate',
            'source_refs_json': json.dumps(cur['source_refs'] + target['source_refs'], ensure_ascii=False),
        })
    return out


def build_semantic_candidates(turn_map: dict[str, dict], embedded_rows: list[dict], top_k: int) -> list[dict]:
    client = ChromaClient()
    coll = client.get_or_create_collection(COLLECTION_NAME)
    out = []
    for row in embedded_rows:
        node_id = row['node_id']
        emb = row.get('embedding')
        if emb is None or node_id not in turn_map:
            continue
        raw = coll.query(
            query_embeddings=[emb],
            n_results=top_k + 1,
            include=['metadatas', 'documents', 'distances'],
        )
        ids = raw.get('ids', [[]])[0]
        distances = raw.get('distances', [[]])[0]
        for idx, other_id in enumerate(ids):
            if other_id == node_id or other_id not in turn_map:
                continue
            dist = distances[idx] if idx < len(distances) else 1.0
            similarity = round(max(0.0, 1.0 - dist / 2.0), 4)
            src = turn_map[node_id]
            tgt = turn_map[other_id]
            if src['session_id'] == tgt['session_id']:
                continue
            out.append({
                'source_node_id': src['node_id'],
                'target_node_id': tgt['node_id'],
                'source_session_id': src['session_id'],
                'source_turn_id': src['turn_id'],
                'target_session_id': tgt['session_id'],
                'target_turn_id': tgt['turn_id'],
                'similarity': similarity,
                'candidate_reason': f'cross_session_semantic_topk:{top_k}',
                'candidate_type': 'semantic_candidate',
                'source_refs_json': json.dumps(src['source_refs'] + tgt['source_refs'], ensure_ascii=False),
            })
    return out


def filter_candidates(candidates: list[dict], min_semantic_score: float) -> tuple[list[dict], dict]:
    reasons = {
        'self_pair': 0,
        'duplicate_pair': 0,
        'missing_source_refs': 0,
        'low_similarity': 0,
    }
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for cand in candidates:
        src = cand['source_node_id']
        tgt = cand['target_node_id']
        if src == tgt:
            reasons['self_pair'] += 1
            continue
        refs = json.loads(cand['source_refs_json']) if cand['source_refs_json'] else []
        refs = list(dict.fromkeys([r for r in refs if str(r).strip()]))
        if not refs:
            reasons['missing_source_refs'] += 1
            continue
        if cand['candidate_type'] == 'semantic_candidate' and float(cand['similarity'] or 0.0) < min_semantic_score:
            reasons['low_similarity'] += 1
            continue
        pair_key = canonical_pair(src, tgt) + (cand['candidate_type'],)
        if pair_key in seen:
            reasons['duplicate_pair'] += 1
            continue
        seen.add(pair_key)
        cand = dict(cand)
        cand['source_refs_json'] = json.dumps(refs, ensure_ascii=False)
        cand['candidate_id'] = candidate_id_for(src, tgt, cand['candidate_type'])
        out.append(cand)
    return out, reasons


def summarize(candidates: list[dict], filter_stats: dict) -> dict:
    same_session = sum(1 for c in candidates if c['source_session_id'] == c['target_session_id'])
    cross_session = len(candidates) - same_session
    by_type: dict[str, int] = {}
    sims = []
    for cand in candidates:
        by_type[cand['candidate_type']] = by_type.get(cand['candidate_type'], 0) + 1
        if cand['candidate_type'] == 'semantic_candidate':
            sims.append(float(cand['similarity'] or 0.0))
    return {
        'candidate_count': len(candidates),
        'by_type': by_type,
        'same_session': same_session,
        'cross_session': cross_session,
        'semantic_avg_similarity': round(sum(sims) / len(sims), 4) if sims else None,
        'filter_stats': filter_stats,
    }


def write_sqlite(candidates: list[dict]) -> None:
    SQLITE_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(SQLITE_DB)
    try:
        con.executescript(SCHEMA_SQL)
        con.execute('DELETE FROM graph_relation_candidates')
        now = time.strftime('%Y-%m-%dT%H:%M:%S')
        rows = [(
            c['candidate_id'], c['source_node_id'], c['target_node_id'],
            c['source_session_id'], c['source_turn_id'], c['target_session_id'],
            c['target_turn_id'], c['similarity'], c['candidate_reason'],
            c['candidate_type'], c['source_refs_json'], now,
        ) for c in candidates]
        con.executemany(
            'INSERT OR REPLACE INTO graph_relation_candidates '
            '(candidate_id, source_node_id, target_node_id, source_session_id, source_turn_id, '
            'target_session_id, target_turn_id, similarity, candidate_reason, candidate_type, '
            'source_refs_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            rows,
        )
        con.commit()
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='Wave 9.1 图关系候选生成')
    p.add_argument('--dry-run', action='store_true', help='只计算候选,不写库')
    p.add_argument('--write', action='store_true', help='写入 SQLite graph_relation_candidates')
    p.add_argument('--limit', type=int, default=DEFAULT_LIMIT, help='只处理前 N 个 turn(0=全部)')
    p.add_argument('--top-k', type=int, default=DEFAULT_TOP_K, help='每个 turn 的语义近邻数')
    p.add_argument('--min-similarity', type=float, default=MIN_SEMANTIC_SCORE,
                   help='semantic_candidate 最低相似度阈值')
    args = p.parse_args(argv)
    if args.dry_run and args.write:
        print('[error] --dry-run 与 --write 互斥')
        return 2
    if not args.dry_run and not args.write:
        print('[error] 必须指定 --dry-run 或 --write')
        return 2

    turn_map, order = load_turn_index()
    preflight = preflight_vector_sync(turn_map)
    embedded_rows = load_embeddings(limit=args.limit)
    if args.limit > 0:
        allowed = {row['node_id'] for row in embedded_rows}
        turn_map = {k: v for k, v in turn_map.items() if k in allowed}
        order = [k for k in order if k in allowed]
    temporal = build_temporal_candidates(turn_map, order)
    semantic = build_semantic_candidates(turn_map, embedded_rows, args.top_k)
    candidates, filter_stats = filter_candidates(temporal + semantic, args.min_similarity)
    report = summarize(candidates, filter_stats)
    report.update({
        'top_k': args.top_k,
        'limit': args.limit,
        'min_similarity': args.min_similarity,
        'turns_loaded': len(turn_map),
        'temporal_raw': len(temporal),
        'semantic_raw': len(semantic),
        'vector_preflight': preflight,
    })

    print('# Graph Relation Candidates')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    preview = candidates[:10]
    if preview:
        print('\n# Preview')
        for cand in preview:
            print(
                f"- [{cand['candidate_type']}] {cand['source_node_id']} -> {cand['target_node_id']} "
                f"sim={cand['similarity']} reason={cand['candidate_reason']}"
            )

    SAMPLE_REPORT.parent.mkdir(parents=True, exist_ok=True)
    SAMPLE_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    if args.write:
        write_sqlite(candidates)
        print(f"\n[write] SQLite graph_relation_candidates = {len(candidates)}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
