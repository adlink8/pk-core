"""Cockpit UI projection 共享基础设施(拆分自 services/ui_projection.py,OC-5)。

包含:统一信封构造、安全 limitation/error 文案、只读连接/探活基础工具、
权威库路径表与各 projection 共用常量。被 projection 包内 10 个投影模块引用。
"""
from __future__ import annotations

import socket
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from personal_knowledge.core.project_paths import EXTERNAL_CONTEXT_DB, VAR_DB


INTERFACE_SCHEMA_VERSION = "decision_cockpit_projection_v1"

AUTHORITY_DB_PATHS = {
    "external_context": EXTERNAL_CONTEXT_DB,
    "decision_analysis": VAR_DB / "decision_analysis.sqlite",
    "project_pilot": VAR_DB / "project_pilot.sqlite",
    "recommendation_calibration": VAR_DB / "recommendation_calibration.sqlite",
}

_PORT_PROBES = {"mcp": 8789, "tunnel": 8081}
_REST_PORT = 8000
# projection/ 在 services/ 下一层,需多上跳一级到项目根
_SUPERVISOR_STATE_PATH = (
    Path(__file__).resolve().parents[4] / "ops" / "state" / "agent-stack.json"
)
_SUPERVISOR_STALE_AFTER = timedelta(minutes=15)

# decision_queue.get / decision_workspace.get 共用上限
_QUEUE_LIMIT = 100


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    """解析带时区的 ISO 时间;缺失 / 无法解析 / 无时区一律返回 None。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _port_up(port: int) -> bool:
    """TCP 探活:只报 up/down,不发任何 payload,异常即 down。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


# === 安全公开 limitation/error 目录(Phase 36:D-36-06) =========================
#
# 单权威/单条读取失败时,公开 limitations 与内联 "error" 字段只能引用这里的固定
# code/message,绝不拼接 str(exc)、异常类型、路径、密钥、provider body 或
# confirmation/HMAC 材料;详细异常仅用于该异常自身触发的服务端行为(如重新抛出
# 给上层隔离),不得以任何形式序列化进返回给浏览器的 JSON。
_SAFE_FAILURE_CODES: dict[str, str] = {
    "authority_read_failed": "读取失败",
    "history_count_unavailable": "历史计数读取异常",
    "item_assembly_failed": "全链组装失败",
    "protocol_explain_failed": "explain 失败",
}


def _safe_failure_message(name: str, code: str) -> str:
    """构造 allowlisted 安全 limitation 文案;code 只能是模块内固定字面量,不接受
    str(exc) 或其它不可信输入。"""
    return f"{name} {_SAFE_FAILURE_CODES[code]}({code})"


def _safe_failure_error(code: str) -> dict[str, str]:
    """构造 allowlisted 安全内联 error 字段(actions_recent/calibration 单条失败用)。"""
    return {"code": code, "message": _SAFE_FAILURE_CODES[code]}


# state.current / changes.recent 等 IntelligenceService 读操作在“当前 active
# snapshot 尚无已提交 personal_state run”时返回 ok=False + error.code=="run_missing"。
# 这是权威自身发布的真实空状态语义(尚未产出个人状态分析),不是读取异常,必须映射
# 为 empty 而非 error,否则会把“还没跑分析”误报成“读取失败”掩盖真实降级原因。
_INTELLIGENCE_EMPTY_CODES = frozenset({"run_missing"})


def _intelligence_data_or_raise(
    result: dict[str, Any], fallback_code: str,
) -> dict[str, Any] | None:
    """校验 IntelligenceService 读操作结果:成功返回 data;真实空状态(run_missing)
    返回 None 交调用方降级为零值 empty 分区;其余失败 raise ValueError(safe code)
    交 _collect/单条 try 隔离为 error(safe code 来自权威自身的固定错误词表,
    不是 str(exc),因此可安全传播)。"""
    if result.get("ok"):
        return result["data"]
    code = str((result.get("error") or {}).get("code") or fallback_code)
    if code in _INTELLIGENCE_EMPTY_CODES:
        return None
    raise ValueError(code)


def _db_readable(path: Path) -> bool:
    """以 mode=ro 打开并执行 SELECT 1 验证可读性。"""
    if not path.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            con.execute("PRAGMA query_only=ON")
            con.execute("SELECT 1")
        finally:
            con.close()
        return True
    except (OSError, sqlite3.Error):
        return False


def _error(operation: str, code: str, detail: str = "") -> dict[str, Any]:
    return {
        "schema_version": INTERFACE_SCHEMA_VERSION,
        "operation": operation,
        "ok": False,
        "error": {"code": code, "detail": detail},
    }


def _envelope(
    operation: str,
    generated_at: str,
    sections: dict[str, Any],
    authorities: dict[str, str],
    limitations: list[str],
    snapshot_bindings: dict[str, Any],
    freshness: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": INTERFACE_SCHEMA_VERSION,
        "operation": operation,
        "ok": True,
        "generated_at": generated_at,
        "snapshot_bindings": snapshot_bindings,
        "freshness": freshness,
        "authorities": authorities,
        "partial": any(status == "error" for status in authorities.values()),
        "limitations": limitations,
        "data": sections,
    }


def _collect(
    loaders: dict[str, Callable[[], dict[str, Any]]],
    limitations: list[str],
    is_empty: Callable[[str, dict[str, Any]], bool],
) -> tuple[dict[str, Any], dict[str, str]]:
    sections: dict[str, Any] = {}
    authorities: dict[str, str] = {}
    for name, loader in loaders.items():
        try:
            section = loader()
        except Exception:  # noqa: BLE001 — 单节失败必须被隔离,详情不进入公开响应
            sections[name] = None
            authorities[name] = "error"
            limitations.append(_safe_failure_message(name, "authority_read_failed"))
            continue
        sections[name] = section
        authorities[name] = "empty" if is_empty(name, section) else "ok"
    return sections, authorities
