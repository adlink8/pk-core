"""Snippet / token search on canonical_messages for frozen gold hits."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

EVAL = Path("integration/evals/knowledge_units/frozen_test_queries.private.jsonl")
CANON = Path("Agent/structured/db/agent_conversations.sqlite")

cases = [json.loads(l) for l in EVAL.read_text(encoding="utf-8").splitlines() if l.strip()]
con = sqlite3.connect(f"file:{CANON.as_posix()}?mode=ro", uri=True)

gold_map = {}
for case in cases:
    for ref in case.get("gold_evidence_refs") or []:
        if ref in gold_map:
            continue
        row = con.execute(
            "SELECT content FROM canonical_messages WHERE canonical_message_id=?", (ref,)
        ).fetchone()
        if row and row[0]:
            gold_map[ref] = row[0]


def search_snippet(q: str, limit: int = 8) -> list[tuple]:
    # long paste: use mid-length distinctive snippet
    q = q.strip()
    candidates = []
    if len(q) >= 40:
        # try several windows to avoid header-only noise
        for start in (0, 20, 50, 100, min(200, max(0, len(q) - 40))):
            snip = q[start : start + 40]
            if len(snip) < 20:
                continue
            # escape LIKE
            esc = snip.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = con.execute(
                "SELECT canonical_message_id, role, substr(content,1,220) FROM canonical_messages "
                "WHERE content LIKE ? ESCAPE '\\' LIMIT ?",
                (f"%{esc}%", limit),
            ).fetchall()
            candidates.extend(rows)
            if candidates:
                break
    # token AND fallback
    if not candidates:
        toks = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z_][A-Za-z0-9_\-]{3,}", q)
        # prefer rarer-looking tokens: longer first
        toks = sorted(set(toks), key=len, reverse=True)[:4]
        if toks:
            sql = "SELECT canonical_message_id, role, substr(content,1,220) FROM canonical_messages WHERE 1=1"
            params = []
            for t in toks:
                sql += " AND content LIKE ?"
                params.append(f"%{t}%")
            sql += " LIMIT ?"
            params.append(limit)
            candidates = con.execute(sql, params).fetchall()
    # dedupe
    seen = set()
    out = []
    for r in candidates:
        if r[0] in seen:
            continue
        seen.add(r[0])
        out.append(r)
        if len(out) >= limit:
            break
    return out


hits = 0
for case in cases:
    q = case["query"]
    gold_refs = set(case.get("gold_evidence_refs") or [])
    golds = [gold_map[r] for r in gold_refs if r in gold_map]
    rows = search_snippet(q)
    found_rank = None
    for rank, (mid, role, content) in enumerate(rows, 1):
        if mid in gold_refs:
            found_rank = rank
            break
        for g in golds:
            if g and len(g) >= 15 and g[:15] in (content or ""):
                found_rank = rank
                break
            if content and len(content) >= 15 and content[:15] in (g or ""):
                found_rank = rank
                break
        if found_rank:
            break
    if found_rank:
        hits += 1
        print("hit", case["id"], "rank", found_rank)
    else:
        print("miss", case["id"], "n_rows", len(rows), "q0", q[:50].replace("\n", " "))

print(f"snippet R@8: {hits}/{len(cases)} = {hits/len(cases):.2f}")
con.close()
