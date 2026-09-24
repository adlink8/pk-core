"""/decision/* 决策只读端点处理器。

从 api_server.py 的 do_GET decision_routes 段原样抽出,行为等价:
- 路由表、参数映射、状态码(200/400)与错误封包格式不变。
- 依赖 api_server.decision_rest_contract 延迟解析(运行时取值),测试 monkeypatch 生效。
"""

from __future__ import annotations


def handle(handler, ctx) -> None:
    import personal_knowledge.services.api_server as api_server

    path = ctx["path"]
    qs = ctx["qs"]
    decision_routes = {
        "/decision/recommendations": "recommendations.list",
        "/decision/recommendation": "recommendations.get",
        "/decision/recommendation/history": "recommendations.history",
        "/decision/recommendation/outcomes": "recommendations.outcomes",
        "/decision/recommendation/effectiveness": "recommendations.effectiveness",
    }
    if path not in decision_routes:
        return
    params = {
        "recommendation_id": qs.get("recommendation_id"),
        "domain": qs.get("domain"),
        "limit": qs.get("limit", "50"),
    }
    if path == "/decision/recommendation":
        params.pop("limit")
        params.pop("domain")
    elif path != "/decision/recommendations":
        params.pop("domain")
    data = api_server.decision_rest_contract(decision_routes[path], params)
    handler._send(api_server._contract(data), 200 if data.get("ok") else 400)
