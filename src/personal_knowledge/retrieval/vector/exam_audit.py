# -*- coding: utf-8 -*-
# 考卷数据质量审计（只读，不写入任何库）
# 用法: python exam_audit.py [--exam docs/retrieval/exam_v2.json] [--db var/db/conversation_vector.sqlite] [--json out.json]
# 检查项: 结构完整性 / 重复题 / answer_set 前缀有效性+唯一性 / agent 家族一致
#         / 家族覆盖缺口 / 原话层系统上下文污染率
# 退出码: 存在 ERROR 级问题=1，否则 0
import sqlite3, json, argparse, sys
from collections import Counter

DEF_DB = r"D:/ADLINK/数据分析/var/db/conversation_vector.sqlite"
DEF_EXAM = r"D:/ADLINK/数据分析/docs/retrieval/exam_v2.json"
# 原话层垃圾签名：命中即判为系统上下文泄漏（非用户真实发言）
JUNK_SIGS = ("<system-reminder", "<user_info", "<environment_context",
             '<data-role="user-context">', "<additional_data>")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exam", default=DEF_EXAM)
    ap.add_argument("--db", default=DEF_DB)
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    P = (lambda *a: None) if args.quiet else (lambda *a: print(*a, flush=True))

    con = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    valid_sids = set(r[0] for r in con.execute(
        "SELECT DISTINCT canonical_session_id FROM summaries WHERE status='ok'"))
    valid_sids |= set(r[0] for r in con.execute(
        "SELECT DISTINCT canonical_session_id FROM quote_chunks"))

    errors, warns = [], []
    exam = json.load(open(args.exam, encoding="utf-8"))
    meta = [e for e in exam if isinstance(e, dict) and not e.get("q")]
    qs = [e for e in exam if isinstance(e, dict) and e.get("q")]
    P(f"== 结构: 条目 {len(exam)} = meta {len(meta)} + 题目 {len(qs)}")
    if len(meta) > 1:
        warns.append(f"meta 条目 {len(meta)} 个（惯例 1 个）")
    for i, e in enumerate(exam):
        if not isinstance(e, dict):
            errors.append(f"[{i}] 非 dict 条目")
            continue
        if e.get("q"):
            for f in ("answer_set", "topic", "agent"):
                if not e.get(f):
                    errors.append(f"[{i}] 缺字段 {f}: {str(e.get('q'))[:30]}")
            if e.get("answer_set") and not isinstance(e["answer_set"], list):
                errors.append(f"[{i}] answer_set 不是列表")

    qcounter = Counter(e["q"].strip() for e in qs)
    dups = [q for q, c in qcounter.items() if c > 1]
    if dups:
        errors.append(f"重复题 {len(dups)} 道: {[q[:25] for q in dups]}")
    P(f"== 重复题: {len(dups)}")

    n_prefix = n_bad = n_amb = n_src_mismatch = n_agent_mismatch = 0
    amb_examples = []
    for i, e in enumerate(qs):
        for p in e.get("answer_set", []):
            n_prefix += 1
            matches = [s for s in valid_sids if s.startswith(p)]
            if not matches:
                n_bad += 1
                errors.append(f"[{i}] 前缀语料零匹配: {p} | {e['q'][:30]}")
            elif len(matches) > 1:
                n_amb += 1
                amb_examples.append((p, len(matches)))
                warns.append(f"[{i}] 前缀歧义({len(matches)}会话): {p}")
        if e.get("source") and e.get("answer_set") and e["source"] != e["answer_set"][0]:
            n_src_mismatch += 1
            warns.append(f"[{i}] source != answer_set[0]")
        if e.get("agent") and e.get("answer_set"):
            fam = e["answer_set"][0].split("|")[1] if "|" in e["answer_set"][0] else "?"
            if fam != e["agent"]:
                n_agent_mismatch += 1
                errors.append(f"[{i}] agent={e['agent']} 但前缀家族={fam}")
    P(f"== 前缀: 共 {n_prefix} 个, 零匹配 {n_bad}, 歧义 {n_amb}, "
      f"source 不一致 {n_src_mismatch}, agent 家族不符 {n_agent_mismatch}")

    fam_corpus = Counter()
    for s in valid_sids:
        fam_corpus[s.split("|")[1] if "|" in s else s] += 1
    fam_exam = Counter(e.get("agent", "?") for e in qs)
    P("== 覆盖（家族: 题数/语料会话数）:")
    for fam, c in fam_corpus.most_common():
        rate = fam_exam.get(fam, 0) / c
        mark = "  <-- 零覆盖" if fam_exam.get(fam, 0) == 0 else ""
        P(f"   {fam:12s} {fam_exam.get(fam, 0):3d} 题 / {c:4d} 会话 ({rate:.2f}){mark}")
    for fam in fam_exam:
        if fam not in fam_corpus:
            errors.append(f"家族 {fam} 在语料中不存在（前缀全失效？）")

    n_q, n_junk = 0, 0
    junk_sessions = set()
    for cid, text in con.execute("SELECT canonical_session_id, text FROM quote_chunks"):
        n_q += 1
        if text.lstrip().lower().startswith(JUNK_SIGS) or \
           any(sig in text.lstrip()[:100].lower() for sig in JUNK_SIGS):
            n_junk += 1
            junk_sessions.add(cid)
    P(f"== 语料污染: 原话块 {n_q}, 系统上下文垃圾 {n_junk} "
      f"({n_junk*100/max(n_q,1):.0f}%), 波及会话 {len(junk_sessions)}")

    P(f"\n== 结论: ERROR {len(errors)} / WARN {len(warns)}")
    for e in errors:
        P(f"   [ERROR] {e}")
    for w in warns[:20]:
        P(f"   [WARN]  {w}")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"exam": args.exam, "questions": len(qs), "meta": len(meta),
                       "dups": dups, "bad_prefix": n_bad, "ambiguous_prefix": n_amb,
                       "agent_mismatch": n_agent_mismatch,
                       "fam_corpus": dict(fam_corpus), "fam_exam": dict(fam_exam),
                       "junk_quotes": n_junk, "total_quotes": n_q,
                       "junk_sessions": len(junk_sessions),
                       "errors": errors, "warns": warns}, f, ensure_ascii=False, indent=2)
        P(f"明细已写 {args.json_out}")
    sys.exit(1 if errors else 0)

if __name__ == "__main__":
    main()
