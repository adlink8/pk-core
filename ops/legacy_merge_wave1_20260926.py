# -*- coding: utf-8 -*-
"""第一波 legacy 补收合并 (2026-09-26)
来源: 权威库孪生(精确同键) + agentsview_normalized.sqlite(09-23冻结快照)
操作: A=标记superseded  B=孪生补漏+标记  C=legacy行补全快照缺失消息
默认干跑; --apply 才写。单事务, 提交前验证, 失败回滚。"""
import sqlite3, hashlib, json, sys, time
from collections import defaultdict

AUTH = r'D:\ADLINK\数据分析\data\canonical\agent\structured\db\agent_conversations.sqlite'
SNAP = r'D:\ADLINK\数据分析\data\canonical\agent\structured\db\agentsview_normalized.sqlite'
APPLY = '--apply' in sys.argv
DB = sys.argv[sys.argv.index('--db') + 1] if '--db' in sys.argv else AUTH

def nkey(content):
    if not content:
        return ''
    return hashlib.md5(''.join(content.split()).encode('utf-8', 'replace')).hexdigest()

auth = sqlite3.connect(DB if APPLY else f'file:{DB}?mode=ro', uri=not APPLY, timeout=90)
auth.execute('PRAGMA busy_timeout=90000')
snap = sqlite3.connect(f'file:{SNAP}?mode=ro', uri=True, timeout=60)

legacy = []
for (sid,) in auth.execute("SELECT canonical_session_id FROM canonical_sessions WHERE agent='legacy'"):
    parts = sid.split('|')[2].split('/', 2)
    legacy.append((sid, parts[1] if len(parts) >= 3 else '?', parts[2] if len(parts) >= 3 else ''))

canon = defaultdict(list)
for sid, content in auth.execute("SELECT canonical_session_id, content FROM canonical_messages"):
    canon[sid].append(nkey(content))

snap_msg = defaultdict(list)
for sid, content in snap.execute("SELECT session_id, content FROM messages"):
    snap_msg[sid].append(nkey(content))
snap_keys = {}
for ssid, skey in snap.execute("SELECT session_id, source_session_id FROM sessions WHERE source_session_id IS NOT NULL AND source_session_id != ''"):
    snap_keys[ssid] = skey

plan = {'A': [], 'B': [], 'C': [], 'noop': []}
for sid, fam, key in legacy:
    leg = canon.get(sid, [])
    lset = set(leg)
    twin_id = f'cs|{fam}|{key}'
    if twin_id in canon and twin_id != sid:
        extras = lset - set(canon[twin_id])
        if not extras:
            plan['A'].append({'legacy': sid, 'twin': twin_id, 'msgs': len(leg)})
        else:
            plan['B'].append({'legacy': sid, 'twin': twin_id, 'extras': len(extras)})
        continue
    best = None
    for ssid, skey in snap_keys.items():
        if not key or key not in skey:
            continue
        strict = skey == key or skey.endswith(key) or key.endswith(skey)
        sset = set(snap_msg.get(ssid, []))
        overlap = len(lset & sset)
        if not strict and overlap < 2:
            continue
        if best is None or overlap > best[1]:
            best = (ssid, overlap, sset)
    if best and (best[2] - lset):
        plan['C'].append({'legacy': sid, 'snap_sid': best[0], 'gain': len(best[2] - lset), 'overlap': best[1]})
    elif best:
        plan['noop'].append({'legacy': sid, 'why': '快照无增量'})
    else:
        plan['noop'].append({'legacy': sid, 'why': '无源(留给第二波客户端侧补收)'})

print(f"分类(严格版): A={len(plan['A'])} B={len(plan['B'])} C={len(plan['C'])} 无操作={len(plan['noop'])}")
print(f"C类预计补入消息: {sum(x['gain'] for x in plan['C']):,}")

if not APPLY:
    json.dump(plan, open(r'D:\ADLINK\数据分析\var\reports\legacy-merge-wave1-plan.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print('干跑完成, 计划已存 var/reports/legacy-merge-wave1-plan.json')
    sys.exit(0)

# ============ 写库模式 ============
print(f'写库目标: {DB}')
plan = json.load(open(r'D:\ADLINK\数据分析\var\reports\legacy-merge-wave1-plan.json', encoding='utf-8'))
cur = auth.cursor()
t0 = time.time()
cur.execute('BEGIN IMMEDIATE')
inserted = superseded = 0
try:
    for item in plan['A']:
        cur.execute("UPDATE canonical_sessions SET merged=1, superseded_by_canonical_id=? WHERE canonical_session_id=?",
                    (item['twin'], item['legacy']))
        superseded += cur.rowcount
    for item in plan['B']:
        leg, twin = item['legacy'], item['twin']
        tparts = twin.split('|', 2)
        tfam, tkey = tparts[1], tparts[2]
        next_ord = (cur.execute("SELECT MAX(ordinal) FROM canonical_messages WHERE canonical_session_id=?", (twin,)).fetchone()[0] or 0) + 1
        tset = {nkey(c) for (c,) in cur.execute("SELECT content FROM canonical_messages WHERE canonical_session_id=?", (twin,)).fetchall()}
        leg_rows = cur.execute(
            "SELECT canonical_message_id, role, content, timestamp, model, is_system, is_sidechain, evidence_scope "
            "FROM canonical_messages WHERE canonical_session_id=?", (leg,)).fetchall()
        n = 0
        for (mid, role, content, ts, model, issys, isside, escope) in leg_rows:
            k = nkey(content)
            if k in tset:
                continue
            tset.add(k)
            cur.execute("""INSERT INTO canonical_messages (canonical_message_id, canonical_session_id, source, source_message_ref,
                           ordinal, role, content, content_length, timestamp, model, is_system, is_sidechain, content_hash, evidence_scope)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (f'cm|{tfam}|{tkey}|bf{next_ord}', twin, 'legacy', mid, next_ord, role, content,
                         len(content or ''), ts, model, issys or 0, isside or 0,
                         hashlib.md5((content or '').encode('utf-8')).hexdigest()[:16], escope))
            next_ord += 1
            n += 1
            inserted += 1
        cur.execute("UPDATE canonical_sessions SET message_count=(SELECT COUNT(*) FROM canonical_messages WHERE canonical_session_id=?), "
                    "user_message_count=(SELECT COUNT(*) FROM canonical_messages WHERE canonical_session_id=? AND role='user') WHERE canonical_session_id=?",
                    (twin, twin, twin))
        cur.execute("UPDATE canonical_sessions SET merged=1, superseded_by_canonical_id=? WHERE canonical_session_id=?", (twin, leg))
        superseded += 1
        item['inserted'] = n
    for item in plan['C']:
        sid, snap_sid = item['legacy'], item['snap_sid']
        next_ord = (cur.execute("SELECT MAX(ordinal) FROM canonical_messages WHERE canonical_session_id=?", (sid,)).fetchone()[0] or 0) + 1
        lset = {nkey(c) for (c,) in cur.execute("SELECT content FROM canonical_messages WHERE canonical_session_id=?", (sid,))}
        n = 0
        rows = snap.execute("""SELECT message_id, role, content, timestamp, model, is_system, is_sidechain, evidence_scope
                               FROM messages WHERE session_id=? ORDER BY timestamp""", (snap_sid,)).fetchall()
        for (mid, role, content, ts, model, issys, isside, escope) in rows:
            k = nkey(content)
            if k in lset:
                continue
            lset.add(k)
            cur.execute("""INSERT INTO canonical_messages (canonical_message_id, canonical_session_id, source, source_message_ref,
                           ordinal, role, content, content_length, timestamp, model, is_system, is_sidechain, content_hash, evidence_scope)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (f'cm|legacy|{sid.split("|", 2)[2]}|bf{next_ord}', sid, 'legacy', mid, next_ord, role, content,
                         len(content or ''), ts, model, issys or 0, isside or 0,
                         hashlib.md5((content or '').encode('utf-8')).hexdigest()[:16], escope or 'user'))
            next_ord += 1
            n += 1
            inserted += 1
        if n:
            cur.execute("UPDATE canonical_sessions SET message_count=(SELECT COUNT(*) FROM canonical_messages WHERE canonical_session_id=?), "
                        "user_message_count=(SELECT COUNT(*) FROM canonical_messages WHERE canonical_session_id=? AND role='user') WHERE canonical_session_id=?",
                        (sid, sid, sid))
        item['inserted'] = n
    v1 = cur.execute("SELECT COUNT(*) FROM canonical_sessions WHERE merged=1 AND superseded_by_canonical_id IS NOT NULL").fetchone()[0]
    assert v1 == superseded, f'supersede 计数不符 {v1} != {superseded}'
    c_gain_total = sum(x['gain'] for x in plan['C'])
    c_actual = sum(x.get('inserted', 0) for x in plan['C'])
    assert c_actual == c_gain_total, f'C插入数不符: 实际 {c_actual} vs 计划 {c_gain_total}'
    assert inserted == c_actual + sum(x.get('inserted', 0) for x in plan['B'])
    assert inserted <= 45000, f'插入量异常: {inserted}'
    total_now = cur.execute("SELECT COUNT(*) FROM canonical_messages WHERE canonical_session_id LIKE 'cs|legacy|%'").fetchone()[0]
    print(f'验证闸通过: superseded={superseded} inserted={inserted} legacy现存行={total_now}')
    auth.commit()
    print(f'OK 已提交: {superseded} 行标记合并, {inserted} 条消息补入, 耗时 {time.time()-t0:.0f}s')
except Exception as e:
    auth.rollback()
    print(f'X 已回滚: {e}')
    raise
finally:
    snap.close()
json.dump(plan, open(r'D:\ADLINK\数据分析\var\reports\legacy-merge-wave1-applied.json', 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1)
