"""/api/pi/* Pi 内核/操作投影 + /internal/pi-domain/dispatch + /agent/session 写路由处理器。

从 api_server.py 的 do_GET pi 段与 do_POST pi/session/pi-domain 段原样抽出,行为等价:
- 状态码(200/409/400)、信封结构、安全错误封包格式与现状完全一致。
- SESSION_WRITE_ROUTES 与 /ui/review/labels 的 Origin gate 由 do_POST 预检(先于 body 解析);
  本模块内 pi mutation 路由的 Origin gate 保持原样(在 body 解析之后)。
- 依赖 api_server 命名空间符号(SESSION_WRITE_ROUTES / PI_DOMAIN_GATEWAY /
  PI_DOMAIN_CAPABILITY_HEADER / orchestration_rest_contract 等)延迟解析,
  测试 monkeypatch 生效。
"""

from __future__ import annotations


def handle_get(handler, ctx) -> None:
    import personal_knowledge.services.api_server as api_server

    path = ctx["path"]
    qs = ctx["qs"]

    if path in {"/api/pi/status", "/api/pi/tasks"}:
        payload = api_server.kernel_status() if path.endswith("/status") else {
            "schema_version": "pi_cockpit_event_v1",
            "tasks": api_server.task_list(),
            "observed_at": api_server.kernel_status()["observed_at"],
        }
        # Pi Cockpit schemas validate the metadata projection itself;
        # do not wrap it in the generic {ok,data} envelope.
        handler._send(api_server._contract(payload))
        return

    if path == "/api/pi/operations":
        handler._send(api_server._contract(api_server.operation_list()))
        return

    if path.startswith("/api/pi/operations/"):
        operation_id = path.rsplit("/", 1)[-1]
        handler._send(api_server._contract(api_server.operation_get(operation_id)), 200)
        return

    if path == "/api/pi/events":
        if qs.get("stream", "").lower() in {"1", "true", "yes"}:
            api_server._stream_pi_events(handler)
            return
        event = api_server.safe_event({
            "event_id": qs.get("event_id"),
            "task_id": qs.get("task_id"),
            "state": qs.get("state"),
            "version": qs.get("version"),
        })
        handler._send(api_server._ok(event))
        return


def handle_post(handler, ctx) -> None:
    import personal_knowledge.services.api_server as api_server

    path = ctx["path"]
    body = ctx["body"]

    if path in {"/api/pi/cancel", "/api/pi/resume"}:
        decision = handler._origin_policy_for_request()
        if not decision["allowed"]:
            body_b, code = api_server._safe_error("origin_not_allowed", 403)
            handler._send(body_b, code)
            return
        result = api_server.mutate_task("cancel" if path.endswith("/cancel") else "resume", body)
        handler._send(api_server._contract(result), 200 if result.get("ok") else 409)
        return

    if path.startswith("/api/pi/operations/"):
        decision = handler._origin_policy_for_request()
        if not decision["allowed"]:
            body_b, code = api_server._safe_error("origin_not_allowed", 403)
            handler._send(body_b, code)
            return
        parts = path.split("/")
        if len(parts) != 6 or parts[-1] not in {"cancel", "resume", "reconcile"}:
            handler._send(api_server._contract({"ok": False, "error": {"code": "operation_route_invalid"}}), 400)
            return
        payload = {**body, "operation_id": body.get("operation_id") or parts[-2]}
        result = api_server.mutate_operation(parts[-1], payload)
        handler._send(api_server._contract(result), 200 if result.get("ok") else 409)
        return

    if path == "/internal/pi-domain/dispatch":
        # Internal means loopback process ownership plus an injected capability;
        # operation IDs are validated by the gateway and never become imports.
        operation = body.get("operation")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        result = api_server.PI_DOMAIN_GATEWAY.invoke(
            operation, params,
            capability=handler.headers.get(api_server.PI_DOMAIN_CAPABILITY_HEADER),
        )
        handler._send(api_server._contract(result), 200 if result.get("ok") else 400)
        return

    if path in api_server.SESSION_WRITE_ROUTES:
        operation = api_server.SESSION_WRITE_ROUTES[path]
        expected = path.rsplit("/", 1)[-1].replace("-", "_")
        if operation == "session.execute" and path != "/agent/session/execute":
            preview = body.get("preview") or {}
            if preview.get("operation") != expected:
                data = api_server.compact_envelope(api_server.GuardedOrchestrationInterface._envelope(
                    operation, ok=False, code="route_operation_mismatch",
                ))
            else:
                data = api_server.orchestration_rest_contract(operation, body)
        else:
            data = api_server.orchestration_rest_contract(operation, body)
        handler._send(api_server._contract(data), 200 if data.get("ok") else 400)
        return
