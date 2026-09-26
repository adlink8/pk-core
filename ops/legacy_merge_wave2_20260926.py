# -*- coding: utf-8 -*-
"""第二波 legacy 补收·合并 (2026-09-26)
读取 legacy-wave2-bundle.json(对账产物), 把客户端找回的缺失消息补入对应 legacy 行。
默认干跑; --apply 才写。单事务, 提交前验证, 失败回滚。"""
import sqlite3, hashlib, json, sys, time

AUTH = r'D:\ADLINK\数据分析\data\canonical\agent\structured\db\agent_conversations.sqlite'
BUNDLE = r'D:\ADLINK\数据分析\var\reports\legacy-wave2-bundle.json'
APPLY = '--apply' in sys.argv
DB = sys.argv[sys.argv.index('--db') + 1] if '--db' in sys.argv else AUTH

bundle = json.load(open(BUNDLE, encoding='utf-8'))
items = [it for fam in bundle.values() for it in fam]
total_gain = sum(it['gain'] for it in items)
print(f'补料包: {len(items)} 个会话, 预计补入 {total_gain:,} 条')

if not APPLY:
    print('干跑结束(对账已含增益数)。')
    sys.exit(0)

auth = sqlite3.connect(DB, timeout=90)
auth.execute('PRAGMA busy_timeout=90000')
cur = auth.cursor()
t0 = time.time()
cur.execute('BEGIN IMMEDIATE')
inserted = 0
per_fam = {}
try:
    for it in items:
        sid = it['legacy']
        fam_key = sid.split('|', 2)[2]              # cs/<fam>/<key>
        next_ord = (cur.execute("SELECT MAX(ordinal) FROM canonical_messages WHERE canonical_session_id=?",
                                (sid,)).fetchone()[0] or 0) + 1
        lset = {hashlib.md5(''.join((c or '').split()).encode('utf-8', 'replace')).hexdigest()
                for (c,) in cur.execute("SELECT content FROM canonical_messages WHERE canonical_session_id=?", (sid,)).fetchall()}
        n = 0
        for m in it['messages']:
            k = hashlib.md5(''.join((m['text'] or '').split()).encode('utf-8', 'replace')).hexdigest()
            if not k or k in lset:
                continue
            lset.add(k)
            content = m['text'] or ''
            cur.execute("""INSERT INTO canonical_messages (canonical_message_id, canonical_session_id, source, source_message_ref,
                           ordinal, role, content, content_length, timestamp, model, is_system, is_sidechain, content_hash, evidence_scope)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (f'cm|legacy|{fam_key}|bf{next_ord}', sid, 'legacy',
                         f"client:{it['src_file'][:180]}", next_ord, m['role'] or 'system',
                         content, len(content), m['ts'], None, 0, 0,
                         hashlib.md5(content.encode('utf-8')).hexdigest()[:16], m['role'] or 'system'))
            next_ord += 1
            n += 1
            inserted += 1
        fam = sid.split('|')[2].split('/')[1]
        per_fam[fam] = per_fam.get(fam, 0) + n
        if n:
            cur.execute("UPDATE canonical_sessions SET message_count=(SELECT COUNT(*) FROM canonical_messages WHERE canonical_session_id=?), "
                        "user_message_count=(SELECT COUNT(*) FROM canonical_messages WHERE canonical_session_id=? AND role='user') WHERE canonical_session_id=?",
                        (sid, sid, sid))
    expected = sum(x['gain'] for x in items)
    print(f'验证闸: 计划 {expected:,} vs 实插 {inserted:,} (差额=补料包内未过指纹闸的重复, 应≈0)')
    if inserted > 120_000:
        raise AssertionError(f'插入量异常: {inserted}')
    auth.commit()
    print(f'OK 已提交: {inserted:,} 条补入, 耗时 {time.time()-t0:.0f}s')
    print('按家族:', dict(sorted(per_fam.items(), key=lambda x: -x[1])))
except Exception as e:
    auth.rollback()
    print(f'X 已回滚: {e}')
    raise
