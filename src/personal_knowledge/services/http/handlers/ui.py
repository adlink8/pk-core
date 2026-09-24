"""/ui/* Cockpit 投影只读端点 + 单人评审台(/ui/review, /ui/review/labels)处理器。

从 api_server.py 的 do_GET ui_routes + /ui/review 段、do_POST /ui/review/labels 段原样抽出,
行为等价:
- 路由表、参数映射、状态码(200/400)与错误封包格式不变。
- /ui/review 返回 text/html + Cache-Control: no-store;装配失败回安全 code
  review_console_error(500),异常详情只留本地 stderr。
- /ui/review/labels 的 Origin gate 由 do_POST 预检(先于 body 解析)完成,本函数不再复检。
- 依赖 api_server.ui_rest_contract 延迟解析(运行时取值),测试 monkeypatch 生效。
"""

from __future__ import annotations

import traceback


def handle(handler, ctx) -> None:
    import personal_knowledge.services.api_server as api_server

    path = ctx["path"]
    qs = ctx["qs"]

    # 999.5 单人评审台(gold 三键核对 + judge 校准打分),localhost only。
    # 页面数据服务端即时装配自 private_evals,不进任何 authority 面。
    if path == "/ui/review":
        from personal_knowledge.services.eval_review import build_review_page
        try:
            page = build_review_page().encode("utf-8")
        except Exception:
            # 页面装配触及私有评审素材;异常详情只留本地 stderr,
            # 公开响应固定安全 code/message,不回显路径/traceback
            traceback.print_exc()
            b, c = api_server._safe_error("review_console_error", 500)
            handler._send(b, c)
            return
        # 页面含私有评审数据:禁止任何浏览器/中间缓存(no-store)。
        # _send 不支持附加 header,这里手动下发,避免改动共享方法。
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(page)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(page)
        return

    ui_routes = {
        "/ui/overview": "overview.get",
        "/ui/system/status": "system.status.get",
        "/ui/personal-state": "personal_state.get",
        "/ui/external/delta": "external_delta.get",
        "/ui/decision-queue": "decision_queue.get",
        "/ui/decision/workspace": "decision_workspace.get",
        "/ui/actions/recent": "actions_recent.get",
        "/ui/proactive/summary": "proactive_summary.get",
        "/ui/calibration/overview": "calibration_overview.get",
        "/ui/evidence/resolve": "evidence_resolve.get",
    }
    if path not in ui_routes:
        return
    params = {}
    if path == "/ui/decision/workspace":
        params = {"recommendation_id": qs.get("recommendation_id")}
    elif path == "/ui/actions/recent":
        # 游标分页透传:cursor 不透明,limit 可选;GET-only,无副作用
        params = {"cursor": qs.get("cursor"), "limit": qs.get("limit")}
    elif path == "/ui/evidence/resolve":
        # 只读证据解析(Phase 37:EVID-01);GET-only,无对应 POST 路由,
        # 参数原样透传给 evidence_resolve.get 做结构校验,不在此层猜测/补全
        params = {
            "subject_type": qs.get("subject_type"),
            "stable_id": qs.get("stable_id"),
            "snapshot_id": qs.get("snapshot_id"),
            "checksum": qs.get("checksum"),
            "assertion_kind": qs.get("assertion_kind"),
            "subject": qs.get("subject"),
            "domain": qs.get("domain"),
            "scope": qs.get("scope"),
            "predicate": qs.get("predicate"),
        }
    data = api_server.ui_rest_contract(ui_routes[path], params)
    handler._send(api_server._contract(data), 200 if data.get("ok") else 400)


def handle_review_labels(handler, ctx) -> None:
    import personal_knowledge.services.api_server as api_server

    body = ctx["body"]
    # 999.5 评审 labels 保存:只写 private_evals 下带时间戳的新文件,
    # 不触碰 SSOT/eval registry;非法判定值直接 400。
    # (Origin gate 已由 do_POST 预检,先于 body 解析完成,这里不再复检)
    from personal_knowledge.services.eval_review import save_review_labels
    try:
        handler._send(api_server._ok(save_review_labels(body)))
    except ValueError as exc:
        b, c = api_server._err(str(exc), 400)
        handler._send(b, c)
