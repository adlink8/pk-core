"""合并 vector + sqlite 代际对比报告，输出缺口分析。

读取:
  ai_context/vector_generation_comparison.json
  ai_context/sqlite_generation_comparison.json

写出:
  ai_context/generation_gap_analysis.md
  ai_context/generation_gap_analysis.json
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AI = ROOT / "integration" / "analysis" / "ai_context"
VEC = AI / "vector_generation_comparison.json"
SQL = AI / "sqlite_generation_comparison.json"
OUT_JSON = AI / "generation_gap_analysis.json"
OUT_MD = AI / "generation_gap_analysis.md"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    if not VEC.exists() or not SQL.exists():
        print("missing comparison json", VEC.exists(), SQL.exists())
        return 1
    v = load(VEC)
    s = load(SQL)

    vr = v["retrieval"]
    vs = v["structure"]
    se = s["layers"]["events"]
    sm = s["layers"]["memory"]
    sk = s["layers"]["knowledge"]
    sc = s["scorecard"]

    # ---- gaps ----
    gaps = []

    # G1 evidence coverage
    ev_cov = (sk.get("evidence") or {}).get("coverage")
    if ev_cov is not None and ev_cov < 0.85:
        gaps.append(
            {
                "id": "G1",
                "priority": "P1",
                "area": "证据链覆盖",
                "status": "open",
                "metric": "knowledge_unit_evidence coverage on draft units",
                "current": ev_cov,
                "target": 0.85,
                "delta": round(0.85 - ev_cov, 3),
                "why": "约一半 draft unit 无独立 evidence 行，削弱可审计性与 career-os 引用",
                "action": "回填 knowledge_unit_evidence：从 draft.source_message_ref / inventory 关联补齐；gate 强制 evidence≥1",
            }
        )

    # G2 frozen recall hybrid not at 0.85
    hy_r = vr["hybrid"]["suites"]["frozen"].get("recall_at_5")
    if hy_r is not None and hy_r < 0.85:
        gaps.append(
            {
                "id": "G2",
                "priority": "P0",
                "area": "混合检索召回",
                "status": "open",
                "metric": "hybrid Recall@5 (frozen)",
                "current": hy_r,
                "target": 0.85,
                "delta": round(0.85 - hy_r, 3),
                "why": "生产 knowledge-first+raw 路径仍低于质量门；字面/代码题仍依赖 raw",
                "action": "扩 frozen 题集分层(profile vs code)；调 KU slots；对 code 类 query 提高 raw 权重；补 hard negatives",
            }
        )

    # G3 pure KU recall
    ku_r = vr["ku"]["suites"]["frozen"].get("recall_at_5")
    if ku_r is not None and ku_r < 0.75:
        gaps.append(
            {
                "id": "G3",
                "priority": "P1",
                "area": "纯知识索引召回",
                "status": "open",
                "metric": "KU-only Recall@5 (frozen)",
                "current": ku_r,
                "target": 0.75,
                "delta": round(0.75 - ku_r, 3),
                "why": "压缩丢失字面匹配；部分 gold 仍只在 raw 可命中",
                "action": "索引文本加入 subject+keywords；可选 dual-embed(question / answer)；merge 后保留 alias",
            }
        )

    # G4 distance tie / overall distance not better
    e_dist = vr["events"]["suites"]["all"]["top1_distance_mean"]
    k_dist = vr["ku"]["suites"]["all"]["top1_distance_mean"]
    if k_dist > e_dist:
        gaps.append(
            {
                "id": "G4",
                "priority": "P2",
                "area": "全量语义距离",
                "status": "open",
                "metric": "top1 distance mean (all queries)",
                "current": {"events": e_dist, "ku": k_dist},
                "target": "ku <= events on knowledge-like subset; hybrid wins overall",
                "why": "全量距离 KU 不占优（query 集含代码/长模板）；profile 子集已显著更好",
                "action": "评估分场景报告；不要用全量距离作为唯一 KPI",
            }
        )

    # G5 merge collapse low
    collapse = (sk.get("dedup") or {}).get("collapse_pct") or 0
    multi = sk.get("multi_member_share") or 0
    if collapse < 0.05 and multi < 0.05:
        gaps.append(
            {
                "id": "G5",
                "priority": "P2",
                "area": "canonical 合并",
                "status": "open",
                "metric": "draft→canonical collapse / multi-member share",
                "current": {"collapse_pct": collapse, "multi_member_share": multi},
                "target": "review true near-duplicates; multi_member_share 视数据而定",
                "why": f"折叠仅 {collapse:.1%}，可能仍有近义重复未合并（如语言偏好中/交互双条）",
                "action": "用 merge_positive/hard_negative 集重跑 merger；对 subject 近邻聚类",
            }
        )

    # G6 extraction residual failures
    rate = sk.get("extraction_success_rate")
    st = sk.get("run_item_status") or {}
    term = st.get("terminal_failed", 0)
    if rate is not None and (rate < 0.9 or term > 0):
        gaps.append(
            {
                "id": "G6",
                "priority": "P1",
                "area": "抽取完成度",
                "status": "open",
                "metric": "knowledge_run_items success / terminal",
                "current": {"success_rate": rate, "status": st},
                "target": {"success_rate": 0.9, "terminal_failed": 0},
                "why": "仍有 terminal_failed / abstained，影响 inventory 完整覆盖",
                "action": "quota 恢复后 resume terminal/retryable；分类 schema_invalid vs api_error",
            }
        )

    # G7 events null title/time
    nulls = se.get("null_rates") or {}
    if nulls.get("title", 0) > 0.1 or nulls.get("event_time", 0) > 0.05:
        gaps.append(
            {
                "id": "G7",
                "priority": "P2",
                "area": "事件层数据质量",
                "status": "open",
                "metric": "unified_events null rates",
                "current": nulls,
                "target": {"title": "<10%", "event_time": "<5%"},
                "why": "title/event_time 空值影响 raw fallback 展示与时间过滤",
                "action": "enrich 管道补 title 回填；无时间事件标 unknown bucket",
            }
        )

    # G8 memory layer scale
    mem_n = sm["tables"].get("memory_items", {}).get("rows", 0)
    ku_n = sk["tables"].get("canonical_knowledge_units", {}).get("rows", 0)
    if mem_n and ku_n and mem_n < ku_n * 0.05:
        gaps.append(
            {
                "id": "G8",
                "priority": "P2",
                "area": "memory_items 与 KU 关系",
                "status": "open",
                "metric": "memory_items vs canonical KU",
                "current": {"memory_items": mem_n, "canonical_ku": ku_n},
                "target": "明确：memory 保留实验/图谱；profile 以 KU 为准，避免双写",
                "why": "memory 仅 291 条 vs KU 3 万，若仍双轨消费易造成画像不一致",
                "action": "文档声明 SSOT=canonical KU；memory 降级为 graph 实验或从 KU 投影",
            }
        )

    # G9 chroma leftover collections (from vector report inventory)
    all_cols = v.get("all_collections") or []
    ku_cols = [c for c in all_cols if "knowledge" in (c.get("name") or "") or (c.get("name") or "").startswith("ku_")]
    if len(ku_cols) > 3:
        gaps.append(
            {
                "id": "G9",
                "priority": "P3",
                "area": "Chroma 历史 collection 堆积",
                "status": "deferred",
                "metric": "knowledge_* / ku_* collection count",
                "current": len(ku_cols),
                "target": "active + 1 canary + optional previous",
                "why": "用户要求暂不清理以便对照；长期增加误用风险与磁盘",
                "action": "对照完成后仅保留 active+previous+canary；脚本化 list/delete candidates",
            }
        )

    # G10 analysis dir hygiene (post cleanup)
    gaps.append(
        {
            "id": "G10",
            "priority": "P3",
            "area": "分析产物目录卫生",
            "status": "mitigated",
            "metric": "analysis layout",
            "current": "stage1_profile/ + ai_context/ + _archive/",
            "target": "保持分类；大体积 JSON 可外置或压缩备份",
            "why": "原 163 文件混杂；已归档中间产物，大文件仍保留",
            "action": "conversation_segments / memory_*_preview 评估是否改为按需生成",
        }
    )

    # wins summary
    wins = [
        {
            "area": "检索召回",
            "evidence": f"frozen R@5 events {vr['events']['suites']['frozen']['recall_at_5']} → ku {ku_r} → hybrid {hy_r}",
        },
        {
            "area": "画像向可答性",
            "evidence": (
                f"profile top1 可答性 {vr['events']['suites']['profile']['answerability_mean']} → "
                f"{vr['ku']['suites']['profile']['answerability_mean']}；距离 "
                f"{vr['events']['suites']['profile']['top1_distance_mean']} → "
                f"{vr['ku']['suites']['profile']['top1_distance_mean']}"
            ),
        },
        {
            "area": "SQLite 治理",
            "evidence": (
                f"governance_flags events {sc['events']['governance_flags']} → "
                f"memory {sc['memory']['governance_flags']} → knowledge {sc['knowledge']['governance_flags']}"
            ),
        },
        {
            "area": "文本压缩",
            "evidence": (
                f"向量文档中位 {vs['events']['doc_len']['median']}→{vs['ku']['doc_len']['median']}；"
                f"SQLite 可答性 {se['text_quality']['answerability_mean']}→{sk['text_quality']['answerability_mean']}"
            ),
        },
        {
            "area": "知识规模",
            "evidence": (
                f"canonical KU {ku_n:,} vs memory {mem_n:,} vs events "
                f"{se['tables']['unified_events']['rows']:,}"
            ),
        },
    ]

    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {
            "vector": str(VEC.name),
            "sqlite": str(SQL.name),
            "vector_generated_at": v.get("generated_at"),
            "sqlite_generated_at": s.get("generated_at"),
        },
        "active_collection": v.get("active_collection"),
        "headline_metrics": {
            "vector": {
                "personal_events": vs["events"]["total"],
                "conversation_turns": vs["turns"]["total"],
                "knowledge_units_active": vs["ku"]["total"],
                "frozen_recall": {
                    "events": vr["events"]["suites"]["frozen"]["recall_at_5"],
                    "turns": vr["turns"]["suites"]["frozen"]["recall_at_5"],
                    "ku": ku_r,
                    "hybrid": hy_r,
                },
                "frozen_mrr": {
                    "events": vr["events"]["suites"]["frozen"]["mrr_at_5"],
                    "ku": vr["ku"]["suites"]["frozen"]["mrr_at_5"],
                    "hybrid": vr["hybrid"]["suites"]["frozen"]["mrr_at_5"],
                },
                "profile_top1_distance": {
                    "events": vr["events"]["suites"]["profile"]["top1_distance_mean"],
                    "ku": vr["ku"]["suites"]["profile"]["top1_distance_mean"],
                },
                "profile_answerability": {
                    "events": vr["events"]["suites"]["profile"]["answerability_mean"],
                    "ku": vr["ku"]["suites"]["profile"]["answerability_mean"],
                },
            },
            "sqlite": {
                "unified_events": se["tables"]["unified_events"]["rows"],
                "memory_items": mem_n,
                "draft_ku": sk["tables"].get("knowledge_units", {}).get("rows"),
                "canonical_ku": ku_n,
                "evidence_coverage": ev_cov,
                "extraction_success_rate": rate,
                "confidence_mean": (sk.get("confidence") or {}).get("mean"),
                "scorecard": sc,
            },
        },
        "wins": wins,
        "gaps": gaps,
        "priority_order": [g["id"] for g in sorted(gaps, key=lambda x: x["priority"])],
    }

    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # markdown
    def gap_table() -> str:
        lines = [
            "| ID | 优先级 | 状态 | 领域 | 当前 | 目标 | 动作 |",
            "|---|---|---|---|---|---|---|",
        ]
        for g in sorted(gaps, key=lambda x: (x["priority"], x["id"])):
            cur = g["current"]
            if isinstance(cur, dict):
                cur_s = json.dumps(cur, ensure_ascii=False)[:80]
            else:
                cur_s = str(cur)
            tgt = g["target"]
            if isinstance(tgt, dict):
                tgt_s = json.dumps(tgt, ensure_ascii=False)[:60]
            else:
                tgt_s = str(tgt)[:60]
            lines.append(
                f"| {g['id']} | {g['priority']} | {g['status']} | {g['area']} | `{cur_s}` | `{tgt_s}` | {g['action'][:60]} |"
            )
        return "\n".join(lines)

    wins_md = "\n".join(f"- **{w['area']}**: {w['evidence']}" for w in wins)
    gaps_detail = ""
    for g in sorted(gaps, key=lambda x: (x["priority"], x["id"])):
        gaps_detail += f"""
### {g['id']} · {g['area']} ({g['priority']}, {g['status']})

- **指标**: {g['metric']}
- **当前**: `{json.dumps(g['current'], ensure_ascii=False) if not isinstance(g['current'], str) else g['current']}`
- **目标**: `{json.dumps(g['target'], ensure_ascii=False) if not isinstance(g['target'], str) else g['target']}`
- **原因**: {g['why']}
- **动作**: {g['action']}
"""

    md = f"""# 向量 + SQLite 代际对比 · 合并报告与缺口分析

- 生成: `{report['generated_at']}`
- 源: `{VEC.name}` ({v.get('generated_at')}) + `{SQL.name}` ({s.get('generated_at')})
- Active 知识索引: `{v.get('active_collection')}`
- 分析目录整理: 见 `../README.md` 与 `ai_context/_archive/`

## 1. 一页结论

| 问题 | 答案 |
|---|---|
| 新知识层是否全面优于旧事件层？ | **在可答性、治理、画像类检索上显著优于**；全量字面/代码距离仍可能打平 |
| 生产该用谁？ | **hybrid（knowledge-first + raw fallback）**，不是删掉 events |
| SQLite 真相源？ | **canonical_knowledge_units** 作为个人知识 SSOT；events 作时间线；memory_items 实验层 |
| 最大缺口？ | hybrid R@5 未达 0.85；evidence 覆盖仅 ~51%；抽取仍有 residual terminal |

## 2. 关键指标对照

### 2.1 向量层

| 指标 | personal_events | conversation_turns | knowledge_units | hybrid |
|---|---:|---:|---:|---:|
| 条数 | {vs['events']['total']:,} | {vs['turns']['total']:,} | {vs['ku']['total']:,} | — |
| 文档长度中位 | {vs['events']['doc_len']['median']} | {vs['turns']['doc_len']['median']} | {vs['ku']['doc_len']['median']} | — |
| 结构可答性 | {vs['events']['answerability_mean']} | {vs['turns']['answerability_mean']} | {vs['ku']['answerability_mean']} | — |
| frozen R@5 | {vr['events']['suites']['frozen']['recall_at_5']} | {vr['turns']['suites']['frozen']['recall_at_5']} | {ku_r} | **{hy_r}** |
| frozen MRR@5 | {vr['events']['suites']['frozen']['mrr_at_5']} | {vr['turns']['suites']['frozen']['mrr_at_5']} | {vr['ku']['suites']['frozen']['mrr_at_5']} | {vr['hybrid']['suites']['frozen']['mrr_at_5']} |
| profile 距离↓ | {vr['events']['suites']['profile']['top1_distance_mean']} | {vr['turns']['suites']['profile']['top1_distance_mean']} | **{vr['ku']['suites']['profile']['top1_distance_mean']}** | 同 KU |
| profile 可答性↑ | {vr['events']['suites']['profile']['answerability_mean']} | {vr['turns']['suites']['profile']['answerability_mean']} | **{vr['ku']['suites']['profile']['answerability_mean']}** | 同 KU |

### 2.2 SQLite 层

| 指标 | L1 events | L2 memory | L3 knowledge |
|---|---:|---:|---:|
| 核心行数 | {se['tables']['unified_events']['rows']:,} | {mem_n:,} | draft {sk['tables'].get('knowledge_units',{}).get('rows'):,} / canon {ku_n:,} |
| 可答性(文本) | {se['text_quality']['answerability_mean']} | {sm.get('text_quality',{}).get('answerability_mean',0)} | {sk['text_quality']['answerability_mean']} |
| 噪声占比 | {se['text_quality']['noise_share']} | {sm.get('text_quality',{}).get('noise_share',0)} | {sk['text_quality']['noise_share']} |
| confidence 覆盖 | 0 | {sm.get('confidence',{}).get('coverage')} | {sk.get('confidence',{}).get('coverage')} (mean={sk.get('confidence',{}).get('mean')}) |
| 治理齐全度 | {sc['events']['governance_flags']} | {sc['memory']['governance_flags']} | {sc['knowledge']['governance_flags']} |
| evidence 覆盖 | — | links_per_item={sm.get('links_per_item')} | **{ev_cov}** |
| 抽取成功率 | — | — | {rate} |

## 3. 已证实的提升（Wins）

{wins_md}

图表索引:

- 向量: `charts/vector_gen_01` … `vector_gen_09`
- SQLite: `charts/sqlite_gen_01` … `sqlite_gen_10`
- 分报告: `vector_generation_comparison.md` · `sqlite_generation_comparison.md`

## 4. 缺口总表

{gap_table()}

## 5. 缺口详情

{gaps_detail}

## 6. 建议执行顺序

1. **P0 G2** — 把 hybrid frozen R@5 从 {hy_r} 推到 ≥0.85（分场景调权 + 题集）
2. **P1 G1** — evidence 覆盖 {ev_cov} → ≥0.85
3. **P1 G3/G6** — 纯 KU 召回与抽取 residual
4. **P2 G5/G7/G8** — 合并质量、事件空值、memory/KU SSOT 澄清
5. **P3 G9/G10** — 对照完成后清理 Chroma 候选；大 JSON 按需化

## 7. 分析目录整理摘要

| 动作 | 说明 |
|---|---|
| 未删除任何文件 | 仅移动 |
| `stage1_profile/` | 原 analysis 根下 profile/capability/memory_report 等 |
| `ai_context/_archive/` | 旧 canary/pilot/ops probe/wave8 备份 |
| 当前主读 | 本文件 + 两份 `*_generation_comparison.md` + `charts/` |

回滚日志: `ai_context/_archive/CLEANUP_LOG_*.json`

## 8. 复跑

```powershell
python -m personal_knowledge.evaluation.vector.compare_vector_generations
python -m personal_knowledge.evaluation.vector.compare_sqlite_generations
python tools/supported/build_generation_gap_analysis.py
```
"""
    OUT_MD.write_text(md, encoding="utf-8")
    print("wrote", OUT_MD)
    print("wrote", OUT_JSON)
    print("gaps", len(gaps), "wins", len(wins))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
