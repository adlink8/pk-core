"""整理 integration/analysis：只读分类 + 安全归档（移动到 _archive，不删除）。

规则:
- 脚本硬编码输出路径保留在原位
- 被替代的中间 canary / pilot / ops probe → ai_context/_archive/
- 根目录阶段一画像报告 → stage1_profile/
- 写入 MANIFEST 与 CLEANUP_LOG，便于回滚

用法::

    python tools/supported/reorganize_analysis_dir.py --dry-run
    python tools/supported/reorganize_analysis_dir.py --write
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ANALYSIS = ROOT / "integration" / "analysis"
AI = ANALYSIS / "ai_context"
ARCHIVE = AI / "_archive"
STAGE1 = ANALYSIS / "stage1_profile"
STAMP = datetime.now(timezone.utc).strftime("%Y%m%d")

# --- 保留在 ai_context 根（脚本/当前主产物）---
KEEP_AI_CONTEXT = {
    # conversation live
    "conversation_segments.json",
    "conversation_summaries.json",
    "conversation_summaries.md",
    "conversation_quality_report.json",
    "conversation_quality_report.md",
    "conversation_prompt_eval_set.json",
    "conversation_graph.html",
    "agent_conversation_cutover_report.json",
    "agentsview_import_inventory.json",
    "agentsview_import_inventory.md",
    # knowledge live / inventory / baseline
    "knowledge_unit_inventory.json",
    "knowledge_unit_inventory.md",
    "knowledge_unit_raw_baseline.json",
    "knowledge_unit_raw_baseline.md",
    "knowledge_incremental_delta.json",
    # latest canary / eval snapshots (current)
    "knowledge_unit_canary_wrapup.json",
    "knowledge_unit_canary_report_v2.json",
    "knowledge_unit_expanded_v2_frozen.json",
    "knowledge_unit_expanded_v2_hybrid.json",
    "knowledge_unit_merged_frozen.json",
    "knowledge_unit_merged_hybrid.json",
    # memory live referenced by audit
    "memory_candidate_extraction_report.json",
    "memory_candidate_extraction_report.md",
    "memory_evidence_bundles_preview.json",
    "memory_evidence_bundles_preview.md",
    "memory_promotion_report.json",
    "memory_promotion_report.md",
    "memory_promotion_candidates_preview.json",
    "memory_promotion_candidates_preview.md",
    "memory_experiment_comparison.json",
    "memory_experiment_comparison.md",
    "memory_experiment_inventory.json",
    "memory_experiment_inventory.md",
    "memory_gate_repair_report.json",
    "memory_gate_repair_report.md",
    "memory_lifecycle_preview.json",
    "memory_mechanism_matrix.json",
    "memory_mechanism_matrix.md",
    "memory_pipeline_target_design.md",
    "memory_decomplexity_plan.json",
    "memory_decomplexity_plan.md",
    "memory_depth_readiness.md",
    "memory_relation_candidate_proposals_report.json",
    "memory_relation_candidate_proposals_report.md",
    "memory_relation_eval_report.json",
    "memory_relation_eval_report.md",
    "mem0_candidate_memories.json",
    "mem0_candidate_evaluation.md",
    "deep_memory_mining.json",
    "deep_memory_mining.md",
    "deep_memory_insights.json",
    "deep_memory_insights.md",
    "deep_memory_profile.md",
    "deep_profile_evaluation.md",
    # graph
    "graph_relation_candidates_report.json",
    "graph_relation_candidate_proposals_report.json",
    "graph_relation_candidate_proposals_report.md",
    "graph_relation_judgments_report.json",
    "graph_relation_eval_report.json",
    "graph_relation_eval_report.md",
    # profiles used by API / pipeline
    "person_profile.md",
    "person_profile_v2.md",
    # vector / sqlite comparison (current)
    "vector_collection_contract.md",
    "vector_collection_health.json",
    "vector_collection_health.md",
    "vector_retrieval_eval_report.json",
    "vector_retrieval_eval_report.md",
    "vector_retrieval_eval_set.json",
    "vector_generation_comparison.json",
    "vector_generation_comparison.md",
    "sqlite_generation_comparison.json",
    "sqlite_generation_comparison.md",
    # phase14 current narrative
    "phase14_priority_execution_report.md",
    "phase14_wrapup_smoke.json",
    "phase14_wrapup_test_report.md",
    "phase14_expanded_final_report.json",
    # quality / coverage current
    "prompt_eval_results.json",
    "prompt_eval_results.md",
    "test_coverage_gaps.json",
    "test_coverage_gaps.md",
    # meta
    "README.md",
    "generation_gap_analysis.md",
    "generation_gap_analysis.json",
}

# empty string cleanup
KEEP_AI_CONTEXT = {x for x in KEEP_AI_CONTEXT if x}

# 归档映射：相对 ai_context → 子目录
ARCHIVE_MAP: dict[str, str] = {
    # superseded canary / candidate builds
    "knowledge_unit_canary_report.json": "knowledge/canary_superseded",
    "knowledge_unit_canary_merged.json": "knowledge/canary_superseded",
    "knowledge_unit_canary_expanded_v2.json": "knowledge/canary_superseded",
    "knowledge_unit_candidate_ab.json": "knowledge/candidate_early",
    "knowledge_unit_candidate_build.json": "knowledge/candidate_early",
    "knowledge_unit_candidate_hybrid.json": "knowledge/candidate_early",
    "knowledge_unit_expanded_frozen.json": "knowledge/eval_superseded",
    "knowledge_unit_expanded_hybrid.json": "knowledge/eval_superseded",
    "canary_labeling_worksheet.local.json": "knowledge/canary_superseded",
    # pilot
    "knowledge_unit_pilot_manifest.json": "knowledge/pilot",
    "knowledge_unit_pilot_preflight.json": "knowledge/pilot",
    "knowledge_unit_pilot_report.json": "knowledge/pilot",
    "knowledge_unit_pilot_review.md": "knowledge/pilot",
    # LLM dev samples
    "knowledge_unit_llm_samples.jsonl": "knowledge/dev_samples",
    "knowledge_unit_llm_test_results.json": "knowledge/dev_samples",
    # phase14 ops probes (superseded by wrapup/final)
    "phase14_missing_positions_recon.json": "phase14/ops_probes",
    "phase14_terminal243_analysis.json": "phase14/ops_probes",
    "phase14_terminal_sample_probe.json": "phase14/ops_probes",
    "phase14_final_reconcile.json": "phase14/ops_probes",
    "phase14_expanded_reconcile.json": "phase14/ops_probes",
}

# 根目录阶段一产物 → stage1_profile/
STAGE1_FILES = [
    "_schema.json",
    "capability_report.json",
    "capability_report.md",
    "classification_summary.json",
    "context_report.json",
    "context_report.md",
    "cross_module_insights.csv",
    "graph_report.json",
    "graph_report.md",
    "integrated_system_report.html",
    "memory_graph.html",
    "memory_graph_llm.html",
    "memory_report.json",
    "memory_report.md",
    "module_summary.csv",
    "preference_report.json",
    "preference_report.md",
    "profile.html",
    "profile.json",
    "profile.md",
    "profile_data_flow.csv",
    "profile_data_flow_dedup.csv",
    "profile_dedup.html",
    "profile_dedup.json",
    "profile_dedup.md",
    "profile_focus.csv",
    "profile_focus_dedup.csv",
    "profile_growth_chart.png",
    "profile_growth_chart_dedup.png",
    "profile_growth_monthly.csv",
    "profile_growth_monthly_dedup.csv",
    "profile_module_focus.csv",
    "profile_module_focus_dedup.csv",
    "profile_screenshot.png",
    "profile_thinking_mode.csv",
    "profile_thinking_mode_dedup.csv",
]


def move(src: Path, dst: Path, dry_run: bool, log: list[dict]) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "action": "move",
        "from": str(src.relative_to(ANALYSIS)).replace("\\", "/"),
        "to": str(dst.relative_to(ANALYSIS)).replace("\\", "/"),
        "bytes": src.stat().st_size,
    }
    log.append(entry)
    print(f"  MOVE {entry['from']} -> {entry['to']}")
    if not dry_run:
        if dst.exists():
            # avoid clobber: suffix
            dst = dst.with_name(dst.stem + f"__{STAMP}" + dst.suffix)
            entry["to"] = str(dst.relative_to(ANALYSIS)).replace("\\", "/")
        shutil.move(str(src), str(dst))


def classify_unmapped(dry_run: bool, log: list[dict]) -> list[str]:
    """ai_context 根下未 KEEP 且未 ARCHIVE_MAP 的文件 → _archive/unsorted。"""
    leftovers = []
    for p in sorted(AI.iterdir()):
        if p.is_dir():
            continue
        if p.name in KEEP_AI_CONTEXT:
            continue
        if p.name in ARCHIVE_MAP:
            continue
        if p.name.startswith("."):
            continue
        leftovers.append(p.name)
        dest = ARCHIVE / "unsorted" / p.name
        move(p, dest, dry_run, log)
    return leftovers


def run(dry_run: bool) -> int:
    log: list[dict] = []
    print(f"reorganize analysis  dry_run={dry_run}")
    print(f"ANALYSIS={ANALYSIS}")

    # 1) archive mapped ai_context intermediates
    print("\n[1] archive superseded ai_context intermediates...")
    for name, sub in ARCHIVE_MAP.items():
        src = AI / name
        if src.exists():
            move(src, ARCHIVE / sub / name, dry_run, log)

    # 2) already-named backup folder stays; ensure under _archive
    print("\n[2] nest _backup_wave8 under _archive if present...")
    wave8 = AI / "_backup_wave8"
    if wave8.exists() and wave8.is_dir():
        dest = ARCHIVE / "conversation" / "backup_wave8"
        if not dest.exists():
            log.append(
                {
                    "action": "move_dir",
                    "from": "ai_context/_backup_wave8",
                    "to": "ai_context/_archive/conversation/backup_wave8",
                }
            )
            print("  MOVE_DIR ai_context/_backup_wave8 -> _archive/conversation/backup_wave8")
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(wave8), str(dest))

    # 3) stage1 root reports
    print("\n[3] stage1_profile for root analysis reports...")
    for name in STAGE1_FILES:
        src = ANALYSIS / name
        if src.exists():
            move(src, STAGE1 / name, dry_run, log)

    # 4) leftovers in ai_context root
    print("\n[4] archive leftover ai_context root files...")
    leftovers = classify_unmapped(dry_run, log)

    # 5) write logs / README
    print("\n[5] write MANIFEST / CLEANUP_LOG / README...")
    cleanup = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dry_run": dry_run,
        "moves": log,
        "kept_ai_context_policy": sorted(KEEP_AI_CONTEXT),
        "leftovers_to_unsorted": leftovers,
        "note": "No files deleted. Restore by moving paths reverse of CLEANUP_LOG.",
    }

    if not dry_run:
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        (ARCHIVE / f"CLEANUP_LOG_{STAMP}.json").write_text(
            json.dumps(cleanup, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        write_readmes()
        # also root analysis README
        write_analysis_readme()
    else:
        print(json.dumps({"would_move": len(log), "leftovers": leftovers}, ensure_ascii=False, indent=2))

    print(f"\nDONE moves={len(log)} dry_run={dry_run}")
    return 0


def write_readmes() -> None:
    (ARCHIVE / "README.md").write_text(
        """# ai_context/_archive

历史/中间产物归档。**不参与**当前检索与 pipeline 默认输出。

| 子目录 | 内容 |
|---|---|
| knowledge/canary_superseded | 旧 canary 报告 |
| knowledge/candidate_early | 早期 candidate build |
| knowledge/eval_superseded | 被 v2/merged 替代的 frozen 评估 |
| knowledge/pilot | Pilot 阶段 manifest/report |
| knowledge/dev_samples | LLM 试抽样本 |
| phase14/ops_probes | 一次性 reconcile/terminal 探针 |
| conversation/backup_wave8 | Wave8 对话摘要备份 |
| unsorted | 整理时未映射但仍移出根目录的文件 |

回滚：见同目录 `CLEANUP_LOG_*.json`。
""",
        encoding="utf-8",
    )

    (AI / "README.md").write_text(
        """# ai_context — 当前有效分析产物索引

> 整理日期见 `_archive/CLEANUP_LOG_*.json`。中间产物已迁入 `_archive/`，**未删除**。

## 当前应看（Current）

| 主题 | 文件 |
|---|---|
| **代际对比·向量** | `vector_generation_comparison.md` + `charts/vector_gen_*.png` |
| **代际对比·SQLite** | `sqlite_generation_comparison.md` + `charts/sqlite_gen_*.png` |
| **合并缺口分析** | `generation_gap_analysis.md` |
| 向量健康/召回 | `vector_collection_health.md`, `vector_retrieval_eval_report.md` |
| 知识 inventory | `knowledge_unit_inventory.md` |
| 知识 raw baseline | `knowledge_unit_raw_baseline.md` |
| 最新 canary/eval | `knowledge_unit_canary_wrapup.json`, `*_merged_*.json`, `*_expanded_v2_*.json` |
| Phase14 叙事 | `phase14_priority_execution_report.md`, `phase14_wrapup_*.md/json` |
| 对话主产物 | `conversation_summaries.*`, `conversation_segments.json` |
| 画像注入 | `person_profile.md` / `person_profile_v2.md` |
| 测试缺口 | `test_coverage_gaps.md` |

## 目录

```
ai_context/
  charts/                 # 对比图表
  _archive/               # 历史中间产物（可回滚）
  *.md / *.json           # 当前有效报告
```

## 大体积文件（保留原因）

| 文件 | ~体积 | 原因 |
|---|---:|---|
| conversation_segments.json | 27 MB | 对话分段主数据，图/摘要上游 |
| memory_evidence_bundles_preview.json | 16 MB | 记忆证据包预览，实验对照 |
| memory_promotion_report.json | 7 MB | 晋升报告 |
| conversation_summaries.json/md | 4+3 MB | 会话摘要主产物 |

精简这些需改 pipeline 输出策略，**不在本整理范围删除**。
""",
        encoding="utf-8",
    )


def write_analysis_readme() -> None:
    (ANALYSIS / "README.md").write_text(
        """# integration/analysis

分析产物根目录。2026-07-12 起结构：

```
analysis/
  README.md                 # 本索引
  stage1_profile/           # 阶段一：跨模块画像/报表/CSV/HTML
  ai_context/               # 阶段二+：对话/记忆/知识/向量/Phase14
    charts/                 # 代际对比图
    _archive/               # 中间产物归档（未删除）
  refactoring/              # 重构验证笔记
```

## 去哪看什么

| 需求 | 路径 |
|---|---|
| 旧统合画像 HTML/CSV | `stage1_profile/profile.md` 等 |
| 向量库新旧对比 | `ai_context/vector_generation_comparison.md` |
| SQLite 分层对比 | `ai_context/sqlite_generation_comparison.md` |
| **合并报告 + 缺口** | `ai_context/generation_gap_analysis.md` |
| 历史 canary/pilot | `ai_context/_archive/` |

## 说明

- **无硬删除**：中间文件移动到 `_archive/` 或 `stage1_profile/`。
- 部分脚本仍写 `ai_context/<固定文件名>`；整理时保留脚本依赖路径。
- 文档中旧路径 `analysis/profile.md` 现为 `analysis/stage1_profile/profile.md`。
""",
        encoding="utf-8",
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--write", action="store_true")
    args = p.parse_args()
    if not args.write and not args.dry_run:
        args.dry_run = True
    if args.write and args.dry_run:
        print("use only one of --dry-run / --write", flush=True)
        return 2
    return run(dry_run=not args.write)


if __name__ == "__main__":
    raise SystemExit(main())
