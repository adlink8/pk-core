"""999.5 单人评审台:gold 三键核对 + judge 人工校准的本地 Web 界面。

delivery 层只读私有评审素材、只写 private_evals 下的 labels 文件:
- 数据源:`var/runtime/private_evals/comprehensive_v1.private.jsonl`
  (split=human_review_candidate)与 `judge_calibration_packet_v1.private.json`,
  证据摘录从 canonical 对话库(mode=ro)取。
- 输出:`var/runtime/private_evals/review_labels_<ts>.json`(append-only,
  不覆盖历史批次)。不触碰任何 authority/SSOT/eval registry——labels 的
  下游转正(gold 入 suite、judge 一致率)由独立脚本/人工流程处理。

路由(api_server 接线):
- GET  /ui/review        → 评审页(HTML,数据服务端即时装配,localhost only)
- POST /ui/review/labels → 保存 labels JSON
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.core.project_paths import (
    AGENT_CONVERSATIONS_DB,
    VAR_RUNTIME,
)

PRIVATE_EVALS_DIR = VAR_RUNTIME / "private_evals"
CANDIDATE_SUITE = PRIVATE_EVALS_DIR / "comprehensive_v1.private.jsonl"
JUDGE_PACKET = PRIVATE_EVALS_DIR / "judge_calibration_packet_v1.private.json"

_EXCERPT_LIMIT = 300
_ANSWER_LIMIT = 600


def _evidence_excerpt(cur: sqlite3.Cursor, id_col: str, ref: str) -> dict:
    for probe in (ref, ref.split("|", 1)[-1]):
        row = cur.execute(
            f"SELECT role, content FROM canonical_messages WHERE {id_col}=?",
            (probe,),
        ).fetchone()
        if row:
            text = " ".join((row[1] or "").split())
            clipped = text[:_EXCERPT_LIMIT] + ("…" if len(text) > _EXCERPT_LIMIT else "")
            return {"ref": ref, "role": row[0], "text": clipped}
    return {"ref": ref, "role": "?", "text": "(未找到源消息 — 建议判「删」)"}


def _load_gold_candidates() -> list[dict]:
    if not CANDIDATE_SUITE.exists():
        return []
    cases = []
    with open(CANDIDATE_SUITE, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d.get("split") == "human_review_candidate":
                cases.append(d)
    if not cases:
        return []
    con = sqlite3.connect(
        f"file:{AGENT_CONVERSATIONS_DB.resolve().as_posix()}?mode=ro", uri=True
    )
    try:
        cur = con.cursor()
        cols = [r[1] for r in cur.execute("PRAGMA table_info(canonical_messages)")]
        id_col = "canonical_message_id" if "canonical_message_id" in cols else cols[0]
        return [
            {
                "id": c["id"],
                "query": c["query"],
                "scenario": c.get("scenario", ""),
                "abstain": bool(c.get("expected_abstain")),
                "conflict": bool(c.get("expected_conflict")),
                "refs": [
                    _evidence_excerpt(cur, id_col, r)
                    for r in (c.get("gold_evidence_refs") or [])[:4]
                ],
            }
            for c in cases
        ]
    finally:
        con.close()


def _load_judge_cases() -> list[dict]:
    if not JUDGE_PACKET.exists():
        return []
    packet = json.loads(JUDGE_PACKET.read_text(encoding="utf-8"))
    by_case: dict[str, list] = defaultdict(list)
    for r in packet.get("rows", []):
        by_case[r["case_id"]].append(r)
    cases = []
    for cid in sorted(by_case):
        grp = sorted(by_case[cid], key=lambda x: x["mode"])
        cases.append(
            {
                "id": cid,
                "query": " ".join(str(grp[0]["query"]).split())[:400],
                "abstain": bool(grp[0].get("expected_abstain")),
                "answers": [
                    {
                        "mode": r["mode"],
                        "text": " ".join(str(r["answer"]).split())[:_ANSWER_LIMIT],
                    }
                    for r in grp
                ],
            }
        )
    return cases


def save_review_labels(payload: dict) -> dict:
    """保存评审 labels 到 private_evals(append-only,文件名带 UTC 时间戳)。"""
    if not isinstance(payload, dict):
        raise ValueError("labels payload 必须是 JSON object")
    gold = payload.get("gold_labels") or {}
    judge = payload.get("judge_labels") or {}
    if not isinstance(gold, dict) or not isinstance(judge, dict):
        raise ValueError("gold_labels / judge_labels 必须是 object")
    allowed_gold = {"对", "错", "删"}
    for k, v in gold.items():
        if v not in allowed_gold:
            raise ValueError(f"非法 gold 判定 {v!r} (case {k})")
    for cid, modes in judge.items():
        if not isinstance(modes, dict):
            raise ValueError(f"judge_labels[{cid}] 必须是 object")
        for mode, score in modes.items():
            if score not in (0, 1, 2):
                raise ValueError(f"非法 judge 分数 {score!r} ({cid}/{mode})")
    PRIVATE_EVALS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = PRIVATE_EVALS_DIR / f"review_labels_{ts}.json"
    record = {
        "schema_version": 1,
        "saved_at": ts,
        "source": "ui_review",
        "gold_labels": gold,
        "judge_labels": judge,
    }
    out.write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    judged_full = sum(1 for m in judge.values() if len(m) >= 5)
    return {
        "saved": out.name,
        "gold_labeled": len(gold),
        "judge_cases_complete": judged_full,
    }


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>评审台 · gold 核对 + judge 打分</title>
<style>
:root{--bg:#14161a;--card:#1e2128;--fg:#e8e8e8;--dim:#9aa0aa;--acc:#4f8ef7;
--ok:#3fb96f;--bad:#e05c5c;--del:#8a8f98;--line:#2c3038}
@media(prefers-color-scheme:light){:root{--bg:#f5f6f8;--card:#fff;--fg:#1a1d23;
--dim:#667085;--line:#e4e7ec}}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--fg);font:15px/1.65 system-ui,"Microsoft YaHei",sans-serif;
padding:1rem;max-width:860px;margin:0 auto}
.tabs{display:flex;gap:.5rem;margin-bottom:1rem}
.tabs button{flex:1;padding:.6rem;border:1px solid var(--line);background:var(--card);
color:var(--fg);border-radius:8px;cursor:pointer;font-size:15px}
.tabs button.on{border-color:var(--acc);color:var(--acc);font-weight:600}
.bar{height:6px;background:var(--line);border-radius:3px;margin-bottom:1rem;overflow:hidden}
.bar i{display:block;height:100%;background:var(--acc);transition:width .2s}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:1.2rem;margin-bottom:1rem}
.q{font-size:17px;font-weight:600;margin-bottom:.6rem}
.meta{color:var(--dim);font-size:13px;margin-bottom:.8rem}
.flag{display:inline-block;background:#7c5c1e33;color:#e0b04f;border-radius:4px;
padding:0 .5rem;font-size:12px;margin-right:.4rem}
.ev{border-left:3px solid var(--line);padding:.4rem .8rem;margin:.5rem 0;
font-size:13.5px;color:var(--dim)}
.ev b{color:var(--fg)}
.keys{display:flex;gap:.6rem;margin-top:1rem;flex-wrap:wrap}
.keys button{flex:1;min-width:100px;padding:.7rem;border-radius:8px;border:1px solid var(--line);
background:transparent;color:var(--fg);cursor:pointer;font-size:15px}
.keys .k1:hover,.keys .k1.sel{background:var(--ok);border-color:var(--ok);color:#fff}
.keys .k2:hover,.keys .k2.sel{background:var(--bad);border-color:var(--bad);color:#fff}
.keys .k3:hover,.keys .k3.sel{background:var(--del);border-color:var(--del);color:#fff}
.ans{border:1px solid var(--line);border-radius:8px;padding:.6rem .8rem;margin:.5rem 0;
font-size:13.5px}
.ans.focus{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc)}
.ans .mode{color:var(--dim);font-size:12px}
.score{float:right;font-weight:700;color:var(--acc)}
.nav{display:flex;justify-content:space-between;color:var(--dim);font-size:13px;margin-top:.5rem}
.export{width:100%;padding:.8rem;margin-top:.5rem;border-radius:8px;border:none;
background:var(--acc);color:#fff;font-size:15px;cursor:pointer}
.hint{color:var(--dim);font-size:13px;text-align:center;margin:.6rem 0}
.done{color:var(--ok);font-weight:600;text-align:center;padding:1rem}
kbd{background:var(--line);border-radius:4px;padding:0 .4em;font-size:12px}
</style></head><body>
<div class="tabs">
  <button id="tG" class="on" onclick="tab('g')">① Gold 核对 <span id="pG"></span></button>
  <button id="tJ" onclick="tab('j')">② Judge 打分 <span id="pJ"></span></button>
</div>
<div class="bar"><i id="bar"></i></div>
<div id="view"></div>
<button class="export" onclick="doExport()">保存评审结果(判完或中途都可存)</button>
<div class="hint" id="msg">进度自动存在浏览器里,关掉重开会接着上次的位置;保存直接落
var/runtime/private_evals/</div>
<script>
const D=__DATA__;
let S=JSON.parse(localStorage.getItem('pk_review')||'{"g":{},"j":{},"tab":"g","gi":0,"ji":0,"ja":0}');
const save=()=>localStorage.setItem('pk_review',JSON.stringify(S));
function tab(t){S.tab=t;save();render()}
function nextUnG(){for(let i=0;i<D.gold.length;i++)if(!S.g[D.gold[i].id])return i;return -1}
function nextUnJ(){for(let i=0;i<D.judge.length;i++){const c=D.judge[i];
  if(!S.j[c.id]||Object.keys(S.j[c.id]).length<c.answers.length)return i}return -1}
function setG(v){const c=D.gold[S.gi];S.g[c.id]=v;
  const n=nextUnG();if(n>=0)S.gi=n;save();render()}
function setJ(v){const c=D.judge[S.ji];S.j[c.id]=S.j[c.id]||{};
  S.j[c.id][c.answers[S.ja].mode]=v;
  if(S.ja<c.answers.length-1){S.ja++}else{const n=nextUnJ();if(n>=0){S.ji=n;S.ja=0}}
  save();render()}
function mv(d){if(S.tab==='g'){S.gi=Math.min(Math.max(S.gi+d,0),D.gold.length-1)}
  else{S.ji=Math.min(Math.max(S.ji+d,0),D.judge.length-1);S.ja=0}save();render()}
document.addEventListener('keydown',e=>{
  if(e.key==='ArrowLeft')return mv(-1);
  if(e.key==='ArrowRight')return mv(1);
  if(S.tab==='g'&&['1','2','3'].includes(e.key))setG({'1':'对','2':'错','3':'删'}[e.key]);
  if(S.tab==='j'&&['0','1','2'].includes(e.key))setJ(+e.key);
  if(e.key==='Tab'&&S.tab==='j'){e.preventDefault();
    const c=D.judge[S.ji];S.ja=(S.ja+1)%c.answers.length;save();render()}
});
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')}
function render(){
  const gd=Object.keys(S.g).length,jd=D.judge.filter(c=>S.j[c.id]&&
    Object.keys(S.j[c.id]).length>=c.answers.length).length;
  document.getElementById('pG').textContent=`${gd}/${D.gold.length}`;
  document.getElementById('pJ').textContent=`${jd}/${D.judge.length}`;
  document.getElementById('tG').className=S.tab==='g'?'on':'';
  document.getElementById('tJ').className=S.tab==='j'?'on':'';
  const v=document.getElementById('view');
  if(S.tab==='g'){
    document.getElementById('bar').style.width=(D.gold.length?gd/D.gold.length*100:0)+'%';
    if(!D.gold.length){v.innerHTML='<div class="done">没有待核对的 gold 候选</div>';return}
    if(gd>=D.gold.length){v.innerHTML='<div class="done">✅ Gold 全部判完 — 点下方保存</div>';return}
    const c=D.gold[S.gi],lab=S.g[c.id];
    v.innerHTML=`<div class="card">
      <div class="meta">${S.gi+1} / ${D.gold.length} · ${esc(c.id)} · ${esc(c.scenario)}
      ${c.abstain?'<span class="flag">期望弃答</span>':''}
      ${c.conflict?'<span class="flag">期望冲突提示</span>':''}</div>
      <div class="q">${esc(c.query)}</div>
      ${c.refs.length?c.refs.map(r=>`<div class="ev"><b>[${esc(r.role)}]</b> ${esc(r.text)}</div>`).join('')
        :'<div class="ev">(无证据 — 弃答/无答案类:判断「问这种问题时正确行为是拒答」是否成立)</div>'}
      <div class="keys">
        <button class="k1 ${lab==='对'?'sel':''}" onclick="setG('对')"><kbd>1</kbd> 对 — 进 gold</button>
        <button class="k2 ${lab==='错'?'sel':''}" onclick="setG('错')"><kbd>2</kbd> 错 — 答案/证据不对</button>
        <button class="k3 ${lab==='删'?'sel':''}" onclick="setG('删')"><kbd>3</kbd> 删 — 模糊/无价值</button>
      </div>
      <div class="nav"><span>← → 前后翻</span><span>判定标准:照这问题去搜,命中这些证据算答对吗?</span></div>
    </div>`;
  }else{
    document.getElementById('bar').style.width=(D.judge.length?jd/D.judge.length*100:0)+'%';
    if(!D.judge.length){v.innerHTML='<div class="done">没有 judge 校准包</div>';return}
    if(jd>=D.judge.length){v.innerHTML='<div class="done">✅ Judge 全部打完 — 点下方保存</div>';return}
    const c=D.judge[S.ji],sc=S.j[c.id]||{};
    v.innerHTML=`<div class="card">
      <div class="meta">${S.ji+1} / ${D.judge.length} · ${esc(c.id)}
      ${c.abstain?'<span class="flag">期望弃答:正确拒答=2,硬编答案=0</span>':''}</div>
      <div class="q">${esc(c.query)}</div>
      ${c.answers.map((a,i)=>`<div class="ans ${i===S.ja?'focus':''}">
        <span class="mode">${esc(a.mode)}</span>
        <span class="score">${sc[a.mode]!==undefined?sc[a.mode]+'分':''}</span>
        <div>${esc(a.text)||'(空答案)'}</div></div>`).join('')}
      <div class="keys">
        <button class="k2" onclick="setJ(0)"><kbd>0</kbd> 错误/无关</button>
        <button class="k3" onclick="setJ(1)"><kbd>1</kbd> 部分正确</button>
        <button class="k1" onclick="setJ(2)"><kbd>2</kbd> 正确有用</button>
      </div>
      <div class="nav"><span>按键给蓝框答案打分,自动跳下一个 · <kbd>Tab</kbd> 换焦点</span><span>← → 换题</span></div>
    </div>`;
  }
}
async function doExport(){
  const out={gold_labels:S.g,judge_labels:S.j};
  const msg=document.getElementById('msg');
  try{
    const r=await fetch('/ui/review/labels',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(out)});
    const j=await r.json();
    if(r.ok){msg.textContent='✅ 已保存: '+(j.data?j.data.saved:JSON.stringify(j));return}
    throw new Error(JSON.stringify(j));
  }catch(e){
    const b=new Blob([JSON.stringify(out,null,2)],{type:'application/json'});
    const a=document.createElement('a');a.href=URL.createObjectURL(b);
    a.download='review_labels_'+new Date().toISOString().slice(0,10)+'.json';a.click();
    msg.textContent='服务端保存失败,已改为下载文件 — 请放入 private_evals/ ('+e.message+')';
  }
}
render();
</script></body></html>"""


def build_review_page() -> str:
    """装配评审页 HTML(数据服务端即时读取,私有内容仅经 localhost 传输)。"""
    data = json.dumps(
        {"gold": _load_gold_candidates(), "judge": _load_judge_cases()},
        ensure_ascii=False,
    ).replace("</", "<\\/")
    return _PAGE_TEMPLATE.replace("__DATA__", data)
