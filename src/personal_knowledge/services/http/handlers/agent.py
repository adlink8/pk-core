"""/agent/* 只读 authority 与 session 只读端点处理器。

从 api_server.py 的 do_GET agent_routes + session_read_routes 段原样抽出,行为等价:
- 路由表、参数映射、状态码(200/400)与错误封包格式不变。
- 依赖 api_server.agent_read_rest_contract / orchestration_rest_contract 延迟解析
  (运行时取值),测试 monkeypatch 生效。
"""

from __future__ import annotations


def handle(handler, ctx) -> None:
    import personal_knowledge.services.api_server as api_server

    path = ctx["path"]
    qs = ctx["qs"]

    agent_routes = {
        "/agent/external": "external.list",
        "/agent/external/item": "external.get",
        "/agent/external/explain": "external.explain",
        "/agent/analysis": "analysis.list",
        "/agent/analysis/item": "analysis.get",
        "/agent/analysis/explain": "analysis.explain",
        "/agent/pilot": "pilot.list",
        "/agent/pilot/item": "pilot.get",
        "/agent/pilot/explain": "pilot.explain",
        "/agent/calibration": "calibration.list",
        "/agent/calibration/item": "calibration.get",
        "/agent/calibration/explain": "calibration.explain",
    }
    if path in agent_routes:
        operation = agent_routes[path]
        params = {"limit": qs.get("limit", "50")}
        if operation.startswith("external.") and operation != "external.list":
            params = {"resource_type": qs.get("resource_type"), "resource_id": qs.get("resource_id")}
        elif operation.startswith("analysis.") and operation != "analysis.list":
            params = {"run_id": qs.get("run_id")}
        elif operation.startswith("pilot.") and operation != "pilot.list":
            params = {"case_id": qs.get("case_id"), "as_of": qs.get("as_of")}
            if operation == "pilot.get":
                params.pop("as_of")
        elif operation.startswith("calibration.") and operation != "calibration.list":
            params = {"protocol_id": qs.get("protocol_id")}
        data = api_server.agent_read_rest_contract(operation, params)
        handler._send(api_server._contract(data), 200 if data.get("ok") else 400)
        return

    session_read_routes = {
        "/agent/session/resume": "session.resume",
        "/agent/session/explain": "session.explain",
    }
    if path in session_read_routes:
        data = api_server.orchestration_rest_contract(
            session_read_routes[path], {"session_id": qs.get("session_id"), "now": qs.get("now")},
        )
        handler._send(api_server._contract(data), 200 if data.get("ok") else 400)
        return
