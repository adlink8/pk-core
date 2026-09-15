"""Quick probe: View FTS vs frozen gold content match."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

EVAL = Path("integration/evals/knowledge_units/frozen_test_queries.private.jsonl")
VIEW = Path.home() / ".agentsview" / "sessions.db"
CANON = Path("Agent/structured/db/agent_conversations.sqlite")


def fts_query(q: str) -> str:
    # extract meaningful tokens
    parts = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z_][A-Za-z0-9_\-]{2,}", q)
    # dedupe preserve order
    seen = set()
    toks = []
    for p in parts:
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        toks.append(p)
        if len(toks) >= 8:
            break
    if not toks:
        return ""
    # FTS OR join, quote tokens with special chars
    out = []
    for t in toks[:6]:
        if re.search(r"[^\w\u4e00-\u9fff]", t, re.U):
            out.append(f'"{t}"')
        else:
            out.append(t)
    return " OR ".join(out)


cases = [json.loads(l) for l in EVAL.read_text(encoding="utf-8").splitlines() if l.strip()]
con_v = sqlite3.connect(f"file:{VIEW.as_posix()}?mode=ro", uri=True)
con_v.execute("PRAGMA query_only=ON")
con_c = sqlite3.connect(f"file:{CANON.as_posix()}?mode=ro", uri=True)

# load gold contents
gold_map = {}
for case in cases:
    for ref in case.get("gold_evidence_refs") or []:
        if ref in gold_map:
            continue
        row = con_c.execute(
            "SELECT content FROM canonical_messages WHERE canonical_message_id=?",
            (ref,),
        ).fetchone()
        if row and row[0]:
            gold_map[ref] = row[0][:200]

hits = 0
for case in cases:
    q = case["query"]
    fq = fts_query(q)
    golds = [gold_map[r] for r in case.get("gold_evidence_refs") or [] if r in gold_map]
    found = False
    if fq:
        try:
            rows = con_v.execute(
                "SELECT m.id, m.role, substr(m.content,1,200) FROM messages_fts f "
                "JOIN messages m ON m.id=f.rowid WHERE messages_fts MATCH ? LIMIT 10",
                (fq,),
            ).fetchall()
        except Exception as e:
            rows = []
            print("fts err", case["id"], e, "q=", fq[:80])
        for rid, role, content in rows:
            for g in golds:
                if len(g) >= 15 and g[:15] in (content or ""):
                    found = True
                    break
                # also reverse snippet
                if content and len(content) >= 15 and content[:15] in g:
                    found = True
                    break
            if found:
                break
    if found:
        hits += 1
    else:
        print("miss", case["id"], "fts_q=", fq[:60] if fq else None)

print(f"FTS content-match R@10 style: {hits}/{len(cases)} = {hits/len(cases):.2f}")
con_v.close()
con_c.close()
