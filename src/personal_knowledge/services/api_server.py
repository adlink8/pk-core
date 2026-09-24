"""个人数据系统 REST API —— 把向量库 + 数据库暴露成 HTTP 接口。

用 Python 标准库 http.server 实现,零额外依赖。覆盖两种接入场景:
1. API 调用:任何能发 HTTP 请求的程序(curl/Postman/前端/其他服务)都能检索用户历史
2. RAG 平台接入:Dify / FastGPT / Coze 等平台都能配"自定义 HTTP 工具"接进来

所有接口内部都走 unified_search 后端,和 CLI / MCP 行为完全一致。

=== 接口列表 ============================================================

GET  /stats                 数据库 + 向量库 + 知识索引统计概览
GET  /knowledge             知识索引状态(active collection / unit_count)
GET  /knowledge/status      同上(别名)
GET  /google/assertions     Google 轻量断言列表(?type=&limit=&offset=；非 KU)
GET  /google/assertions/<id> 单条 Google 轻量断言
GET  /categories            所有 category_v2 分布(?source=可选过滤)
GET  /memory                长期记忆概览(?type=可选过滤)
GET  /memory/<subject>      单条记忆详情 + 关系(?neighbors=N 可选)
POST /search/semantic       语义检索(knowledge-first + raw fallback)
POST /search/cards          MVP 会话卡检索(semantic_mvp_v3.sqlite 只读;body 传 session_id 则返回单卡详情)
POST /search/query          精确查询(结构化条件过滤 sqlite)
GET  /event/<event_id>      单条事件全字段
GET  /profile               返回 AI 长期上下文文档内容(RAG 注入用)
GET  /health                健康检查(含 knowledge.active_collection)
GET  /ui/overview           Personal Decision Cockpit 总览投影(五权威只读聚合)  [FROZEN 2026-08-11]
GET  /ui/system/status      Cockpit 系统状态(端口探活 / 知识索引 / 权威 DB 可读性)  [FROZEN 2026-08-11]
GET  /ui/personal-state     Cockpit 个人状态投影(八域断言 / 生命周期 / 近期变化)  [FROZEN 2026-08-11]
GET  /ui/external/delta     Cockpit 外部数据增量投影(source / fact / delta 分类)  [FROZEN 2026-08-11]
GET  /ui/decision-queue     Cockpit 决策队列投影(六 stage 看板分组)  [FROZEN 2026-08-11]
GET  /ui/decision/workspace Cockpit 决策工作区投影(?recommendation_id=<id>,四节聚合)  [FROZEN 2026-08-11]
GET  /ui/actions/recent     Cockpit 近期行动投影(最近推荐的全链六阶段时间线)  [FROZEN 2026-08-11]
GET  /ui/proactive/summary  Cockpit 主动情报摘要(inbox 分组 + 噪声指标)  [FROZEN 2026-08-11]
GET  /ui/calibration/overview Cockpit 校准总览(逐 protocol verdict 摘要)  [FROZEN 2026-08-11]
GET  /ui/evidence/resolve  Cockpit 只读证据解析(?subject_type=personal_state|external_fact|decision
                            &stable_id=&snapshot_id=&checksum=,personal_state 另需
                            assertion_kind/subject/domain/scope/predicate)  [FROZEN 2026-08-11]
GET  /ui/topics             Wiki P0 topic.list 只读目录(保持可用)
GET  /ui/topic               Wiki P0 topic.get 只读主题投影(保持可用)
GET  /ui/topic/backlinks     Wiki P0 topic.backlinks 显式关系(保持可用)
GET  /ui/topic/resolve       Wiki scoped read fallback provenance(保持可用)
GET  /app[/<path>]          Cockpit 前端静态托管(apps/personal_decision_cockpit/dist,SPA fallback)  [FROZEN 2026-08-11]

FROZEN 说明:2026-08-11 起 Cockpit(web)入口停用,产品方向调整为 wiki 统合层,web 不再
是核心入口。路由定义与代码保留不删,分发处已注释(见 do_GET 内 "FROZEN 2026-08-11" 段落),
恢复时取消对应注释即可。wiki 4 条接口不受影响。

GET  接口也可改用 POST(方便前端统一处理),参数走 query string。
POST 接口参数走 JSON body。

=== 启动 ================================================================

    python api_server.py [--host 127.0.0.1] [--port 8000]

默认只监听 127.0.0.1(本地安全,不对外暴露)。

=== 示例 ================================================================

    # 统计概览
    curl http://127.0.0.1:8000/stats

    # 知识索引状态
    curl http://127.0.0.1:8000/knowledge

    # 语义检索(POST + JSON；knowledge-first + raw fallback)
    curl -X POST http://127.0.0.1:8000/search/semantic ^
         -H "Content-Type: application/json" ^
         -d "{\"query\": \"PPT 排版\", \"top_k\": 3}"

    # 精确查询
    curl -X POST http://127.0.0.1:8000/search/query ^
         -H "Content-Type: application/json" ^
         -d "{\"source\": \"Agent\", \"month\": \"2025-03\"}"

    # 拿 AI 长期上下文文档(注入 RAG / system prompt)
    curl http://127.0.0.1:8000/profile

设计原则:
- 纯标准库,装了 Python 就能跑,不强求 Flask/FastAPI
- 统一返回 {"ok": bool, "data": ..., "error": ...} 结构
- 内部异常不泄露栈,只回简短错误信息
- 出站 JSON 统一经 privacy_guard 封存明文密钥/凭据（MCP/Apps 同源）
- 默认 127.0.0.1 only,需要对外自己加反代 + 鉴权
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

# When launched as a script (`python .../services/api_server.py`), the
# interpreter prepends the script directory (`services/`) to sys.path; the
# local `services/http/` package would then shadow the standard-library
# `http.server`. Drop the script directory from sys.path before any stdlib
# import so both direct-script and `python -m` launches behave identically.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR in sys.path:
    sys.path.remove(_SCRIPT_DIR)

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.parse import urlparse, parse_qs, unquote  # noqa: E402

# 让本模块能找到同目录依赖
_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

import personal_knowledge.retrieval.unified_search as backend  # noqa: E402
from personal_knowledge.core.privacy_guard import guard_jsonable, guard_text  # noqa: E402
from personal_knowledge.core.project_paths import UNIFIED_DB  # noqa: E402
from personal_knowledge.intelligence.service import IntelligenceService  # noqa: E402
from personal_knowledge.intelligence.decision.service import DecisionFeedbackService  # noqa: E402
from personal_knowledge.intelligence.proactive.service import ProactiveIntelligenceService  # noqa: E402
from personal_knowledge.services.decision_intelligence_reads import (  # noqa: E402
    DecisionIntelligenceReadService,
)
from personal_knowledge.services.orchestration_service import (  # noqa: E402
    GuardedOrchestrationInterface,
)
from personal_knowledge.services.pi_domain_gateway import (  # noqa: E402
    PI_DOMAIN_CAPABILITY_HEADER,
    PiDomainGateway,
)
from personal_knowledge.services.pi_read_dispatch import (  # noqa: E402
    make_descriptive_evidence_tool,
    make_real_warehouse_tools,
    read_handler as pi_read_handler,
)
from personal_knowledge.services.warehouse_mutations import (  # noqa: E402
    SqliteWarehouseStore,
    WarehouseOperationLedger,
)
from personal_knowledge.services.semantic_maintenance_tools import SemanticMaintenanceTools  # noqa: E402
from personal_knowledge.services.retrieval_maintenance_tools import RetrievalMaintenanceTools  # noqa: E402
from personal_knowledge.services.snapshot_release_tools import SnapshotAuthorityFixture, SnapshotReleaseTools  # noqa: E402
from personal_knowledge.services.pi_runtime_projection import (  # noqa: E402
    kernel_status,
    mutate_task,
    open_event_stream,
    safe_event,
    task_list,
)
from personal_knowledge.services.pi_operation_projection import (  # noqa: E402
    mutate_operation,
    operation_get,
    operation_list,
)
from personal_knowledge.services.agent_contract import compact_envelope  # noqa: E402
from personal_knowledge.services.ui_projection import (  # noqa: E402
    CockpitProjectionService,
)
from personal_knowledge.services.topic_projection import (  # noqa: E402
    TopicProjectionService,
)
from personal_knowledge.wiki.materialization import WikiMaterializer  # noqa: E402
from personal_knowledge.services.http_contracts import (  # noqa: E402,F401 re-export: 外部仍从 api_server 导入
    agent_read_rest_contract,
    decision_rest_contract,
    intelligence_rest_contract,
    orchestration_rest_contract,
    proactive_rest_contract,
    topic_rest_contract,
    ui_rest_contract,
)
from personal_knowledge.services.http import meta_handlers  # noqa: E402
from personal_knowledge.services.http.handlers import (  # noqa: E402
    agent as agent_handlers,
    data as data_handlers,
    decision as decision_handlers,
    intelligence as intelligence_handlers,
    orchestration as orchestration_handlers,
    proactive as proactive_handlers,
    topic as topic_handlers,
    ui as ui_handlers,
)

# AI 长期上下文文档路径(给 /profile 用)
ROOT = _THIS_DIR.parents[1]
PROFILE_MD = ROOT / "integration" / "analysis" / "ai_context" / "person_profile.md"
WIKI_DERIVED_STORE = ROOT / "var" / "db" / "personal_wiki_projection.sqlite"


def _build_pi_domain_gateway() -> PiDomainGateway:
    """Build one process-owned domain gateway.

    The test ledger is opt-in and isolated by an explicit environment path.
    Normal API startup keeps the existing no-authority behaviour for project
    mutations; end-to-end tests can inject a real SQLite observation point
    without touching the production warehouse.

    v2.0 Phase 55-57: the read branch is wired to real data sources via
    ``read_handler`` (pi_read_dispatch) and warehouse read tools get real
    per-authority metadata (warehouse metadata factory). Both are fail-safe:
    missing DBs degrade to synthetic/zero metadata instead of crashing.
    """
    real_read_handler = pi_read_handler
    real_warehouse_tools = make_real_warehouse_tools()
    real_evidence_tool = make_descriptive_evidence_tool()
    ledger_path = str(os.environ.get("PI_DOMAIN_TEST_LEDGER_PATH") or "").strip()
    if ledger_path:
        store = SqliteWarehouseStore(ledger_path)
        ledger = WarehouseOperationLedger(ledger_path, store=store)
        return PiDomainGateway(
            warehouse_ledger=ledger,
            semantic_tools=SemanticMaintenanceTools(ledger),
            retrieval_tools=RetrievalMaintenanceTools(ledger),
            snapshot_tools=SnapshotReleaseTools(ledger, authority=SnapshotAuthorityFixture()),
            read_handler=real_read_handler,
            warehouse_tools=real_warehouse_tools,
            evidence_tool=real_evidence_tool,
        )
    return PiDomainGateway(
        read_handler=real_read_handler,
        warehouse_tools=real_warehouse_tools,
        evidence_tool=real_evidence_tool,
    )


PI_DOMAIN_GATEWAY = _build_pi_domain_gateway()

# Personal Decision Cockpit 前端构建产物(SPA, npm run build 生成)
COCKPIT_DIST = ROOT / "apps" / "personal_decision_cockpit" / "dist"

_COCKPIT_ASSET_TYPES = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".map": "application/json",
}


def _resolve_cockpit_asset(url_path: str) -> Path | None:
    """把 /app/... URL 映射到 COCKPIT_DIST 内的文件;越界 / 缺失返回 None。

    /app、/app/ 与无扩展名子路径一律回退 index.html(SPA fallback);
    有扩展名的路径做 resolve() 后校验仍位于 COCKPIT_DIST 内(防 ../ 穿越)。
    """
    if not (url_path == "/app" or url_path.startswith("/app/")):
        return None
    if not COCKPIT_DIST.is_dir():
        return None
    rel = unquote(url_path[len("/app"):]).lstrip("/")
    segments = [seg for seg in rel.split("/") if seg and seg != "."]
    index = COCKPIT_DIST / "index.html"
    if not segments:
        return index if index.is_file() else None
    if any(seg == ".." for seg in segments):
        return None
    candidate = COCKPIT_DIST.joinpath(*segments)
    if not candidate.suffix:
        return index if index.is_file() else None
    try:
        resolved = candidate.resolve()
        root = COCKPIT_DIST.resolve()
    except OSError:
        return None
    if resolved != root and root not in resolved.parents:
        return None
    return resolved if resolved.is_file() else None


# === 同源 transport 与 CORS 策略（Phase 36：D-36-03） ======================
#
# 生产 Cockpit 由本进程以 /app 同源托管，浏览器同源 fetch 天然不受 CORS 限制，
# 不需要任何 Access-Control-Allow-Origin 响应头。唯一需要显式 CORS 的场景是
# 本地开发：Vite dev server（默认 127.0.0.1:5173）与本 REST 进程（默认 8000）
# 端口不同，浏览器发起的是跨源请求。允许的开发 Origin 只能来自启动时的显式
# 配置（环境变量），不接受任何请求内容扩大该列表。
_DEFAULT_DEV_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")


def _dev_allowed_origins() -> frozenset[str]:
    """显式开发 Origin allowlist：内置 Vite 默认端口 + PK_COCKPIT_DEV_ORIGINS(逗号分隔)。"""
    raw = os.environ.get("PK_COCKPIT_DEV_ORIGINS", "")
    extra = tuple(item.strip() for item in raw.split(",") if item.strip())
    return frozenset(_DEFAULT_DEV_ORIGINS + extra)


def _same_origin(origin: str, host_header: str | None) -> bool:
    """判断请求 Origin 是否与本进程自身(生产 /app 同源)一致。"""
    if not host_header:
        return False
    return origin in (f"http://{host_header}", f"https://{host_header}")


def _origin_policy(origin: str | None, host_header: str | None) -> dict:
    """集中的 Origin 判定,是 CORS 响应与跨 Origin mutation 拒绝的唯一决策点。

    - 无 Origin(既有本地非浏览器调用,如 curl/CLI/MCP): 放行,不下发 CORS header。
    - Origin 与本进程 Host 一致(生产同源 Cockpit): 放行,不下发 CORS header
      (浏览器同源请求本就不受 CORS 约束)。
    - Origin 命中显式开发 allowlist: 放行,下发该 Origin 专属 CORS header。
    - 其余任意 Origin: 拒绝,不下发 CORS header,不回显该 Origin。
    """
    if not origin:
        return {"allowed": True, "reason": "no_origin", "cors_origin": None}
    if _same_origin(origin, host_header):
        return {"allowed": True, "reason": "same_origin", "cors_origin": None}
    if origin in _dev_allowed_origins():
        return {"allowed": True, "reason": "dev_origin", "cors_origin": origin}
    return {"allowed": False, "reason": "origin_not_allowed", "cors_origin": None}


# 需要 Origin gate 前置拦截的受控 session 写路由(mutation 抵达 orchestration_rest_contract 之前)
SESSION_WRITE_ROUTES: dict[str, str] = {
    "/agent/session/prepare": "session.prepare",
    "/agent/session/confirm": "session.confirm",
    "/agent/session/preview": "session.preview",
    "/agent/session/execute": "session.execute",
    "/agent/session/generate": "session.execute",
    "/agent/session/publish": "session.execute",
    "/agent/session/decide": "session.execute",
    "/agent/session/preregister": "session.execute",
    "/agent/session/action-start": "session.execute",
    "/agent/session/action-complete": "session.execute",
    "/agent/session/observe": "session.execute",
    "/agent/session/calibrate": "session.execute",
}


# === 安全公开错误目录（Phase 36：D-36-06） ==================================
#
# /app 静态资源缺失/越界、Origin 拒绝与受控 mutation 的 transport 错误共用
# 固定 code/message；不得拼接请求路径、异常文本、密钥、provider 响应体、
# confirmation token 或 HMAC。详细诊断只进本地 stderr(traceback.print_exc)。
_SAFE_ERRORS: dict[str, str] = {
    "cockpit_asset_not_found": "请求的前端资源不存在",
    "cockpit_not_built": "前端未构建,请先执行 npm run build",
    "origin_not_allowed": "跨源请求已拒绝",
    "internal_error": "服务器内部错误",
    # 999.5 评审台页面装配失败(私有评审素材路径/异常详情只留本地 stderr)
    "review_console_error": "评审台页面装配失败",
}


def _safe_error(code: str, http_status: int) -> tuple[bytes, int]:
    """构造 allowlisted 安全错误 envelope;code 只能是模块内固定字面量,不接受外部输入。"""
    message = _SAFE_ERRORS.get(code, "请求处理失败")
    payload = {"ok": False, "error": {"code": code, "message": message}}
    return (
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        http_status,
    )


_PI_EVENT_STATE = {
    "task_accepted": "queued", "task_started": "running",
    "task_completed": "succeeded", "task_failed": "failed",
    "task_cancel_requested": "cancel_requested", "error": "failed",
}


def _project_kernel_sse_event(event: dict) -> dict:
    event_type = str(event.get("type") or "")
    payload_ref = event.get("payload_ref") if isinstance(event.get("payload_ref"), dict) else {}
    task_id = str(event.get("correlation_id") or payload_ref.get("ref") or "")
    return safe_event({
        "event_id": event.get("event_id"),
        "task_id": task_id,
        "session_id": "",
        "state": _PI_EVENT_STATE.get(event_type, "stale"),
        "version": 0,
        "progress": None,
        "tool_label": "pi-kernel task",
        "evidence_refs": [],
        "observed_at": event.get("occurred_at"),
    })


def _stream_pi_events(handler: "Handler") -> None:
    """Proxy Kernel SSE and project every record to safe cockpit metadata."""
    upstream = None
    headers_sent = False
    try:
        upstream = open_event_stream(handler.headers.get("Last-Event-ID"))
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache, no-transform")
        handler.send_header("Connection", "keep-alive")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        headers_sent = True
        event_id, event_name, data_lines = "", "", []
        for raw_line in upstream:
            line = raw_line.decode("utf-8", errors="strict").rstrip("\r\n")
            if line.startswith("id:"):
                event_id = line[3:].strip()
            elif line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line and data_lines:
                try:
                    payload = json.loads("\n".join(data_lines))
                    if event_name == "heartbeat":
                        output = f"event: heartbeat\ndata: {json.dumps({'status': 'alive', 'latest_sequence': int(payload.get('latest_sequence') or 0)}, ensure_ascii=False)}\n\n"
                    elif isinstance(payload, dict) and isinstance(payload.get("event"), dict):
                        safe = _project_kernel_sse_event(payload["event"])
                        output = f"id: {event_id}\nevent: pi-event\ndata: {json.dumps(safe, ensure_ascii=False)}\n\n"
                    else:
                        output = ""
                    if output:
                        handler.wfile.write(output.encode("utf-8"))
                        handler.wfile.flush()
                except (ValueError, TypeError, UnicodeError):
                    pass
                event_id, event_name, data_lines = "", "", []
    except Exception:
        if not headers_sent:
            body, code = _safe_error("internal_error", 503)
            handler._send(body, code)
    finally:
        try:
            upstream.close()
        except Exception:
            pass


def _seal_payload(data):
    """出站数据隐私封存；保持结构不变。"""
    sealed, _meta = guard_jsonable(data)
    return sealed


def _ok(data) -> bytes:
    return json.dumps(
        {"ok": True, "data": _seal_payload(data)}, ensure_ascii=False, default=str
    ).encode("utf-8")


def _contract(data) -> bytes:
    return json.dumps(_seal_payload(data), ensure_ascii=False, default=str).encode("utf-8")


def _err(msg: str, code: int = 400) -> tuple[bytes, int]:
    safe_msg = guard_text(msg).text
    return (
        json.dumps({"ok": False, "error": safe_msg}, ensure_ascii=False).encode("utf-8"),
        code,
    )


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _data_filters(qs: dict) -> dict:
    return {
        "source": qs.get("source"),
        "service": qs.get("service"),
        "category": qs.get("category") or qs.get("category_v2"),
        "time_from": (
            qs.get("time_from")
            or qs.get("start_time")
            or qs.get("from")
            or qs.get("start")
        ),
        "time_to": (
            qs.get("time_to")
            or qs.get("end_time")
            or qs.get("to")
            or qs.get("end")
        ),
        "keyword": qs.get("keyword") or qs.get("q"),
    }


class Handler(BaseHTTPRequestHandler):
    # 静音默认日志(太吵),自己打一行精简的
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[api] {self.command} {self.path} -> {args[1]}\n")

    def _origin_policy_for_request(self) -> dict:
        return _origin_policy(self.headers.get("Origin"), self.headers.get("Host"))

    def _send(self, body: bytes, code: int = 200, ctype: str = "application/json"):
        self.send_response(code)
        # 文本类响应声明 charset;二进制资源(图片/字体等)不追加
        if ctype.startswith("text/") or ctype in {
            "application/json", "application/javascript", "image/svg+xml",
        }:
            ctype = f"{ctype}; charset=utf-8"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 生产同源不需要 CORS header;仅显式开发 Origin 获得专属(非 wildcard)响应头
        decision = self._origin_policy_for_request()
        if decision["cors_origin"]:
            self.send_header("Access-Control-Allow-Origin", decision["cors_origin"])
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        """读 JSON body,空/非法返回 {}。处理编码兼容性。"""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        # 优先 UTF-8,回退系统 locale 编码(兼容 Windows GBK 终端)
        for enc in ("utf-8", "gbk", "gb2312", "gb18030"):
            try:
                text = raw.decode(enc)
                data = json.loads(text)
                return data if isinstance(data, dict) else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        return {}

    def do_OPTIONS(self):  # CORS 预检
        decision = self._origin_policy_for_request()
        if not decision["allowed"]:
            # 未知 Origin 的预检 → 安全拒绝;不下发 CORS header,不回显 Origin
            body, code = _safe_error("origin_not_allowed", 403)
            self._send(body, code)
            return
        self._send(b"", 204)

    # --- GET 路由 ----------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        ctx = {"url": url, "path": path, "qs": qs}

        try:
            # /api/pi/* —— Pi 内核/操作投影(或chestration 域)
            if (path in {"/api/pi/status", "/api/pi/tasks", "/api/pi/operations"}
                    or path.startswith("/api/pi/operations/")
                    or path == "/api/pi/events"):
                orchestration_handlers.handle_get(self, ctx)
                return
            # --- FROZEN 2026-08-11: Cockpit 前端静态托管(/app[/<path>])停用。产品方向调整
            # 为 wiki 统合层,web 不再是核心入口。代码保留不删,请求现在落到下方 404 未知路径。
            # 恢复方式:取消本段注释即可恢复 apps/personal_decision_cockpit/dist 的 SPA 托管。
            # if url.path == "/app" or url.path.startswith("/app/"):
            #     meta_handlers.serve_cockpit_static(self, ctx)
            #     return
            if path == "/health":
                meta_handlers.handle_health(self, ctx)
                return
            if path == "/stats":
                meta_handlers.handle_stats(self, ctx)
                return
            # 999.5 单人评审台(ui 域)
            if path == "/ui/review":
                ui_handlers.handle(self, ctx)
                return
            if path in {
                "/intelligence/state/current", "/intelligence/state/history",
                "/intelligence/changes/recent", "/intelligence/state/explain",
            }:
                intelligence_handlers.handle(self, ctx)
                return
            if path in {
                "/decision/recommendations", "/decision/recommendation",
                "/decision/recommendation/history", "/decision/recommendation/outcomes",
                "/decision/recommendation/effectiveness",
            }:
                decision_handlers.handle(self, ctx)
                return
            if path in {
                "/proactive/inbox", "/proactive/digest", "/proactive/candidate",
                "/proactive/candidate/explain", "/proactive/controls/status",
                "/proactive/metrics",
            }:
                proactive_handlers.handle(self, ctx)
                return
            if path in {
                "/agent/external", "/agent/external/item", "/agent/external/explain",
                "/agent/analysis", "/agent/analysis/item", "/agent/analysis/explain",
                "/agent/pilot", "/agent/pilot/item", "/agent/pilot/explain",
                "/agent/calibration", "/agent/calibration/item", "/agent/calibration/explain",
                "/agent/session/resume", "/agent/session/explain",
            }:
                agent_handlers.handle(self, ctx)
                return
            # --- FROZEN 2026-08-11: Cockpit 专用只读投影路由停用。产品方向调整为 wiki 统合层,
            # web 不再是核心入口。代码保留不删,请求现在落到下方 404 未知路径。
            # 停用列表(10 条):/ui/overview /ui/system/status /ui/personal-state
            #   /ui/external/delta /ui/decision-queue /ui/decision/workspace /ui/actions/recent
            #   /ui/proactive/summary /ui/calibration/overview /ui/evidence/resolve
            # 恢复方式:取消本段注释即可恢复。wiki 接口(/ui/topics /ui/topic
            #   /ui/topic/backlinks /ui/topic/resolve)保持可用,见下方独立分发段。
            # if path in {
            #     "/ui/overview", "/ui/system/status", "/ui/personal-state",
            #     "/ui/external/delta", "/ui/decision-queue", "/ui/decision/workspace",
            #     "/ui/actions/recent", "/ui/proactive/summary", "/ui/calibration/overview",
            #     "/ui/evidence/resolve",
            # }:
            #     ui_handlers.handle(self, ctx)
            #     return
            if path in {"/ui/topics", "/ui/topic", "/ui/topic/backlinks", "/ui/topic/resolve"}:
                topic_handlers.handle(self, ctx)
                return
            if path in ("/knowledge", "/knowledge/status"):
                meta_handlers.handle_knowledge_status(self, ctx)
                return
            if (path.startswith("/google/assertions")
                    or path == "/categories"
                    or path.startswith("/data/")
                    or path == "/memory" or path.startswith("/memory/")
                    or path == "/profile" or path.startswith("/event/")):
                data_handlers.handle_get(self, ctx)
                return

            body, code = _err(f"未知路径: {path}", 404)
            self._send(body, code)

        except ValueError as e:
            body, code = _err(str(e), 400)
            self._send(body, code)
        except Exception:
            # 详细异常留本地 stderr;公开响应只回安全 code/message,不拼接 str(exc)
            traceback.print_exc()
            body, code = _safe_error("internal_error", 500)
            self._send(body, code)
    # --- POST 路由 ---------------------------------------------------------
    def do_POST(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"

        try:
            if path in {"/ui/topics", "/ui/topic", "/ui/topic/backlinks", "/ui/topic/resolve"}:
                self._send(
                    json.dumps({"ok": False, "error": {"code": "method_not_allowed"}}, ensure_ascii=False).encode("utf-8"),
                    405,
                )
                return
            if path in SESSION_WRITE_ROUTES:
                # Origin gate 必须先于 body 解析与 orchestration 委派(D-36-03):
                # 不匹配 Origin 时零解析、零委派、零写入。
                decision = self._origin_policy_for_request()
                if not decision["allowed"]:
                    body, code = _safe_error("origin_not_allowed", 403)
                    self._send(body, code)
                    return

            # 999.5 评审 labels 与 session 写路由同规则(D-36-03):
            # Origin gate 先于 body 解析,不匹配 Origin 时零解析、零写入。
            if path == "/ui/review/labels":
                decision = self._origin_policy_for_request()
                if not decision["allowed"]:
                    body, code = _safe_error("origin_not_allowed", 403)
                    self._send(body, code)
                    return

            body = self._read_body()
            ctx = {"url": url, "path": path, "qs": {}, "body": body}

            # /api/pi/* 与 /internal/pi-domain/dispatch 与 /agent/session/* 写路由(orchestration 域)
            if (path in {"/api/pi/cancel", "/api/pi/resume"}
                    or path.startswith("/api/pi/operations/")
                    or path == "/internal/pi-domain/dispatch"
                    or path in SESSION_WRITE_ROUTES):
                orchestration_handlers.handle_post(self, ctx)
                return
            if path == "/ui/review/labels":
                ui_handlers.handle_review_labels(self, ctx)
                return
            if path == "/search/semantic":
                data_handlers.handle_search_semantic(self, ctx)
                return
            if path == "/search/cards":
                data_handlers.handle_search_cards(self, ctx)
                return
            if path == "/search/query":
                data_handlers.handle_search_query(self, ctx)
                return

            body_b, code = _err(f"未知路径: {path}", 404)
            self._send(body_b, code)

        except Exception:
            # 详细异常留本地 stderr;公开响应只回安全 code/message,不拼接 str(exc)
            traceback.print_exc()
            body_b, code = _safe_error("internal_error", 500)
            self._send(body_b, code)
    def _wiki_method_not_allowed(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in {"/ui/topics", "/ui/topic", "/ui/topic/backlinks", "/ui/topic/resolve"}:
            payload = {"ok": False, "error": {"code": "method_not_allowed"}}
            self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"), 405)
            return True
        return False

    def do_PUT(self):
        if not self._wiki_method_not_allowed():
            self._send(*_safe_error("internal_error", 405))

    def do_PATCH(self):
        if not self._wiki_method_not_allowed():
            self._send(*_safe_error("internal_error", 405))

    def do_DELETE(self):
        if not self._wiki_method_not_allowed():
            self._send(*_safe_error("internal_error", 405))

def main() -> None:
    p = argparse.ArgumentParser(description="个人数据系统 REST API")
    p.add_argument("--host", default="127.0.0.1", help="监听地址(默认 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000, help="监听端口(默认 8000)")
    args = p.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[api] 个人数据 REST API 启动:")
    print(f"[api]   http://{args.host}:{args.port}")
    print(f"[api] 接口:")
    print(f"[api]   GET  /health               健康检查(+knowledge 摘要)")
    print(f"[api]   GET  /stats                数据库+向量库+知识索引统计")
    print(f"[api]   GET  /knowledge            知识索引状态(?no_chroma=1)")
    print(f"[api]   GET  /categories           分类分布(?source=可选)")
    print(f"[api]   GET  /memory               长期记忆概览(?type=可选)")
    print(f"[api]   GET  /memory/<subject>     单条记忆详情(+?neighbors=N)")
    print(f"[api]   POST /search/semantic      语义检索(knowledge-first)")
    print(f"[api]   POST /search/cards         MVP 会话卡检索(semantic_cards 只读)")
    print(f"[api]   POST /search/query         精确查询(sqlite)")
    print(f"[api]   GET  /event/<id>           单条事件详情")
    print(f"[api]   GET  /profile              AI 长期上下文文档(RAG 注入)")
    print(f"[api]   GET  /intelligence/*       个人状态/变化只读接口")
    print(f"[api]   GET  /data/*               分页/导出/聚合/时间线/质量")
    print(f"[api] Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[api] 已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
