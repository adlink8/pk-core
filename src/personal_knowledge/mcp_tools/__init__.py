"""业务 MCP 工具包：tool schema 定义 + 按域拆分的 handler。

曾用名 personal_knowledge/mcp/（namespace 子包），为避免与第三方 MCP SDK
（site-packages/mcp）同名冲突而更名为 mcp_tools。本包为常规包，
与 SDK 包名无冲突，可安全做常规导入。

结构：
    tool_definitions.py          工具名常量 + ALL_TOOLS schema + profile 过滤
    handlers/                    按域拆分（data / intelligence / decision /
                                 proactive / agent / orchestration）
"""
