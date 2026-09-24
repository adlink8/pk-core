"""meta 端点处理器 —— /app 静态托管、/health、/stats、/knowledge 状态。

从 api_server.py 抽出(第二阶段拆分,OC-1)。每个函数接收 handler 与路由上下文,
自行完成 send_response/_send 并返回 None(已完成)或返回 (body, code) 由调用方发送。

关键约束:api_server 模块命名空间的符号(backend / COCKPIT_DIST / _resolve_cockpit_asset
/ _safe_error / _COCKPIT_ASSET_TYPES / _truthy)一律在函数体内经 api_server 延迟解析,
保证测试对 api_server.backend / api_server.COCKPIT_DIST 的 monkeypatch 照常生效。
"""

from __future__ import annotations

from typing import Any


def _api():
    """延迟解析 api_server 模块,避免导入期循环依赖。"""
    import personal_knowledge.services.api_server as api_server

    return api_server


def serve_cockpit_static(handler, ctx: dict[str, Any]) -> None:
    """/app 与 /app/... Cockpit 前端静态托管(SPA fallback + 安全错误封包)。

    与原实现完全一致:
    - /app 精确命中 → 301 到 /app/
    - 资源可解析 → 按扩展名返回,未命中 → cockpit_not_built / cockpit_asset_not_found 404
    """
    api_server = _api()
    url = ctx["url"]
    if url.path == "/app":
        handler.send_response(301)
        handler.send_header("Location", "/app/")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    asset = api_server._resolve_cockpit_asset(url.path)
    if asset is not None:
        ctype = api_server._COCKPIT_ASSET_TYPES.get(
            asset.suffix.lower(), "application/octet-stream"
        )
        handler._send(asset.read_bytes(), 200, ctype)
        return
    if not api_server.COCKPIT_DIST.is_dir():
        body, code = api_server._safe_error("cockpit_not_built", 404)
        handler._send(body, code)
        return
    body, code = api_server._safe_error("cockpit_asset_not_found", 404)
    handler._send(body, code)


def handle_health(handler, ctx: dict[str, Any]) -> None:
    """/health —— 健康检查(含 knowledge.active_collection)。"""
    api_server = _api()
    ku = api_server.backend.get_knowledge_status(probe_chroma=False)
    handler._send(
        api_server._ok(
            {
                "status": "ok",
                "knowledge": {
                    "available": ku.get("available"),
                    "active_collection": ku.get("active_collection"),
                    "unit_count": ku.get("unit_count") or ku.get("db_unit_count"),
                },
            }
        )
    )


def handle_stats(handler, ctx: dict[str, Any]) -> None:
    """/stats —— 数据库 + 向量库 + 知识索引统计概览。"""
    api_server = _api()
    handler._send(api_server._ok(api_server.backend.stats()))


def handle_knowledge_status(handler, ctx: dict[str, Any]) -> None:
    """/knowledge 与 /knowledge/status —— 知识索引状态(?no_chroma=1)。"""
    api_server = _api()
    qs = ctx["qs"]
    probe = not api_server._truthy(qs.get("no_chroma"))
    handler._send(api_server._ok(api_server.backend.get_knowledge_status(probe_chroma=probe)))
