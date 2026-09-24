"""/intelligence/* 个人状态/变化只读端点处理器。

从 api_server.py 的 do_GET intelligence_routes 段原样抽出,行为等价:
- 路由表、参数映射、状态码(200/400)与错误封包格式不变。
- 依赖 api_server.intelligence_rest_contract 延迟解析(运行时取值),测试 monkeypatch 生效。
"""

from __future__ import annotations


def handle(handler, ctx) -> None:
    import personal_knowledge.services.api_server as api_server

    path = ctx["path"]
    qs = ctx["qs"]
    intelligence_routes = {
        "/intelligence/state/current": "state.current",
        "/intelligence/state/history": "state.history",
        "/intelligence/changes/recent": "changes.recent",
        "/intelligence/state/explain": "state.explain",
    }
    if path not in intelligence_routes:
        return
    params = {
        "snapshot_id": qs.get("snapshot_id"),
        "run_id": qs.get("run_id"),
        "as_of": qs.get("as_of"),
    }
    if path.endswith("/current") or path.endswith("/history") or path.endswith("/recent"):
        params["limit"] = qs.get("limit", "50")
    if path.endswith("/recent"):
        params["window_start"] = qs.get("window_start")
    if path.endswith("/explain"):
        params.update({
            "assertion_kind": qs.get("assertion_kind"),
            "subject": qs.get("subject"),
            "domain": qs.get("domain"),
            "scope": qs.get("scope"),
            "predicate": qs.get("predicate"),
        })
    data = api_server.intelligence_rest_contract(intelligence_routes[path], params)
    handler._send(api_server._contract(data), 200 if data.get("ok") else 400)
