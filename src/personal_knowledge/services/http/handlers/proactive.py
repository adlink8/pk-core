"""/proactive/* 主动情报只读端点处理器。

从 api_server.py 的 do_GET proactive_routes 段原样抽出,行为等价:
- 路由表、参数映射、状态码(200/400)与错误封包格式不变。
- 依赖 api_server.proactive_rest_contract 延迟解析(运行时取值),测试 monkeypatch 生效。
"""

from __future__ import annotations


def handle(handler, ctx) -> None:
    import personal_knowledge.services.api_server as api_server

    path = ctx["path"]
    qs = ctx["qs"]
    proactive_routes = {
        "/proactive/inbox": "inbox.list",
        "/proactive/digest": "digest.get",
        "/proactive/candidate": "candidates.get",
        "/proactive/candidate/explain": "candidates.explain",
        "/proactive/controls/status": "controls.status",
        "/proactive/metrics": "metrics.get",
    }
    if path not in proactive_routes:
        return
    params = {"candidate_id": qs.get("candidate_id"), "domain": qs.get("domain"), "limit": qs.get("limit", "50"), "as_of": qs.get("as_of")}
    if path in {"/proactive/candidate", "/proactive/candidate/explain"}:
        params = {"candidate_id": qs.get("candidate_id")}
    elif path == "/proactive/controls/status":
        params = {"candidate_id": qs.get("candidate_id"), "as_of": qs.get("as_of")}
    elif path == "/proactive/metrics":
        params = {}
    data = api_server.proactive_rest_contract(proactive_routes[path], params)
    handler._send(api_server._contract(data), 200 if data.get("ok") else 400)
