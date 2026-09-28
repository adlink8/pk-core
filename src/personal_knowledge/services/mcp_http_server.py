# -*- coding: utf-8 -*-
"""MCP HTTP 常驻服务实现（streamable-http, 仅监听 127.0.0.1:8789）。

为何入口在 integration/scripts/mcp_http_server.py（shim）而不是本文件直接跑：
本目录存在既有包 services/http/，若本文件被当作脚本直接执行，sys.path[0]=本目录
会遮蔽标准库 http（httpx 导入链 import http.client 即炸，2026-09-29 实测）。
shim 从 integration/scripts 启动则 sys.path[0] 无遮蔽，本模块只经包路径导入。

安全：仅绑定 127.0.0.1，不对局域网暴露；无鉴权=信任本机用户（与 stdio 同信任域）。
回退：注销计划任务 pk-mcp-http + 删除本文件与 shim 即整体回退；stdio 客户端零感知。
"""
from __future__ import annotations
import contextlib
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
os.environ.setdefault("PERSONAL_DATA_MCP_PROFILE", "core")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

from personal_knowledge.services.mcp_server import server  # noqa: E402 复用同一 Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, Response  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402

session_manager = StreamableHTTPSessionManager(
    app=server, json_response=True, stateless=True)


async def health(request: Request) -> Response:
    return JSONResponse({"ok": True, "service": "personal-data-mcp",
                         "transport": "streamable-http"})


@contextlib.asynccontextmanager
async def lifespan(app):
    """streamable-http 会话管理器要求包在 run() 里。"""
    async with session_manager.run():
        yield


app = Starlette(
    lifespan=lifespan,
    # 本版 Starlette 的 Mount 不匹配裸 "/mcp"（实测 404），故用兜底 Mount("/")
    # 接住 /mcp 与 /mcp/ 两种写法（.agents/mcp_config.json 即无尾斜杠写法）；
    # manager 对路径不敏感，/health 由前置 Route 优先命中
    routes=[
        Route("/health", health),
        Mount("/mcp/", app=session_manager.handle_request),
        Mount("/", app=session_manager.handle_request),
    ],
)
# 新版 Starlette 无 redirect_slashes 构造参数，改在 router 属性上关闭
app.router.redirect_slashes = False

def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8789, log_level="warning")
