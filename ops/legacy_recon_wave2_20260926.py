# -*- coding: utf-8 -*-
"""第二波 legacy 补收·对账 (2026-09-26)
对 535 个无源 D 类会话, 用各家族适配器解析客户端现存文件, 按内容指纹重叠匹配,
输出找回清单 + 补料包(每会话缺失消息)。只读客户端与权威库, 不写权威库。"""
import sys, json, hashlib, time, glob, os
sys.path.insert(0, r'D:\ADLINK\数据分析\src')
from pathlib import Path
from collections import defaultdict
from personal_knowledge.application.conversation.live_sync import _capture_and_adapt
from personal_knowledge.core.conversation_events import EventKind

AUTH = r'D:\ADLINK\数据分析\data\canonical\agent\structured\db\agent_conversations.sqlite'
STORE = Path(r'D:\ADLINK\数据分析\tmp\wave2-store')
MSG_KINDS = {EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE,
             EventKind.DEVELOPER_MESSAGE, EventKind.SYSTEM_MESSAGE}
ROLE_MAP = {EventKind.USER_MESSAGE: 'user', EventKind.ASSISTANT_MESSAGE: 'assistant',
            EventKind.DEVELOPER_MESSAGE: 'system', EventKind.SYSTEM_MESSAGE: 'system'}

def nkey(text):
    if not text:
        return ''
    return hashlib.md5(''.join(text.split()).encode('utf-8', 'replace')).hexdigest()

# ---- 1. 权威库: D 类 legacy 会话 + 其指纹 ----
import sqlite3
auth = sqlite3.connect(f'file:{AUTH}?mode=ro', uri=True, timeout=90)
plan = json.load(open(r'D:\ADLINK\数据分析\var\reports\legacy-merge-wave1-plan.json', encoding='utf-8'))
done = {x['legacy'] for x in plan['A']} | {x['legacy'] for x in plan['B']} | {x['legacy'] for x in plan['C']}
canon = defaultdict(set)
for sid, content in auth.execute("SELECT canonical_session_id, content FROM canonical_messages"):
    canon[sid].add(nkey(content))
targets = []
for (sid,) in auth.execute("SELECT canonical_session_id FROM canonical_sessions WHERE agent='legacy'"):
    if sid in done or sid not in canon or not canon[sid]:
        continue
    parts = sid.split('|')[2].split('/', 2)
    if len(parts) >= 3:
        targets.append((sid, parts[1], parts[2], canon[sid]))
by_fam = defaultdict(list)
for t in targets:
    by_fam[t[1]].append(t)
print('D 类目标:', {f: len(v) for f, v in by_fam.items()})

FAMS = {
    'workbuddy':   (r'D:\C_Links\.workbuddy\projects', r'**\*', 'workbuddy'),
    'grok':        (r'C:\Users\li\.grok\sessions', r'*\*\chat_history.jsonl', 'grok'),
    'antigravity': (r'C:\Users\li\.gemini\antigravity\conversations', r'*.db', 'antigravity'),
    'opencode':    (r'C:\Users\li\.local\share\opencode', r'opencode.db', 'opencode'),
    'qoder':       (r'C:\Users\li\.qoder', r'**\*.jsonl', 'qoder'),
    'codex':       (r'C:\Users\li\.codex\sessions', r'**\*.jsonl', 'codex'),
    'kimi':        (r'C:\Users\li\.kimi-code', r'**\*.jsonl', 'kimi'),
}
if '--only' in sys.argv:
    only = set(sys.argv[sys.argv.index('--only') + 1].split(','))
    FAMS = {k: v for k, v in FAMS.items() if k in only}

report = {}
bundle = {}
t0 = time.time()
for fam, (root, pattern, adapt_fam) in FAMS.items():
    tgts = by_fam.get(fam, [])
    if not tgts:
        continue
    files = [p for p in glob.glob(os.path.join(root, pattern), recursive=True) if os.path.isfile(p)]
    print(f'\n== {fam} == 目标 {len(tgts)}, 客户端文件 {len(files)}')
    best = {}   # legacy_sid -> (overlap, gain_set, src, session_id, msgs)
    parsed = failed = 0
    for fp in files:
        rel = os.path.relpath(fp, root).replace('\\', '/')
        try:
            artifact, result = _capture_and_adapt(
                Path(fp), family=adapt_fam, mirror_path=f'wave2/{fam}/{rel}',
                artifact_store=STORE, byte_limit=600_000_000, count_limit=100_000)
        except Exception as e:
            failed += 1
            continue
        parsed += 1
        # 按事件 session 分组
        groups = defaultdict(list)
        for ev in result.events:
            if ev.kind not in MSG_KINDS:
                continue
            text = ev.content if ev.content is not None else ev.summary
            groups[ev.session_id].append((ev.occurred_at, ROLE_MAP.get(ev.kind, 'system'), text))
        for ssid, msgs in groups.items():
            ks = {nkey(m[2]) for m in msgs}
            for sid, _, key, lset in tgts:
                ov = len(lset & ks)
                if ov < 2:
                    continue
                gain = ks - lset
                cur = best.get(sid)
                if cur is None or ov > cur[0]:
                    keep = [m for m in msgs if nkey(m[2]) in gain]
                    best[sid] = (ov, len(gain), fp, ssid, keep)
        if parsed % 25 == 0:
            print(f'   已解析 {parsed}/{len(files)} 失败 {failed} 命中 {len(best)} ({time.time()-t0:.0f}s)')
    matched = {sid: v for sid, v in best.items() if v[1] > 0}
    total_gain = sum(v[1] for v in matched.values())
    print(f'   解析 {parsed} 失败 {failed}; 匹配 {len(matched)}/{len(tgts)}; 可补 {total_gain:,} 条')
    report[fam] = {'files': len(files), 'parsed': parsed, 'failed': failed,
                   'matched': len(matched), 'targets': len(tgts), 'gain': total_gain}
    bundle[fam] = [{'legacy': sid, 'src_file': v[2], 'native_session': v[3],
                    'overlap': v[0], 'gain': v[1],
                    'messages': [{'ts': m[0], 'role': m[1], 'text': m[2]} for m in v[4]]}
                   for sid, v in matched.items()]

auth.close()
# 合并保留其它家族的既有补料包结果
old_bundle_path = r'D:\ADLINK\数据分析\var\reports\legacy-wave2-bundle.json'
old_report_path = r'D:\ADLINK\数据分析\var\reports\legacy-wave2-recon.json'
try:
    old_bundle = json.load(open(old_bundle_path, encoding='utf-8'))
    old_report = json.load(open(old_report_path, encoding='utf-8'))
except Exception:
    old_bundle, old_report = {}, {}
merged_bundle = dict(old_bundle)
merged_bundle.update(bundle)
merged_report = dict(old_report)
merged_report.update(report)
json.dump(merged_report, open(old_report_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
json.dump(merged_bundle, open(old_bundle_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print(f'\n本轮找回: {sum(r["gain"] for r in report.values()):,} 条; 匹配会话 {sum(r["matched"] for r in report.values())}')
print(f'累计补料包: {sum(len(v) for v in merged_bundle.values())} 会话')
print('报告与补料包已写入 var/reports/.')
