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
# 2026-09-29 夜测补：HTTP stateless 拿不到 clientInfo，call_log 客户端标签兜底用此值
os.environ.setdefault("PERSONAL_DATA_MCP_CLIENT_TAG", "http")

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

def _prewarm() -> None:
    """2026-09-29 夜测改动：起服务前把检索链路焐热。
    为何：冷启动三件套（语料池加载 ~6s + Ollama 嵌入首调 + Jev 裁判进 GPU ~30s+）
    原先全部落在第一个真实请求上，且同步冻结事件循环——实测已打到真实客户端
    流量（首个查询 8-12s 无响应）。boot 期做掉，起来即热。
    失败不阻塞启动：预热挂了服务照起，行为退回旧冷启动。"""
    try:
        from personal_knowledge.retrieval.vector import serving
        serving.load_pool_cached()
        serving.search("检索服务冷启动预热探针", k=1)  # 池+嵌入热链路
        serving._Q().get_judge()                        # 裁判模型显式进 GPU
        print("[prewarm] 池+嵌入+裁判 已热", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[prewarm] 失败(不阻塞启动): {e!r}", file=sys.stderr, flush=True)


def main() -> None:
    _prewarm()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8789, log_level="warning")
