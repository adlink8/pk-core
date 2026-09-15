"""Phase 15 Wave 0 investigation: I01–I04 (read-only on DBs / chroma).

Writes:
  - assets/evals/knowledge_units/suite_tags.json
  - integration/analysis/ai_context/phase15_hybrid_miss_audit.json
  - integration/analysis/ai_context/phase15_evidence_backfill_feasibility.json
  - integration/analysis/ai_context/phase15_turns_baseline.json
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.core.chroma_client import ChromaClient, ChromaError  # noqa: E402
from personal_knowledge.core.project_paths import (  # noqa: E402
    AGENT_CONVERSATIONS_DB,
    AI_CONTEXT_DIR,
    DB_DIR,
    INTEGRATION_DIR,
    UNIFIED_DB,
)
import personal_knowledge.core.local_embed as local_embed  # noqa: E402

EVAL_DIR = INTEGRATION_DIR / "evals" / "knowledge_units"
ACTIVE_KU_PATH = DB_DIR / "knowledge_index_active.txt"
FROZEN_PATH = EVAL_DIR / "frozen_test_queries.private.jsonl"
DEV_PATH = EVAL_DIR / "dev_queries.private.jsonl"

# Heuristic patterns for suite tagging
CODE_PATTERNS = [
    r"\b(def |class |import |from |public |private |void |function |const |let |var |namespace |using |package )\b",
    r"\b(JSONException|JSONObject|JSONTokener|ArrayAdapter|findViewById|System\.Windows)\b",
    r"\b(InvalidTemplateDeployment|RequestDisallowedByAzure)\b",
    r"\b(traceback|stacktrace|Exception|NullPointer|SyntaxError)\b",
    r"```",
    r"\.java\b|\.py\b|\.tsx?\b|\.cs\b|\.xml\b|\.xaml\b",
    r"R\.id\.|org\.json\.|com\.example\.",
    r"POST /api/|GET /api/",
    r"TypeScript|Tailwind|Next\.js|shadcn",
    r"docstring|模块级|中文文档注释",
    r"spinner1\s*=",
    r"WpfApp1",
    r"src文件夹|前缀我就直接放到",
]
PROFILE_PATTERNS = [
    r"人生|价值观|家庭|职业|生活|个人",
    r"我的项目|github\.com/",
    r"Hermes agent|自进化",
    r"远程ssh|电脑.*手机.*平板",
    r"数据库.*向量库|可视化界面",
    r"用户就是ai|管理员则是后台",
    r"是这种卡吗|程序最多多大",
    r"信息安全.*实验",
    r"偏好|preference|个人|我是谁",
]
GOOGLE_PATTERNS = [
    r"\bgoogle\b|Gmail|YouTube|Chrome|搜索历史|浏览记录|活动日志",
    r"myactivity|My Activity",
]
MIXED_HINTS = [
    r"Phase \d|GSD|工作目录|子代理|planner|必须读取",
    r"Assistant Rules|Available Skills|Codex agent history",
    r"desktop-app|prototype|project-ui",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _score_tag(query: str) -> tuple[str, dict]:
    """Return suite_tag and signal scores based on content heuristics."""
    q = query or ""
    code_hits = sum(1 for p in CODE_PATTERNS if re.search(p, q, re.I))
    profile_hits = sum(1 for p in PROFILE_PATTERNS if re.search(p, q, re.I))
    google_hits = sum(1 for p in GOOGLE_PATTERNS if re.search(p, q, re.I))
    mixed_hits = sum(1 for p in MIXED_HINTS if re.search(p, q, re.I))

    # Structural code signals
    bracey = q.count("{") + q.count("}") + q.count(";")
    pathy = len(re.findall(r"[A-Za-z]:\\[^\s]+|/[a-zA-Z0-9_\-./]+\.(py|ts|tsx|js|java|cs|md)", q))
    if bracey >= 6 or pathy >= 2:
        code_hits += 2
    if re.search(r"^\s*(using |import |public |private |\{)", q[:80], re.M):
        code_hits += 2

    scores = {
        "code": code_hits,
        "profile": profile_hits,
        "google": google_hits,
        "mixed": mixed_hits,
        "bracey": bracey,
        "pathy": pathy,
    }

    if google_hits >= 1 and max(code_hits, profile_hits) == 0:
        return "google", scores

    # Strong code literal (stacktraces, source dumps)
    if code_hits >= 2 and profile_hits == 0:
        return "code", scores
    if code_hits >= 3:
        return "code", scores

    # Project/task agent prompts with file paths + planning = mixed
    if mixed_hits >= 1 and code_hits >= 1:
        return "mixed", scores
    if mixed_hits >= 2:
        return "mixed", scores
    if pathy >= 1 and (mixed_hits >= 1 or "工作目录" in q or "Phase" in q):
        return "mixed", scores

    if profile_hits >= 1 and code_hits == 0:
        return "profile", scores

    if code_hits >= 1 and profile_hits >= 1:
        return "mixed", scores
    if code_hits >= 1:
        return "code", scores
    if mixed_hits >= 1:
        return "mixed", scores
    if profile_hits >= 1:
        return "profile", scores

    # Default: short conversational / personal-ish → profile; long agent tasks → mixed
    if len(q) < 120 and not re.search(r"[\\/].+\.\w{1,5}", q):
        return "profile", scores
    return "mixed", scores


# Manual overrides after spot-check (query id → tag)
# Ensures frozen suite is well-calibrated vs human judgment.
SPOT_CHECK_OVERRIDES: dict[str, str] = {
    # frozen
    "frozen-001": "code",       # Azure ARM JSON error dump
    "frozen-002": "code",       # check Chinese docstrings in py files
    "frozen-003": "mixed",      # product feature task (code + product)
    "frozen-004": "mixed",      # agent rules/skills system prompt
    "frozen-005": "mixed",      # codex review history
    "frozen-006": "profile",    # personal/values chat
    "frozen-007": "profile",    # personal github project
    "frozen-008": "mixed",      # product role design (user/admin/agent)
    "frozen-009": "code",       # Android Java spinner
    "frozen-010": "mixed",      # fullstack app phase2 frontend agent task
    "frozen-011": "profile",    # hardware card capability question (personal context)
    "frozen-012": "code",       # package prefix / src layout
    "frozen-013": "profile",    # family/values philosophy
    "frozen-014": "profile",    # personal device networking (ssh tablets)
    "frozen-015": "code",       # JSONException stacktrace
    "frozen-016": "code",       # WPF C# source
    "frozen-017": "mixed",      # 信息安全 lab writeup task
    "frozen-018": "mixed",      # GSD planner fullstack app phase1
    "frozen-019": "profile",    # which DB/vector UI to download (personal setup)
    "frozen-020": "profile",    # Hermes agent awareness
    # dev (spot-check)
    "dev-001": "code",          # Python fill-in exercise comments
    "dev-002": "profile",       # token usage personal
    "dev-003": "mixed",         # preferences consolidate
    "dev-004": "mixed",         # turn aborted system msg
    "dev-005": "code",          # JSONException stack
    "dev-006": "mixed",         # GSD phase5 fullstack app
    "dev-007": "profile",       # DB architecture questions
    "dev-008": "mixed",         # cb_summary agent context
    "dev-009": "mixed",         # roadmap issue cleanup
    "dev-010": "code",          # read py files for comments
    "dev-011": "mixed",         # gpt tunnel mcp
    "dev-012": "code",          # cleanup unused code
    "dev-013": "mixed",         # memory graph browser
    "dev-014": "mixed",         # codex URL config
    "dev-015": "mixed",         # gsd research mem0 etc
    "dev-016": "mixed",         # GSD phase5 wave3
    "dev-017": "profile",       # marriage/family economics
    "dev-018": "profile",       # memory experiment roadmap
    "dev-019": "mixed",         # phase8 plan agent
    "dev-020": "profile",       # ESP32 trend chat
}


def run_i01() -> dict:
    frozen = _load_jsonl(FROZEN_PATH)
    dev = _load_jsonl(DEV_PATH)
    tags: dict[str, str] = {}
    rationales: dict[str, dict] = {}
    for case in frozen + dev:
        qid = case["id"]
        heuristic, scores = _score_tag(case.get("query", ""))
        tag = SPOT_CHECK_OVERRIDES.get(qid, heuristic)
        tags[qid] = tag
        rationales[qid] = {
            "tag": tag,
            "heuristic": heuristic,
            "override": qid in SPOT_CHECK_OVERRIDES,
            "scores": scores,
            "query_preview": (case.get("query") or "")[:100].replace("\n", " "),
        }
    counts = dict(Counter(tags.values()))
    out = {
        "generated_at": _now_iso(),
        "method": "content_heuristics + spot_check_overrides",
        "tags": tags,
        "counts": counts,
        "counts_by_split": {
            "frozen_test": dict(Counter(tags[c["id"]] for c in frozen)),
            "dev": dict(Counter(tags[c["id"]] for c in dev)),
        },
        "rationales": rationales,
    }
    path = EVAL_DIR / "suite_tags.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[I01] wrote {path} counts={counts}")
    return out


def _load_gold_contents(cases: list[dict]) -> dict[str, str]:
    gold: dict[str, str] = {}
    if not AGENT_CONVERSATIONS_DB.exists():
        return gold
    con = sqlite3.connect(f"file:{AGENT_CONVERSATIONS_DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    refs = set()
    for case in cases:
        for r in case.get("gold_evidence_refs") or []:
            refs.add(r)
    for ref in refs:
        row = con.execute(
            "SELECT content FROM canonical_messages WHERE canonical_message_id=?",
            (ref,),
        ).fetchone()
        if row and row["content"]:
            gold[ref] = row["content"][:200]
    con.close()
    return gold


def _match_found(
    ids: list,
    documents: list,
    metadatas: list,
    gold_refs: set[str],
    gold_snippets: list[str],
) -> int | None:
    for rank, (rid, doc, meta) in enumerate(zip(ids, documents, metadatas), 1):
        if rid in gold_refs:
            return rank
        if meta and isinstance(meta, dict):
            for key in ("source_message_ref", "canonical_message_id", "message_id", "ref"):
                val = meta.get(key) or ""
                if val and val in gold_refs:
                    return rank
            # some collections store cm| in id
            smr = meta.get("source_message_ref")
            if smr and smr in gold_refs:
                return rank
        if doc and gold_snippets:
            for snippet in gold_snippets:
                if len(snippet) >= 15 and snippet[:15] in (doc or ""):
                    return rank
                # looser: first 30 chars of gold without whitespace
                sn = re.sub(r"\s+", "", snippet)[:20]
                dd = re.sub(r"\s+", "", doc or "")
                if len(sn) >= 12 and sn in dd:
                    return rank
    return None


def _query_collection(coll, embedding, n=5):
    try:
        result = coll.query(
            query_embeddings=[embedding],
            n_results=n,
            include=["metadatas", "documents", "distances"],
        )
    except ChromaError as e:
        return {"error": str(e)[:200], "ids": [], "docs": [], "metas": [], "dists": []}
    ids = (result.get("ids") or [[]])[0] or []
    docs = (result.get("documents") or [[]])[0] or []
    metas = (result.get("metadatas") or [[]])[0] or []
    dists = (result.get("distances") or [[]])[0] or []
    return {"ids": ids, "docs": docs, "metas": metas, "dists": dists}


def _top1_preview(res: dict) -> dict:
    if not res.get("ids"):
        return {"id": None, "distance": None, "source": None, "meta_keys": [], "doc_preview": None}
    meta = res["metas"][0] if res["metas"] else {}
    if not isinstance(meta, dict):
        meta = {}
    source = (
        meta.get("source")
        or meta.get("source_agent")
        or meta.get("agent")
        or meta.get("unit_type")
        or meta.get("event_type")
    )
    return {
        "id": res["ids"][0],
        "distance": res["dists"][0] if res["dists"] else None,
        "source": source,
        "meta": {k: (str(v)[:80] if not isinstance(v, (int, float, bool)) else v)
                 for k, v in list(meta.items())[:12]},
        "doc_preview": (res["docs"][0] or "")[:160].replace("\n", " ") if res["docs"] else None,
    }


def run_i02_i04(suite_tags: dict[str, str]) -> tuple[dict, dict]:
    active_ku = ACTIVE_KU_PATH.read_text(encoding="utf-8").strip()
    cases = _load_jsonl(FROZEN_PATH)
    gold_contents = _load_gold_contents(cases)

    ok, msg, dim = local_embed.verify_model()
    if not ok:
        raise RuntimeError(f"embed model unavailable: {msg}")

    client = ChromaClient()
    collections = {
        "personal_events": client.get_or_create_collection("personal_events"),
        "knowledge_units": client.get_or_create_collection(active_ku),
        "conversation_turns": client.get_or_create_collection("conversation_turns"),
    }
    coll_counts = {k: v.count() for k, v in collections.items()}

    per_query = []
    # I04 accumulators
    by_tag_stats: dict[str, dict] = defaultdict(lambda: {
        "n": 0,
        "personal_events_hits": 0,
        "knowledge_units_hits": 0,
        "conversation_turns_hits": 0,
        "personal_events_top1_dists": [],
        "knowledge_units_top1_dists": [],
        "conversation_turns_top1_dists": [],
        "personal_events_mrr": 0.0,
        "knowledge_units_mrr": 0.0,
        "conversation_turns_mrr": 0.0,
    })

    for case in cases:
        qid = case["id"]
        query = case["query"]
        gold_refs = set(case.get("gold_evidence_refs") or [])
        gold_snippets = [gold_contents[r] for r in gold_refs if r in gold_contents]
        tag = suite_tags.get(qid, "mixed")

        emb = local_embed.embed(query)
        row = {
            "id": qid,
            "suite_tag": tag,
            "gold_evidence_refs": list(gold_refs),
            "gold_content_available": len(gold_snippets) > 0,
            "query_preview": (query or "")[:100].replace("\n", " "),
            "collections": {},
        }

        any_hit = []
        for cname, coll in collections.items():
            res = _query_collection(coll, emb, n=5)
            if res.get("error"):
                row["collections"][cname] = {"error": res["error"]}
                continue
            rank = _match_found(res["ids"], res["docs"], res["metas"], gold_refs, gold_snippets)
            top1 = _top1_preview(res)
            row["collections"][cname] = {
                "found_rank": rank,
                "gold_matched": rank is not None,
                "top1": top1,
                "top5_ids": res["ids"][:5],
                "top5_distances": res["dists"][:5],
            }
            if rank is not None:
                any_hit.append(cname)

            # I04 stats
            st = by_tag_stats[tag]
            # n incremented once per query outside
            key_hits = f"{cname}_hits"
            key_mrr = f"{cname}_mrr"
            key_dist = f"{cname}_top1_dists"
            if rank is not None:
                st[key_hits] += 1
                st[key_mrr] += 1.0 / rank
            if top1.get("distance") is not None:
                st[key_dist].append(float(top1["distance"]))

        st = by_tag_stats[tag]
        st["n"] += 1

        row["found_on"] = any_hit
        row["miss_all"] = len(any_hit) == 0
        # hybrid legacy style: ku top1 + pe fill
        ku_c = row["collections"].get("knowledge_units", {})
        pe_c = row["collections"].get("personal_events", {})
        row["hybrid_legacy_hit"] = bool(ku_c.get("gold_matched") or pe_c.get("gold_matched"))
        row["would_need_turns"] = (not row["hybrid_legacy_hit"]) and bool(
            row["collections"].get("conversation_turns", {}).get("gold_matched")
        )
        per_query.append(row)
        print(
            f"  {qid} tag={tag} hits={any_hit or ['NONE']} "
            f"pe={pe_c.get('found_rank')} ku={ku_c.get('found_rank')} "
            f"ct={row['collections'].get('conversation_turns', {}).get('found_rank')}"
        )

    # Summaries I02
    n = len(per_query)
    def hit_rate(cname: str) -> float:
        return round(sum(1 for r in per_query if r["collections"].get(cname, {}).get("gold_matched")) / max(n, 1), 4)

    miss_all = [r["id"] for r in per_query if r["miss_all"]]
    pe_only = [r["id"] for r in per_query if r["collections"].get("personal_events", {}).get("gold_matched")
               and not r["collections"].get("knowledge_units", {}).get("gold_matched")]
    ku_only = [r["id"] for r in per_query if r["collections"].get("knowledge_units", {}).get("gold_matched")
               and not r["collections"].get("personal_events", {}).get("gold_matched")]
    turns_rescues = [r["id"] for r in per_query if r.get("would_need_turns")]
    hybrid_hits = sum(1 for r in per_query if r["hybrid_legacy_hit"])

    # top1 source distribution for PE
    pe_sources = Counter()
    for r in per_query:
        src = (r["collections"].get("personal_events", {}).get("top1") or {}).get("source")
        pe_sources[str(src)] += 1

    i02 = {
        "generated_at": _now_iso(),
        "active_ku_collection": active_ku,
        "collection_counts": coll_counts,
        "n_queries": n,
        "summary": {
            "personal_events_r_at_5": hit_rate("personal_events"),
            "knowledge_units_r_at_5": hit_rate("knowledge_units"),
            "conversation_turns_r_at_5": hit_rate("conversation_turns"),
            "hybrid_legacy_r_at_5": round(hybrid_hits / max(n, 1), 4),
            "miss_all_count": len(miss_all),
            "miss_all_ids": miss_all,
            "pe_only_hits": pe_only,
            "ku_only_hits": ku_only,
            "turns_rescue_ids": turns_rescues,
            "personal_events_top1_source_dist": dict(pe_sources),
        },
        "per_query": per_query,
    }

    # I04 baseline by suite_tag
    by_tag_out = {}
    overall = {
        "n": n,
        "personal_events_r_at_5": hit_rate("personal_events"),
        "knowledge_units_r_at_5": hit_rate("knowledge_units"),
        "conversation_turns_r_at_5": hit_rate("conversation_turns"),
        "hybrid_legacy_r_at_5": round(hybrid_hits / max(n, 1), 4),
    }
    for tag, st in sorted(by_tag_stats.items()):
        nn = max(st["n"], 1)
        def avg(xs):
            return round(sum(xs) / len(xs), 4) if xs else None
        by_tag_out[tag] = {
            "n": st["n"],
            "personal_events": {
                "r_at_5": round(st["personal_events_hits"] / nn, 4),
                "mrr_at_5": round(st["personal_events_mrr"] / nn, 4),
                "avg_top1_distance": avg(st["personal_events_top1_dists"]),
            },
            "knowledge_units": {
                "r_at_5": round(st["knowledge_units_hits"] / nn, 4),
                "mrr_at_5": round(st["knowledge_units_mrr"] / nn, 4),
                "avg_top1_distance": avg(st["knowledge_units_top1_dists"]),
            },
            "conversation_turns": {
                "r_at_5": round(st["conversation_turns_hits"] / nn, 4),
                "mrr_at_5": round(st["conversation_turns_mrr"] / nn, 4),
                "avg_top1_distance": avg(st["conversation_turns_top1_dists"]),
            },
        }

    i04 = {
        "generated_at": _now_iso(),
        "active_ku_collection": active_ku,
        "collection_counts": coll_counts,
        "method": "gold_evidence_refs match via id/source_message_ref/content snippet on top-5",
        "overall": overall,
        "by_suite_tag": by_tag_out,
        "notes": [
            "conversation_turns has 3601 docs (dialogue turns), personal_events is NOT full dialogue",
            "code-tagged queries often need literal fallback; compare turns vs personal_events R@5",
            "hybrid_legacy = hit on KU or personal_events (current production hybrid raw path)",
        ],
    }

    p02 = AI_CONTEXT_DIR / "phase15_hybrid_miss_audit.json"
    p04 = AI_CONTEXT_DIR / "phase15_turns_baseline.json"
    p02.write_text(json.dumps(i02, ensure_ascii=False, indent=2), encoding="utf-8")
    p04.write_text(json.dumps(i04, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[I02] wrote {p02}")
    print(f"[I04] wrote {p04}")
    print(f"[I02] summary: {json.dumps(i02['summary'], ensure_ascii=False)}")
    print(f"[I04] overall: {json.dumps(overall, ensure_ascii=False)}")
    return i02, i04


def run_i03() -> dict:
    """Count knowledge_units without evidence; of those, how many have resolvable source_message_ref."""
    if not UNIFIED_DB.exists():
        raise FileNotFoundError(UNIFIED_DB)

    con = sqlite3.connect(f"file:{UNIFIED_DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    ku_tables = [t for t in tables if "knowledge_unit" in t.lower()]
    print("[I03] knowledge-related tables:", ku_tables)

    # Prefer knowledge_units + knowledge_unit_evidence if present
    has_ku = "knowledge_units" in tables
    has_ev = "knowledge_unit_evidence" in tables
    has_canon = "canonical_knowledge_units" in tables

    result: dict = {
        "generated_at": _now_iso(),
        "db": str(UNIFIED_DB),
        "agent_conversations_db": str(AGENT_CONVERSATIONS_DB),
        "tables_found": ku_tables,
    }

    if has_ku:
        total_ku = con.execute("SELECT COUNT(*) AS c FROM knowledge_units").fetchone()["c"]
        # units with at least one evidence row
        if has_ev:
            # detect join key
            ev_cols = [r[1] for r in con.execute("PRAGMA table_info(knowledge_unit_evidence)").fetchall()]
            ku_cols = [r[1] for r in con.execute("PRAGMA table_info(knowledge_units)").fetchall()]
            result["knowledge_unit_evidence_columns"] = ev_cols
            result["knowledge_units_columns"] = ku_cols

            # common patterns
            if "unit_id" in ev_cols and "unit_id" in ku_cols:
                join_key = "unit_id"
            elif "knowledge_unit_id" in ev_cols:
                join_key = "knowledge_unit_id"
            elif "ku_id" in ev_cols:
                join_key = "ku_id"
            else:
                join_key = ev_cols[0] if ev_cols else "unit_id"

            # knowledge_units PK
            if "unit_id" in ku_cols:
                ku_pk = "unit_id"
            elif "id" in ku_cols:
                ku_pk = "id"
            elif "knowledge_unit_id" in ku_cols:
                ku_pk = "knowledge_unit_id"
            else:
                ku_pk = ku_cols[0]

            with_ev = con.execute(
                f"""
                SELECT COUNT(DISTINCT k.{ku_pk}) AS c
                FROM knowledge_units k
                INNER JOIN knowledge_unit_evidence e ON e.{join_key} = k.{ku_pk}
                """
            ).fetchone()["c"]
            without_ev = total_ku - with_ev

            # of without evidence: non-empty source_message_ref
            if "source_message_ref" in ku_cols:
                without_rows = con.execute(
                    f"""
                    SELECT k.{ku_pk} AS uid, k.source_message_ref AS ref
                    FROM knowledge_units k
                    LEFT JOIN knowledge_unit_evidence e ON e.{join_key} = k.{ku_pk}
                    WHERE e.{join_key} IS NULL
                      AND k.source_message_ref IS NOT NULL
                      AND TRIM(k.source_message_ref) != ''
                    """
                ).fetchall()
                no_ref = con.execute(
                    f"""
                    SELECT COUNT(*) AS c
                    FROM knowledge_units k
                    LEFT JOIN knowledge_unit_evidence e ON e.{join_key} = k.{ku_pk}
                    WHERE e.{join_key} IS NULL
                      AND (k.source_message_ref IS NULL OR TRIM(k.source_message_ref) = '')
                    """
                ).fetchone()["c"]
            else:
                without_rows = []
                no_ref = without_ev

            refs = [r["ref"] for r in without_rows if r["ref"]]
            unique_refs = sorted(set(refs))

            # check existence in canonical_messages
            exists_count = 0
            missing_count = 0
            sample_missing = []
            if AGENT_CONVERSATIONS_DB.exists() and unique_refs:
                ccon = sqlite3.connect(
                    f"file:{AGENT_CONVERSATIONS_DB.as_posix()}?mode=ro", uri=True
                )
                for ref in unique_refs:
                    row = ccon.execute(
                        "SELECT 1 FROM canonical_messages WHERE canonical_message_id=? LIMIT 1",
                        (ref,),
                    ).fetchone()
                    if row:
                        exists_count += 1
                    else:
                        missing_count += 1
                        if len(sample_missing) < 10:
                            sample_missing.append(ref)
                ccon.close()

            # map unique ref existence back to unit count
            # approximate: units whose ref is in existing set
            if unique_refs and AGENT_CONVERSATIONS_DB.exists():
                ccon = sqlite3.connect(
                    f"file:{AGENT_CONVERSATIONS_DB.as_posix()}?mode=ro", uri=True
                )
                existing_set = set()
                # batch
                for i in range(0, len(unique_refs), 500):
                    chunk = unique_refs[i : i + 500]
                    placeholders = ",".join("?" * len(chunk))
                    for r in ccon.execute(
                        f"SELECT canonical_message_id FROM canonical_messages "
                        f"WHERE canonical_message_id IN ({placeholders})",
                        chunk,
                    ):
                        existing_set.add(r[0])
                ccon.close()
                units_resolvable = sum(1 for r in without_rows if r["ref"] in existing_set)
            else:
                units_resolvable = 0
                existing_set = set()

            result["knowledge_units"] = {
                "total": total_ku,
                "with_evidence_rows": with_ev,
                "without_evidence_rows": without_ev,
                "without_evidence_pct": round(without_ev / max(total_ku, 1), 4),
                "without_ev_and_empty_ref": no_ref,
                "without_ev_and_nonempty_ref": len(without_rows),
                "unique_nonempty_refs_among_missing_ev": len(unique_refs),
                "unique_refs_exist_in_canonical_messages": exists_count,
                "unique_refs_missing_from_canonical": missing_count,
                "units_without_ev_with_resolvable_ref": units_resolvable,
                "auto_backfill_feasible_pct_of_missing_ev": round(
                    units_resolvable / max(without_ev, 1), 4
                ),
                "auto_backfill_feasible_pct_of_all_ku": round(
                    units_resolvable / max(total_ku, 1), 4
                ),
                "join_key": join_key,
                "ku_pk": ku_pk,
                "sample_missing_refs": sample_missing,
            }
        else:
            result["knowledge_units"] = {
                "total": total_ku,
                "error": "knowledge_unit_evidence table not found",
            }

    # Also report canonical layer if present
    if has_canon:
        c_total = con.execute("SELECT COUNT(*) AS c FROM canonical_knowledge_units").fetchone()["c"]
        c_cols = [r[1] for r in con.execute("PRAGMA table_info(canonical_knowledge_units)").fetchall()]
        result["canonical_knowledge_units"] = {"total": c_total, "columns": c_cols}
        if "source_message_ref" in c_cols and has_ev:
            # evidence may link to unit_id of draft or canonical — report draft-focused primarily
            pass

    # Evidence coverage overall (units with evidence / all)
    if "knowledge_units" in result and "with_evidence_rows" in result.get("knowledge_units", {}):
        ku = result["knowledge_units"]
        result["headline"] = {
            "evidence_coverage": round(ku["with_evidence_rows"] / max(ku["total"], 1), 4),
            "missing_evidence": ku["without_evidence_rows"],
            "auto_backfill_candidates": ku["units_without_ev_with_resolvable_ref"],
            "auto_backfill_pct_of_missing": ku["auto_backfill_feasible_pct_of_missing_ev"],
            "auto_backfill_pct_of_all": ku["auto_backfill_feasible_pct_of_all_ku"],
            "post_backfill_projected_coverage": round(
                (ku["with_evidence_rows"] + ku["units_without_ev_with_resolvable_ref"])
                / max(ku["total"], 1),
                4,
            ),
        }

    con.close()
    path = AI_CONTEXT_DIR / "phase15_evidence_backfill_feasibility.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[I03] wrote {path}")
    print(f"[I03] headline: {json.dumps(result.get('headline', result), ensure_ascii=False)[:500]}")
    return result


def main():
    print("=== Phase 15 Wave 0 I01–I04 ===")
    i01 = run_i01()
    print("=== I03 evidence feasibility ===")
    i03 = run_i03()
    print("=== I02/I04 retrieval audit (embed + chroma) ===")
    i02, i04 = run_i02_i04(i01["tags"])
    print("=== DONE ===")
    return {"i01": i01["counts"], "i02": i02["summary"], "i03": i03.get("headline"), "i04": i04["overall"]}


if __name__ == "__main__":
    main()
