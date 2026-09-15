"""Wave 6 Task 3: 构造固定的 prompt 评测样本集。

PLAN 强制:每个样本保留源引用,不得用 synthetic sample 代替真实本地数据。
本脚本从 agent_data.sqlite 抽取 6 类代表性 turn,固化为 conversation_prompt_eval_set.json。

样本覆盖(PLAN Wave 6 Task 3):
  1. short_qa    短问答(开发环境选型,开放性建议)
  2. code_debug  代码排障
  3. ppt_rebuild 多分支任务(PPT 重构,一次性任务 vs 稳定偏好易混淆)
  4. bsod        系统排障(蓝屏,日志分析)
  5. ssh_server  服务器配置(命令/路径密集)
  6. test_design 测试用例设计(等价类划分,规则推理)
  7. long_ctx    长上下文(单 turn 文本 >10k 字符)

每个样本用 build_conversation_summary 的组装逻辑渲染成 turn 文本,
作为 evaluate_conversation_prompt.py 的输入。

用法:
  python build_conversation_eval_set.py --write        # 生成/刷新固定样本集
  python build_conversation_eval_set.py --dry-run      # 只打印样本概览
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
AGENT_DB = ROOT / "Agent" / "structured" / "db" / "agent_data.sqlite"
OUT = ROOT / "integration" / "analysis" / "ai_context" / "conversation_prompt_eval_set.json"

# 固定样本规格:(category, label, session_id, turn_index, 评测关注点)
# turn_index 指组装后第几个实质 turn(0-based,跳过纯 env 序言)
SAMPLES_SPEC: list[tuple[str, str, str, int, str]] = [
    ("short_qa", "短问答(开发环境选型,开放性建议)",
     "rollout-2026-04-04T11-21-44-019d5682-c787-7b60-91b7-d9aaf745409a",
     0, "验证短问答不被过度压缩,保留建议要点而非压缩成空壳"),
    ("code_debug", "代码排障(MQTT + TypeError)",
     "rollout-2026-01-31T21-51-26-019c1452-a60a-7860-a57e-ee00e084a6aa",
     2, "验证错误栈/函数名/修法等关键细节保留"),
    ("ppt_rebuild", "PPT 重构(一次性任务,易被误判为偏好)",
     "rollout-2026-05-06T20-00-33-019dfd29-42a6-7912-bf57-797bef3dbc54",
     1, "验证一次性操作指令不被压缩成稳定偏好"),
    ("bsod", "蓝屏排查(日志分析)",
     "rollout-2026-04-06T16-07-54-019d61d5-7b90-7ec1-803f-23177332e471",
     0, "验证排查路径和结论边界保留"),
    ("ssh_server", "SSH 服务器配置(命令/路径密集)",
     "rollout-2026-04-09T18-03-02-019d71b1-fa95-78c1-9010-edaeb297b9f4",
     0, "验证命令/配置项/路径等细节保留"),
    ("test_design", "测试用例设计(等价类划分,规则推理)",
     "rollout-2026-04-07T08-18-31-019d654e-1f7d-78f0-8a32-f47161454cc0",
     1, "验证规则约束和推理步骤保留(等价类划分/有效无效类)"),
    ("long_ctx", "长上下文(单 turn >10k 字符)",
     "rollout-2026-06-03T11-44-24-019e8b95-18e9-73c1-8d0a-b01f128cd798",
     0, "验证长上下文压缩率与细节平衡"),
]


@dataclass
class EvalSample:
    sample_id: str          # eval:short_qa 等
    category: str
    label: str
    session_id: str
    turn_index: int         # 该 session 第几个实质 turn
    turn_text: str          # 喂给被测 prompt 的原始 turn 文本
    turn_text_len: int
    focus_notes: str        # 该样本的评测关注点
    source_refs: list[str] = field(default_factory=list)


def build_sample(category: str, label: str, sid: str, turn_idx: int,
                 focus: str) -> EvalSample | None:
    """从数据库抽取指定 session 的第 turn_idx 个实质 turn。"""
    # 复用 summary 脚本的组装逻辑,保证 turn 切分与生产一致
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import personal_knowledge.application.conversation.summary as m
    import sqlite3

    con = sqlite3.connect(AGENT_DB)
    raw = con.execute(
        "select event_index, turn_id, role, text, payload_type, raw_file, line_no "
        "from agent_messages where session_id=? and role in ('user','assistant') "
        "order by event_index", (sid,),
    ).fetchall()
    if not raw:
        con.close()
        return None
    msgs = m.dedup_messages(raw)
    tc = m.load_tool_calls(con, sid)
    to = m.load_tool_outputs(con, sid)
    turns = m.assemble_turns(msgs, tc, to)
    con.close()

    # 实质 turn: 含 assistant 回复或有工具调用(跳过纯 env 序言)
    real_turns = [t for t in turns
                  if any(mm["role"] == "assistant" for mm in t["messages"]) or t["tools"]]
    if turn_idx >= len(real_turns):
        return None
    t = real_turns[turn_idx]
    turn_text = m.render_turn_text(t, 1)
    # 取前 3 个 source_ref 作为回溯证据
    refs = []
    for mm in t["messages"][:3]:
        refs.append(f"{mm['raw_file']}:{mm['line_no']}")
    return EvalSample(
        sample_id=f"eval:{category}",
        category=category,
        label=label,
        session_id=sid,
        turn_index=turn_idx,
        turn_text=turn_text,
        turn_text_len=len(turn_text),
        focus_notes=focus,
        source_refs=refs,
    )


def run(write: bool) -> int:
    if not AGENT_DB.exists():
        print(f"[error] 缺少数据库: {AGENT_DB.relative_to(ROOT)}")
        return 1

    samples: list[EvalSample] = []
    skipped: list[str] = []
    for category, label, sid, turn_idx, focus in SAMPLES_SPEC:
        s = build_sample(category, label, sid, turn_idx, focus)
        if s is None:
            skipped.append(f"{category} (session={sid[:30]}.. turn_idx={turn_idx})")
            # 触发自动修正:对长上下文样本,turn_idx 可能不准,回退取第一个实质 turn
            if category == "long_ctx":
                s = build_sample(category, label, sid, 0, focus)
                if s:
                    samples.append(s)
                    continue
            continue
        samples.append(s)

    print(f"评测样本集: {len(samples)}/{len(SAMPLES_SPEC)} 个样本")
    print(f"{'category':14s} | {'turn_len':9s} | label")
    print("-" * 70)
    for s in samples:
        print(f"{s.category:14s} | {s.turn_text_len:6d} 字 | {s.label}")
    if skipped:
        print(f"\n[warn] 跳过 {len(skipped)} 个样本(session_id 失效或 turn_idx 越界):")
        for sk in skipped:
            print(f"  - {sk}")

    if not write:
        print("\n[dry] 未写文件。加 --write 生成 conversation_prompt_eval_set.json。")
        return 0

    if not samples:
        print("[error] 没有可用样本,不写文件。")
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        json.dump([asdict(s) for s in samples], fh, ensure_ascii=False, indent=2)
    print(f"\n已写入: {OUT.relative_to(ROOT)} ({len(samples)} 样本)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="构造 prompt 评测样本集 (Wave 6 Task 3)")
    p.add_argument("--write", action="store_true", help="生成/刷新固定样本集")
    p.add_argument("--dry-run", action="store_true", help="只打印样本概览")
    args = p.parse_args(argv)
    if args.dry_run and args.write:
        print("[error] --dry-run 与 --write 互斥", file=sys.stderr)
        return 2
    return run(args.write)


if __name__ == "__main__":
    raise SystemExit(main())
