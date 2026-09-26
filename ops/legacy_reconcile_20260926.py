# -*- coding: utf-8 -*-
"""Step1 只读对账: 1,224 个 legacy 会话分类
A=有原生孪生且孪生⊇legacy  B=有孪生但legacy有额外消息  C=无孪生但快照有内容  D=无源"""
import sqlite3, hashlib, json, re
from collections import defaultdict

AUTH = r'D:\ADLINK\数据分析\data\canonical\agent\structured\db\agent_conversations.sqlite'
SNAP = r'D:\ADLINK\数据分析\data\canonical\agent\structured\db\agentsview_normalized.sqlite'

def nkey(content):
    if not content: return ''
    return hashlib.md5(''.join(content.split()).encode('utf-8', 'replace')).hexdigest()

auth = sqlite3.connect(f'file:{AUTH}?mode=ro', uri=True, timeout=60)
snap = sqlite3.connect(f'file:{SNAP}?mode=ro', uri=True, timeout=60)

# 1. legacy 会话清单
legacy = []
for (sid,) in auth.execute("SELECT canonical_session_id FROM canonical_sessions WHERE agent='legacy'"):
    parts = sid.split('|')[2].split('/', 2)
    legacy.append((sid, parts[1] if len(parts) >= 3 else '?', parts[2] if len(parts) >= 3 else ''))
print(f'legacy 会话: {len(legacy)}')

# 2. 权威库全部会话的消息指纹(一次拉全, 内存分组)
canon = defaultdict(list)
for sid, content in auth.execute("SELECT canonical_session_id, content FROM canonical_messages"):
    canon[sid].append(nkey(content))

# 3. 快照: 全部会话指纹 + 键索引
snap_msg = defaultdict(list)
for sid, content in snap.execute("SELECT session_id, content FROM messages"):
    snap_msg[sid].append(nkey(content))
snap_keys = defaultdict(list)
for (ssid, skey, agent, mc) in snap.execute("SELECT session_id, source_session_id, agent, message_count FROM sessions"):
    if skey: snap_keys[agent].append((ssid, skey))

def best_snapshot(fam, key):
    """返回与 key 指纹重合最大的快照会话 (ssid, 覆盖数, 快照条数)"""
    cands = []
    for ssid, skey in snap_keys.get(fam, []):
        if key and key in skey:
            cands.append(ssid)
    best = None
    legacy_set = None
    for ssid in cands:
        sset = set(snap_msg.get(ssid, []))
        if best is None or len(sset) > best[2]:
            best = (ssid, 0, len(sset))
    return best

matrix = []
stat = defaultdict(lambda: defaultdict(int))
recoverable_msgs = 0
for sid, fam, key in legacy:
    leg = canon.get(sid, [])
    lset = set(leg)
    twin_id = f'cs|{fam}|{key}'
    twin = canon.get(twin_id)
    snap_best = best_snapshot(fam, key)
    if twin is not None:
        tset = set(twin)
        extras = lset - tset
        if not extras:
            cls, detail = 'A', f'孪生完全覆盖({len(leg)}→{len(twin)})'
        else:
            cls, detail = 'B', f'孪生缺{len(extras)}条(legacy {len(leg)} vs 孪生 {len(twin)})'
            recoverable_msgs += len(extras)
    elif snap_best is not None and snap_msg.get(snap_best[0]):
        sset = set(snap_msg[snap_best[0]])
        extras = lset - sset
        gain = len(sset - lset)
        if gain > 0:
            cls, detail = 'C', f'快照可补{gain}条(legacy {len(leg)} vs 快照 {len(sset)}), legacy独有{len(extras)}'
            recoverable_msgs += gain
        else:
            cls, detail = 'A', f'快照确认无增量(legacy {len(leg)})'
    else:
        cls, detail = 'D', f'无孪生无快照(legacy {len(leg)} 条)'
    stat[fam][cls] += 1
    matrix.append({'session_id': sid, 'family': fam, 'key': key, 'class': cls,
                   'legacy_msgs': len(leg), 'detail': detail})

auth.close(); snap.close()

print('\n按家族分类矩阵:')
print(f"{'家族':<14}{'A孪生全':>8}{'B孪生缺':>8}{'C快照补':>8}{'D无源':>7}{'小计':>7}")
for fam in sorted(stat, key=lambda f: -sum(stat[f].values())):
    s = stat[fam]
    tot = sum(s.values())
    print(f"{fam:<14}{s.get('A',0):>8}{s.get('B',0):>8}{s.get('C',0):>8}{s.get('D',0):>7}{tot:>7}")
s = defaultdict(int)
for fam in stat:
    for c, n in stat[fam].items(): s[c] += n
print(f"\n总计: A={s.get('A',0)} B={s.get('B',0)} C={s.get('C',0)} D={s.get('D',0)}")
print(f"预计可找回消息(去重后): {recoverable_msgs:,} 条")

out = r'D:\ADLINK\数据分析\var\reports\legacy-reconcile-20260926.json'
json.dump({'summary': {f: dict(stat[f]) for f in stat}, 'recoverable_msgs': recoverable_msgs,
           'matrix': matrix}, open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('矩阵已写:', out)
