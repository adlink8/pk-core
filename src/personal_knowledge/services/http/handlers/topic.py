"""/ui/topic* Wiki P0 只读投影端点处理器。

从 api_server.py 的 do_GET topic_routes 段原样抽出,行为等价:
- 路由表、参数映射、状态码(200/404 topic_not_found/400)与错误封包格式不变。
- 依赖 api_server.topic_rest_contract 延迟解析(运行时取值);topic_rest_contract 内部
  亦延迟解析 api_server.TopicProjectionService / WIKI_DERIVED_STORE,测试 monkeypatch 生效。
"""

from __future__ import annotations


def handle(handler, ctx) -> None:
    import personal_knowledge.services.api_server as api_server

    path = ctx["path"]
    qs = ctx["qs"]
    topic_routes = {
        "/ui/topics": "topic.list",
        "/ui/topic": "topic.get",
        "/ui/topic/backlinks": "topic.backlinks",
        "/ui/topic/resolve": "topic.resolve",
    }
    if path not in topic_routes:
        return
    operation = topic_routes[path]
    params = {
        "topic_type": qs.get("topic_type"),
        "topic_id": qs.get("topic_id"),
        "topic_key": qs.get("topic_key"),
        "query": qs.get("query"),
        "cursor": qs.get("cursor"),
        "limit": qs.get("limit", "50"),
    }
    if operation != "topic.list":
        params.pop("cursor")
        if operation != "topic.backlinks":
            params.pop("limit")
    if operation == "topic.resolve":
        params = {"topic_key": qs.get("topic_key"), "query": qs.get("query")}
    data = api_server.topic_rest_contract(operation, params)
    error = data.get("error") if isinstance(data, dict) else None
    status = 200 if data.get("ok") else 404 if error == "topic_not_found" else 400
    handler._send(api_server._contract(data), status)
