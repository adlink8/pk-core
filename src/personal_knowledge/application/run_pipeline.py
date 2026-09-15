"""数据管道统一入口。

把所有 build_* 步骤按依赖顺序串联，避免手动依次运行出错。
每步幂等，失败时打印错误并停止（不跳过，防止下游跑污染数据）。

管道步骤（固定顺序):
  1  build_integrated_system    重建统合 SQLite（9张原始表）
  2  enrich_unified_events      语义增强（补文本/修分类/建跨模块链接）必须紧跟步骤1
  3  build_merge_layer          去重折叠（叠加表，不破坏原数据）
  4  build_deep_profiles        module_profile + profile
  5  build_memory_store         记忆层基础表（tooling 工具偏好）
  6  build_capability_memory    能力记忆抽取
  7  build_context_memory       上下文记忆抽取（fact/project/habit）
  8  build_preference_memory    偏好记忆抽取（Google 关注偏好）
  9  build_memory_graph         记忆关系图谱构建
  10 build_vector_store         向量化到 ChromaDB（约 53s，支持 --resume）
  11 build_context_doc          生成 AI 长期上下文文档
  12 build_profile_from_memory  生成记忆图谱版 AI 上下文文档
  13 build_conversation_vector_store  [Phase 07 Wave 7] turn 叙述回流向量库（独立 collection）

注意：步骤 13 依赖 conversation_summaries.json（由 build_conversation_summary.py 生成，
需 LLM 配置），且需 chroma 服务在线。缺失时步骤 13 会报错但不影响前 12 步。
建议手动单独运行：python build_conversation_summary.py --write 后再跑步骤 13。

.. deprecated::
    Product day-to-day sync is ``pk-sync conversations [--write]``.
    This module retains steps 1–12 for forensics only and requires
    ``--legacy-integrated`` (and CLI allow-flag when invoked as rag-pipeline).

用法（仅 legacy / 取证）:
  set PK_ALLOW_LEGACY_PIPELINE=1
  python -m personal_knowledge.application.run_pipeline --legacy-integrated --dry-run
  python -m personal_knowledge.application.run_pipeline --legacy-integrated --from 2

产品对话增量:
  pk-sync conversations --write
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

# ===== 管道步骤定义 =====

ROOT = Path(__file__).resolve().parents[3]

STEP_MODULES = {
    "build_integrated_system": "personal_knowledge.application.build_integrated_system",
    "enrich_unified_events": "personal_knowledge.application.enrich_unified_events",
    "build_merge_layer": "personal_knowledge.application.graph.build_merge_layer",
    "build_deep_profiles": "personal_knowledge.application.build_deep_profiles",
    "build_memory_store": "personal_knowledge.application.memory.build_memory_store",
    "build_capability_memory": "personal_knowledge.application.memory.build_capability_memory",
    "build_context_memory": "personal_knowledge.application.memory.build_context_memory",
    "build_preference_memory": "personal_knowledge.application.memory.build_preference_memory",
    "build_memory_graph": "personal_knowledge.application.memory.build_memory_graph",
    "build_vector_store": "personal_knowledge.retrieval.build_vector_store",
    "build_context_doc": "personal_knowledge.application.build_context_doc",
    "build_profile_from_memory": "personal_knowledge.application.memory.build_profile_from_memory",
    "build_conversation_vector_store": "personal_knowledge.application.conversation.build_conversation_vector_store",
}

STEPS: list[dict] = [
    {
        "num": 1,
        "name": "build_integrated_system",
        "desc": "重建统合 SQLite（会删除并重建整个库文件）",
        "extra_args": [],
    },
    {
        "num": 2,
        "name": "enrich_unified_events",
        "desc": "语义增强：补真实文本 / 修复分类污染 / 建跨模块链接",
        "extra_args": [],
    },
    {
        "num": 3,
        "name": "build_merge_layer",
        "desc": "去重折叠：L1 真重复 / L2 同主题 / L3 保留（叠加表）",
        "extra_args": [],
    },
    {
        "num": 4,
        "name": "build_deep_profiles",
        "desc": "生成module_profile + profile（含 --use-merged 去重视图）",
        "extra_args": ["--use-merged"],
    },
    {
        "num": 5,
        "name": "build_memory_store",
        "desc": "记忆层：tooling 工具偏好记忆（Phase 04 Wave 3）",
        "extra_args": [],
    },
    {
        "num": 6,
        "name": "build_capability_memory",
        "desc": "记忆层：capability 能力使用记忆",
        "extra_args": [],
    },
    {
        "num": 7,
        "name": "build_context_memory",
        "desc": "记忆层：fact / project / habit 上下文记忆",
        "extra_args": [],
    },
    {
        "num": 8,
        "name": "build_preference_memory",
        "desc": "记忆层：preference 关注偏好记忆（Google 信号）",
        "extra_args": [],
    },
    {
        "num": 9,
        "name": "build_memory_graph",
        "desc": "记忆图谱：节点 + 5种跨类关系边",
        "extra_args": [],
    },
    {
        "num": 10,
        "name": "build_vector_store",
        "desc": "向量化到 ChromaDB personal_events（约 53s，支持断点续传）",
        "extra_args": ["--resume"],
    },
    {
        "num": 11,
        "name": "build_context_doc",
        "desc": "生成 AI 长期上下文文档 person_profile.md",
        "extra_args": [],
    },
    {
        "num": 12,
        "name": "build_profile_from_memory",
        "desc": "生成记忆图谱版 AI 上下文文档 person_profile_v2.md",
        "extra_args": [],
    },
    {
        "num": 13,
        "name": "build_conversation_vector_store",
        "desc": "[Phase 07 Wave 7] turn 叙述回流向量库（独立 collection，需 summary+chroma）",
        "extra_args": ["--write"],
    },
]


# ===== CLI 参数解析 =====

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="个人数据分析项目管道统一入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--from", dest="from_step", type=int, default=1, metavar="N",
        help="从第 N 步开始运行（默认 1）",
    )
    p.add_argument(
        "--only", dest="only_steps", type=str, default="", metavar="N,N",
        help="只运行指定步骤，逗号分隔，如 --only 3,4",
    )
    p.add_argument(
        "--skip", dest="skip_steps", type=str, default="", metavar="N,N",
        help="跳过指定步骤，逗号分隔，如 --skip 10",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="只打印将要执行的步骤，不实际运行",
    )
    p.add_argument(
        "--include-conversation-turns", action="store_true",
        help="显式启用步骤13：build_conversation_vector_store（默认不纳入全量管道）",
    )
    p.add_argument(
        "--agentsview", action="store_true",
        help="先运行 AgentView 会话源集成（Phase 13.5）：snapshot → normalized → canonical，"
             "在步骤 1 前串行执行。默认不运行。",
    )
    p.add_argument(
        "--agentsview-write", action="store_true",
        help="agentsview 阶段实际写入 normalized/canonical DB（默认 dry-run）",
    )
    p.add_argument(
        "--agentsview-only", action="store_true",
        help="只运行 AgentView 会话源集成阶段，不继续执行步骤 1-12",
    )
    p.add_argument(
        "--legacy-integrated",
        action="store_true",
        help=(
            "允许执行已退役的统合步骤 1–12（personal_events / memory 批处理）。"
            "产品默认禁止；取证时再用。"
        ),
    )
    return p.parse_args()


def parse_step_list(s: str) -> set[int]:
    if not s.strip():
        return set()
    return {int(x.strip()) for x in s.split(",")}


# ===== 执行逻辑 =====

def select_steps(args: argparse.Namespace) -> list[dict]:
    only = parse_step_list(args.only_steps)
    skip = parse_step_list(args.skip_steps)

    result = []
    for step in STEPS:
        n = step["num"]
        if n == 13 and not args.include_conversation_turns:
            if not only or 13 not in only:
                continue
        if only and n not in only:
            continue
        if n < args.from_step:
            continue
        if n in skip:
            continue
        result.append(step)
    return result


def fmt_step(step: dict) -> str:
    return f"步骤 {step['num']:>2}  {step['name']:<35}  {step['desc']}"


def run_step(step: dict) -> tuple[bool, float]:
    cmd = [sys.executable, "-m", STEP_MODULES[step["name"]]] + step["extra_args"]

    print(f"\n{'='*70}")
    print(f"[RUN] {fmt_step(step)}")
    print(f"{'='*70}")

    t0 = time.time()
    ret = subprocess.run(cmd, cwd=str(ROOT))
    elapsed = time.time() - t0

    if ret.returncode != 0:
        print(f"\n[FAIL] 步骤 {step['num']} 失败（exit {ret.returncode}），管道已中止。")
        print(f"   修复后可用 --from {step['num']} 从此步重跑。")
        return False, elapsed

    print(f"\n[OK] 步骤 {step['num']} 完成（{elapsed:.1f}s）")
    return True, elapsed


# ===== 主入口 =====

def run_agentsview_stage(write: bool = False) -> bool:
    """Phase 13.5：AgentView 会话源集成（inventory → normalized → canonical）。

    严格串行：先只读 inventory，再生成 normalized（staging + 原子发布），
    再构建 canonical conversation store。任一步失败则中止。
    不修改 AgentView 源库（mode=ro + query_only）。
    """
    mode = "--write" if write else "--dry-run"
    # inventory 始终是报告-only（无 --write 语义）；normalized/canonical 用 mode
    steps: list[tuple[str, str, list[str]]] = [
        (
            "agentsview import inventory",
            "personal_knowledge.application.conversation.import_agentsview_sessions",
            ["--dry-run"],
        ),
        (
            "agentsview normalized build",
            "personal_knowledge.application.conversation.build_agentsview_normalized",
            [mode],
        ),
        (
            "canonical conversation store",
            "personal_knowledge.application.conversation.build_canonical_agent_conversations",
            [mode],
        ),
    ]
    print(f"\n{'='*70}")
    print("[Phase 13.5] AgentView 会话源集成")
    print(f"{'='*70}")
    for label, module, extra in steps:
        cmd = [sys.executable, "-m", module] + extra
        print(f"\n[RUN] {label}")
        print(f"      {' '.join(cmd)}")
        t0 = time.time()
        ret = subprocess.run(cmd, cwd=str(ROOT))
        elapsed = time.time() - t0
        if ret.returncode != 0:
            print(f"[FAIL] {label} 失败（exit {ret.returncode}），agentsview 阶段中止。")
            return False
        print(f"[OK] {label}（{elapsed:.1f}s）")
    return True


# ==============================================================================
# Phase 62-04: v2 conversation orchestration (dry-run / shadow / activation)
#
# Implementation lives in :mod:`personal_knowledge.application.conversation.v2_sync`
# (one module, one change reason). The public seam is re-exported here so the
# sync command and tests keep a stable import path. These seams never touch the
# live canonical ``agent_conversations.sqlite`` / ``personal_system.sqlite``.
# ==============================================================================

from personal_knowledge.application.conversation.v2_sync import (  # noqa: E402
    activate_conversation_generation,
    probe_conversation_sources,
    shadow_conversation_generation,
)


def main() -> None:
    args = parse_args()

    if args.agentsview or args.agentsview_only:
        ok = run_agentsview_stage(write=args.agentsview_write)
        if not ok:
            sys.exit(1)
        if args.agentsview_only or not select_steps(args):
            print("\n[done] agentsview 阶段结束（未继续执行统合管道步骤）。")
            print("提示: 产品入口请用 `pk-sync conversations [--write]`。")
            return

    selected = select_steps(args)

    if not selected:
        print("没有匹配的步骤，请检查 --from / --only / --skip 参数。")
        print("产品对话同步: pk-sync conversations [--write]")
        sys.exit(1)

    # Block default integrated pipeline (steps 1–12/13) without explicit opt-in.
    if not args.legacy_integrated:
        print(
            "[blocked] 统合管道步骤 1–12 已退役，不再作为产品默认流程。\n"
            "\n"
            "请改用:\n"
            "  pk-sync conversations           # dry-run\n"
            "  pk-sync conversations --write   # 发布对话 SSOT\n"
            "\n"
            "若确需取证重跑旧 personal_events/memory 批处理:\n"
            "  python -m personal_knowledge.application.run_pipeline "
            "--legacy-integrated --dry-run\n",
            file=sys.stderr,
        )
        sys.exit(2)

    print(f"\n{'='*70}")
    print("个人数据分析管道 [LEGACY-INTEGRATED / 取证模式]")
    print(f"{'='*70}")
    print(f"共 {len(selected)} 步将执行{'（dry-run 模式，不实际运行）' if args.dry_run else ''}：\n")
    for step in selected:
        marker = "  " if not args.dry_run else "--"
        print(f"  {marker} {fmt_step(step)}")
    print()

    if args.dry_run:
        return

    total_t0 = time.time()
    for step in selected:
        ok, _ = run_step(step)
        if not ok:
            sys.exit(1)

    total = time.time() - total_t0
    print(f"\n{'='*70}")
    print(f"[OK] 全部 {len(selected)} 步完成，总耗时 {total:.0f}s（{total/60:.1f} 分钟）")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
