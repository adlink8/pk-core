"""个人数据系统 MCP Server —— 把向量库 + 数据库 + 知识索引暴露为 MCP tools。

让任何支持 MCP 的 AI 客户端(Claude Desktop / Cursor / ZCode / Continue 等)
能直接检索用户的历史数据与知识单元,无需手写集成代码。

工具分档（环境变量 PERSONAL_DATA_MCP_PROFILE=core|full，默认 core）：

  core（KU 架构默认，约 14 个）
    search_semantic, knowledge_status, stats
    list_google_assertions, get_google_assertion
    get_memory_profile, get_memory_by_subject
    data_list_events, data_list_memories, data_list_relations
    data_get_event_by_id, data_get_memory_by_id
    data_aggregate, data_export, data_quality_report

  full 额外恢复兼容工具：
    query_events（≈ data_list_events）, get_event_detail（≈ data_get_event_by_id）
    list_categories（≈ data_aggregate group_by=category）
    data_timeline

  已合并：data_export_all + data_export_query → data_export（query 可选）

启动方式(stdio 传输,MCP 标准协议):

    python -m personal_knowledge.services.mcp_server

客户端配置(Claude Desktop / Cursor / ZCode 等):

    {
      "mcpServers": {
        "personal-data": {
          "command": "python",
          "args": ["-m", "personal_knowledge.services.mcp_server"],
          "cwd": "D:/ADLINK/数据分析",
          "env": { "PERSONAL_DATA_MCP_PROFILE": "core" }
        }
      }
    }

设计原则:
- Data tools 复用 unified_search.py 的 /data/* contract,直接读取本地 SQLite
- 语义检索与其他工具一致,进程内直连 backend.search_knowledge_units（不再经
  HTTP 回环调用常驻 REST API）
- 所有 tool 返回结构化文本(AI 友好),内部异常被捕获转成错误提示
- 统一出口经 privacy_guard 扫描明文密钥/凭据并自动封存
- MCP 本身使用 stdio
- 每次调用记录简要日志到 stderr(不影响 stdio 协议)

实现拆分（重构后）:
- 本文件只保留 server 生命周期 + ALL_TOOLS 汇总导出 + 轻量分派
- tool schema 定义在 personal_knowledge/mcp/tool_definitions.py
- 按域拆分的 handler 在 personal_knowledge/mcp/handlers/（data / intelligence /
  decision / proactive / agent / orchestration）
- 所有对外符号(ALL_TOOLS / CORE_TOOL_NAMES / *tool_contract / _format_* /
  TOOLS / active_tools / handle_list_tools)原地保留,旧 import 路径不变
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

from mcp.server import Server  # noqa: E402
from mcp.server.lowlevel.server import NotificationOptions  # noqa: E402
from mcp.server.models import InitializationOptions  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402

# MCP server 是按客户端启动的短生命周期进程。默认用 CPU 生成查询向量，
# 避免与常驻 REST API 的 GPU 模型发生 CUDA 初始化竞争。
os.environ.setdefault("PERSONAL_DATA_EMBED_DEVICE", "cpu")
import mcp.types as types  # noqa: E402

from personal_knowledge.core.privacy_guard import guard_mcp_payload  # noqa: E402
from personal_knowledge.mcp_tools.tool_definitions import (  # noqa: E402
    CORE_TOOL_NAMES,
    FULL_ONLY_TOOL_NAMES,
    ALL_TOOLS,
    active_tools,
    _mcp_profile,
)
from personal_knowledge.mcp_tools.handlers import render_tool  # noqa: E402
from personal_knowledge.mcp_tools.handlers.agent import agent_read_tool_contract  # noqa: E402
from personal_knowledge.mcp_tools.handlers.decision import decision_tool_contract  # noqa: E402
from personal_knowledge.mcp_tools.handlers.intelligence import intelligence_tool_contract  # noqa: E402
from personal_knowledge.mcp_tools.handlers.orchestration import orchestration_tool_contract  # noqa: E402
from personal_knowledge.mcp_tools.handlers.proactive import proactive_tool_contract  # noqa: E402
from personal_knowledge.mcp_tools.handlers._format import (  # noqa: E402
    _event_contract_args,
    _format_categories,
    _format_detail,
    _format_knowledge_status,
    _format_memory_detail,
    _format_memory_profile,
    _format_query,
    _format_semantic,
    _format_stats,
    _json_contract,
)

# 兼容旧测试与 import 名：拆分前这些符号直接定义在本模块。
# （ALL_TOOLS 之上仍保留 CORE_TOOL_NAMES / FULL_ONLY_TOOL_NAMES / active_tools，
#  保证 profile 过滤与工具列表契约不变。）
TOOLS = active_tools()


# === MCP Server ===========================================================

server = Server("personal-data")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """声明本 server 提供哪些 tools。客户端启动时调一次。"""
    return active_tools()


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent]:
    """处理 tool 调用。所有异常都转成文本返回,避免 server 崩溃。"""
    arguments = arguments or {}
    # 日志走 stderr,不污染 stdio 协议
    log_arguments = {
        key: ("[REDACTED]" if key in {"confirmation_token", "token", "secret"} else value)
        for key, value in arguments.items()
    }
    print(f"[mcp] call {name} args={log_arguments}", file=sys.stderr, flush=True)

    try:
        # 轻量分派：转发到按域拆分的 handlers 包
        text = render_tool(name, arguments)
    except Exception as e:
        # 捕获所有异常,返回错误信息(而非让 server 崩溃)
        tb = traceback.format_exc()
        text = f"工具 {name} 执行失败: {e}\n\n{tb}"
        print(f"[mcp] error {name}: {e}", file=sys.stderr, flush=True)

    # 统一隐私出口：所有 tool 正文先过密钥/凭据封存
    safe = guard_mcp_payload(text)
    if safe != text:
        print(f"[mcp] privacy_guard sealed tool={name}", file=sys.stderr, flush=True)
    return [types.TextContent(type="text", text=safe)]


async def main() -> None:
    """stdio 模式启动 server。"""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="personal-data",
                server_version="1.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
