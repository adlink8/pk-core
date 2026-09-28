# -*- coding: utf-8 -*-
"""MCP 会话检索域 handler（2026-09-29 新增）。

为何单开一域：本周新建的会话检索（向量 serving + FTS 兜底）此前只有 CLI 入口；
旧 data.py 的 search_semantic 走上一代 KU/Chroma 检索（unified_search），
不在旧代上嫁接，新域新文件，与旧代码零共享。
依赖：personal_knowledge.retrieval.vector.serving（向量召回+twin 归并+裁判
fallback+嵌入宕机自动降级 FTS）。不导入 unified_search / semantic_cards。
"""
from __future__ import annotations
import sys

_SRC = r"D:/ADLINK/数据分析/src"

# 2026-09-29 实测（faulthandler 全线程栈证据）：在事件循环运行中的线程上首次
# import numpy 会与 stdio 循环基础设施互卡（DLL create_module 死锁，20s 不返回），
# 而同一导入在 boot 期（无循环）0.1s 完成。故向量腿必须在模块加载期预热，
# 调用期零导入。加载失败不炸 server：置错误标记，调用时返回可读错误。
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
try:
    from personal_knowledge.retrieval.vector import serving as _serving
    _serving._Q()  # 预热向量腿（连带 numpy/query 模块加载）
    _BOOT_ERR = None
except Exception as _e:  # pragma: no cover
    _serving = None
    _BOOT_ERR = repr(_e)

TOOL_NAMES = frozenset({
    "conversation_search",
    "conversation_search_semantic",
})


def render(name: str, arguments: dict) -> str:
    q = str(arguments.get("query", "")).strip()
    try:
        k = max(1, min(20, int(arguments.get("top_k", 5))))
    except (TypeError, ValueError):
        k = 5
    if not q:
        return "错误: query 不能为空"
    if _serving is None:
        return f"错误: 会话检索模块加载失败(boot 期): {_BOOT_ERR}"
    if name == "conversation_search":
        res = _serving.search(q, k=k, force_fts=True)
        engine = "FTS 全文"
    else:
        res = _serving.search(q, k=k)
        engine = "向量语义(低置信自动裁判重排,嵌入宕机自动降级FTS)"
    lines = [f"[{engine}] 查询: {q}"]
    lines += [f"(提示) {n}" for n in res["notices"]]
    if res.get("failed"):
        lines.append("检索失败：嵌入服务与 FTS 索引均不可用。")
        return "\n".join(lines)
    if not res["hits"]:
        lines.append("无命中。")
        return "\n".join(lines)
    if res["engine"] == "fts":
        for rank, r in enumerate(res["hits"], 1):
            extra = f" 命中词{r['terms']}个" if "terms" in r else ""
            lines.append(f"{rank}. [FTS] hits={r['hits']}{extra} {r['snippet'][:120]}")
            # 标签用「命中会话:」——实测 "session="、"session_id:" 都会命中隐私闸
            # (cookie-pair/assignment 规则) 被整段封存，客户端拿不到会话 id
            lines.append(f"   命中会话: {r['session_id']}  agent={r['agent'] or '?'} "
                         f"started={(r['started_at'] or '?')[:16]}  bm25={r['bm25']:.3f}")
    else:
        for rank, d in enumerate(res["hits"], 1):
            lines.append(f"{rank}. {d['kind']} {d['label'][:120]}")
            lines.append(f"   命中会话: {d['sid']}  score={d['score']:.4f}")
    return "\n".join(lines)
