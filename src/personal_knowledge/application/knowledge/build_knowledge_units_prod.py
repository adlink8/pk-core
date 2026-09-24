"""Phase 14 Plan 02 Task 2：可恢复 batch + 分类 retry + 内容寻址 cache。

production backfill engine，支持 --start / --resume、item ledger 状态机、
内容寻址 response cache、分类 retry（401 刷新 token 后重试，429/500/503 重试，400/403/404 fail-fast）。

核心契约（RESEARCH）：
  - 新建 run 与恢复 run 分成两个显式入口
  - resume 不调用 begin_staging()（不删除已完成结果）
  - cache hit 仍重跑当前 Pydantic/evidence gate
    （evidence_quote 对不上原文的 unit 丢弃并计入 units_dropped_no_evidence）
  - SQLite 单 writer：worker 只返回纯响应，主线程提交
  - model 从 CLI/config 注入，写 manifest，不可用则 abort
  - 多线程并行：ThreadPoolExecutor 并发 LLM，主线程串行写库

用法::

    # 新 run
    python build_knowledge_units_prod.py --start --inventory <id> --model gemini-3.5-flash-lite --limit 50

    # 恢复（默认 8 路并发）
    python build_knowledge_units_prod.py --resume <run_id> --model gemini-3.5-flash-lite --workers 8

    # 检查状态
    python build_knowledge_units_prod.py --status <run_id>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sqlite3
import sys
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.core.sqlite import connect_rw
from personal_knowledge.core.providers import TokenProvider  # noqa: F401  (canonical home; OC-10)
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import UNIFIED_DB, AGENT_CONVERSATIONS_DB  # noqa: E402
from personal_knowledge.core.runtime_config import (  # noqa: E402
    vertex_config,
    vertex_generate_content_url,
    vertex_generation_config,
)
from personal_knowledge.application.knowledge.knowledge_unit_pipeline import RunManifest  # noqa: E402
from personal_knowledge.application.knowledge.confidence import derive_confidence  # noqa: E402
from personal_knowledge.application.knowledge.confirmation_signals import (  # noqa: E402
    detect_confirmation_signal,
)
from personal_knowledge.application.knowledge.build_knowledge_units import (  # noqa: E402
    KnowledgeUnit, ExtractionResult, strip_system_injections, is_meaningful,
    _clean_json, PROMPT_PATH, PROMPT_VERSION,
    V2_PROMPT_PATH, V2_PROMPT_VERSION,
    AssistantExtractionResult, ASSISTANT_PROMPT_PATH, ASSISTANT_PROMPT_VERSION,
    V2_ASSISTANT_PROMPT_PATH, V2_ASSISTANT_PROMPT_VERSION,
)
from personal_knowledge.application.knowledge.injection_context import (
    INJECTION_ANSWER_CHARS,
    INJECTION_MAX_UNITS,
    SubjectIndex,
    format_injection_block,
    scan_subject_occurrences,
    validate_duplicate_of,
)
from personal_knowledge.application.knowledge.state_subjects import load_state_subjects, match_state_subject
from personal_knowledge.core.providers import (  # noqa: E402
    PiKernelProvider, ProviderError, ProviderRequest, ProviderTimeout,
)

# === Phase 41：run 级单轨双轨引擎（D-01/D-02）===

# 单条消息尾部硬截上限（双轨对称；对齐 L2 MAX_WINDOW_CHARS。
# 48000 依据：实测 >12k 真实候选 p90≈52k，覆盖 ~94%；Phase 41 CONTEXT deferred）
MESSAGE_MAX_CHARS = 48000


@dataclass(frozen=True)
class TrackConfig:
    """一条抽取轨的全部参数（run 级单轨：一个 run 只属于一条轨）。

    R2 硬约束：unit_id_prefix 必须恰好 3 字符且以 '|' 结尾——
    StagingPublisher.promote 取 substr(unit_id,1,3) 作 pass 族，
    构造期 fail closed。
    """

    name: str  # "user" / "assistant"
    prompt_path: Path
    prompt_version: str
    role_label: str  # 注入 LLM 包装文案（证据段标签）
    evidence_scope: str
    unit_id_prefix: str
    result_model: type  # ExtractionResult / AssistantExtractionResult
    roles: tuple[str, ...]

    def __post_init__(self) -> None:
        assert len(self.unit_id_prefix) == 3 and self.unit_id_prefix.endswith("|"), (
            f"unit_id_prefix must be exactly 3 chars ending with '|' "
            f"(StagingPublisher pass-family contract), got {self.unit_id_prefix!r}"
        )


USER_TRACK = TrackConfig(
    name="user",
    prompt_path=PROMPT_PATH,
    prompt_version=PROMPT_VERSION,
    role_label="用户对话证据（role=user）：",
    evidence_scope="user",
    unit_id_prefix="v1|",
    result_model=ExtractionResult,
    roles=("user",),
)
ASSISTANT_TRACK = TrackConfig(
    name="assistant",
    prompt_path=ASSISTANT_PROMPT_PATH,
    prompt_version=ASSISTANT_PROMPT_VERSION,
    role_label="助手回答证据（role=assistant）：",
    evidence_scope="assistant",
    unit_id_prefix="as|",
    result_model=AssistantExtractionResult,
    roles=("assistant",),
)
V2_USER_TRACK = TrackConfig(
    name="user", prompt_path=V2_PROMPT_PATH, prompt_version=V2_PROMPT_VERSION,
    role_label="用户对话证据（role=user）：", evidence_scope="user",
    unit_id_prefix="v1|", result_model=ExtractionResult, roles=("user",),
)
V2_ASSISTANT_TRACK = TrackConfig(
    name="assistant", prompt_path=V2_ASSISTANT_PROMPT_PATH,
    prompt_version=V2_ASSISTANT_PROMPT_VERSION,
    role_label="助手回答证据（role=assistant）：", evidence_scope="assistant",
    unit_id_prefix="as|", result_model=AssistantExtractionResult, roles=("assistant",),
)


def track_for_prompt_version(prompt_version: str) -> TrackConfig:
    """按 run manifest 的 prompt_version 反查 TrackConfig（extract 唯一轨来源）。"""
    tracks = {
        ASSISTANT_PROMPT_VERSION: ASSISTANT_TRACK,
        V2_PROMPT_VERSION: V2_USER_TRACK,
        V2_ASSISTANT_PROMPT_VERSION: V2_ASSISTANT_TRACK,
    }
    return tracks.get(prompt_version or "", USER_TRACK)

_VERTEX = vertex_config()
GCP_PROJECT = _VERTEX.project
DEFAULT_WORKERS = 4
# 跨线程全局起步间隔：默认约 20 RPM（与原串行 sleep(3) 同量级，避免 429）
DEFAULT_MIN_REQUEST_INTERVAL = 3.0
# 同 item 累计 attempt 超过此值后，retryable 升格 terminal，避免死循环
MAX_ITEM_ATTEMPTS = 6

# === 错误分类 ===

# 401 归 retryable：gcloud token 约 1h 过期，刷新后可重试（call_llm_with_retry 内 refresh）
RETRYABLE_STATUS = {401, 429, 500, 502, 503}
TERMINAL_STATUS = {400, 403, 404}


def classify_error(status: int | None, exc: Exception | None) -> str:
    """分类 HTTP 错误为 retryable / terminal。401 归 retryable（token 过期，刷新后重试）。"""
    if status and status in RETRYABLE_STATUS:
        return "retryable"
    if status and status in TERMINAL_STATUS:
        return "terminal"
    if isinstance(exc, urllib.error.HTTPError) and exc.code in RETRYABLE_STATUS:
        return "retryable"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "retryable"
    return "terminal"


# === Cache ===

def compute_cache_key(model: str, prompt_hash: str, schema_hash: str,
                      input_hash: str, config_hash: str) -> str:
    """内容寻址 cache key。"""
    payload = f"{model}|{prompt_hash}|{schema_hash}|{input_hash}|{config_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def get_cached_response(con: sqlite3.Connection, cache_key: str) -> str | None:
    """从 cache 读原始 LLM 响应。"""
    row = con.execute(
        "SELECT response_text FROM knowledge_response_cache WHERE cache_key=?",
        (cache_key,),
    ).fetchone()
    return row[0] if row else None


def put_cached_response(con: sqlite3.Connection, cache_key: str, model: str,
                        prompt_hash: str, schema_hash: str, input_hash: str,
                        config_hash: str, response_text: str, response_hash: str,
                        run_id: str, tokens_total: int = 0) -> None:
    """写 cache。"""
    con.execute(
        "INSERT OR REPLACE INTO knowledge_response_cache VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?)",
        (cache_key, run_id, model, prompt_hash, schema_hash, input_hash,
         config_hash, response_text, response_hash,
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         tokens_total),
    )


# === LLM 调用（分类 retry + token refresh）===


class RequestRateLimiter:
    """跨线程最小请求间隔限速，支持 429 自适应减速。"""

    def __init__(self, min_interval: float) -> None:
        self._base_interval = max(0.0, float(min_interval))
        self._min_interval = self._base_interval
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._consecutive_429 = 0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next_at - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_at = now + self._min_interval

    def note_success(self) -> None:
        with self._lock:
            self._consecutive_429 = 0
            # 缓慢回到 base interval
            self._min_interval = max(
                self._base_interval,
                self._min_interval * 0.9,
            )

    def note_429(self) -> None:
        with self._lock:
            self._consecutive_429 += 1
            # 最多扩到 base * 4（默认 12s ≈ 5 RPM）
            self._min_interval = min(
                self._base_interval * 4.0,
                max(self._base_interval, self._min_interval * 1.5),
            )
            # 立刻插入冷却，避免雪崩
            cooldown = min(30.0, 3.0 * self._consecutive_429)
            self._next_at = max(self._next_at, time.monotonic() + cooldown)


def call_llm_with_retry(
    system_prompt: str,
    user_content: str,
    model: str,
    token_provider: TokenProvider,
    max_retries: int = 4,
    base_backoff: float = 2.0,
    max_backoff: float = 60.0,
    rate_limiter: RequestRateLimiter | None = None,
    role_label: str = "用户对话证据（role=user）：",
) -> dict:
    """调用受控模型边界，分类 retry。

    Supervisor 运行的生产路径由 Pi Kernel 接管；Vertex 代码保留为显式
    rollback/test seam，便于既有单元测试和回滚适配器复用。
    """
    if os.environ.get("PI_KERNEL_LEGACY_MODE", "").strip() != "1" and (
        os.environ.get("PI_KERNEL_AI_WORKFLOW", "").strip() == "1"
        or isinstance(token_provider, TokenProvider)
    ):
        return _call_pi_kernel_with_receipt(
            system_prompt, user_content, max_output_tokens=2048,
            timeout_seconds=120.0,
            role_label=role_label,
        )
    url = vertex_generate_content_url(_VERTEX, model)
    user_text = f"{system_prompt}\n\n---\n{role_label}\n{user_content}\n\n---\n请提取知识单元，输出JSON："
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": vertex_generation_config(model, 2048),
    }).encode()

    for attempt in range(max_retries + 1):
        if rate_limiter is not None:
            rate_limiter.wait()
        token = token_provider.get()
        req = urllib.request.Request(url, data=body, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=120)
            data = json.loads(resp.read().decode())
            # extract text
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if p.get("text") and not p.get("thought"))
            usage = data.get("usageMetadata", {})
            if rate_limiter is not None:
                rate_limiter.note_success()
            return {"text": text, "usage": usage}
        except urllib.error.HTTPError as e:
            error_class = classify_error(e.code, e)
            if e.code == 429 and rate_limiter is not None:
                rate_limiter.note_429()
            if error_class == "retryable" and attempt < max_retries:
                # Retry-After + jitter
                retry_after = e.headers.get("Retry-After")
                if retry_after:
                    wait = min(float(retry_after), max_backoff)
                else:
                    wait = min(base_backoff * (2 ** attempt), max_backoff)
                wait += int(hashlib.sha256(f"{attempt}".encode()).hexdigest()[:2], 16) % 16  # jitter 0-15
                time.sleep(float(wait))
                if e.code == 401:
                    token_provider.refresh()
                continue
            return {"error": f"HTTP {e.code}", "error_class": error_class,
                    "detail": e.read().decode()[:200], "attempts": attempt + 1}
        except (TimeoutError, ConnectionError) as e:
            if attempt < max_retries:
                wait = min(base_backoff * (2 ** attempt), max_backoff)
                time.sleep(wait)
                continue
            return {"error": str(e), "error_class": "retryable", "attempts": attempt + 1}
        except Exception as e:
            error_class = classify_error(None, e)
            if error_class == "retryable" and attempt < max_retries:
                wait = min(base_backoff * (2 ** attempt), max_backoff)
                wait += int(hashlib.sha256(f"{attempt}".encode()).hexdigest()[:2], 16) % 16
                time.sleep(float(wait))
                continue
            return {"error": f"{type(e).__name__}: {str(e)[:150]}", "error_class": error_class, "attempts": attempt + 1}
    return {"error": "max retries exceeded", "error_class": "retryable", "attempts": max_retries + 1}


def _call_pi_kernel_with_receipt(
    system_prompt: str,
    user_content: str,
    *,
    max_output_tokens: int,
    timeout_seconds: float,
    role_label: str,
) -> dict:
    """One Pi task for extraction; no local retry after an unknown outcome."""
    user_text = f"{system_prompt}\n\n---\n{role_label}\n{user_content}\n\n---\n请提取知识单元，输出JSON："
    request_checksum = hashlib.sha256(user_text.encode("utf-8")).hexdigest()
    provider = PiKernelProvider(purpose="extraction_summary", timeout_seconds=timeout_seconds)
    try:
        result = provider.generate(ProviderRequest(
            prompt=user_text, request_checksum=request_checksum, temperature=0,
            max_output_tokens=min(max_output_tokens, 4096), timeout_seconds=min(timeout_seconds, 120.0),
        ))
        raw_text = json.dumps(dict(result.response_payload), ensure_ascii=False, separators=(",", ":"))
        candidate_id = f"pi_ku_{request_checksum[:24]}"
        provider.stage_candidate(
            candidate_id=candidate_id,
            proposal={
                "kind": "knowledge_extraction",
                "status": "pending_validation",
                "input_checksum": request_checksum,
                "response_checksum": result.response_checksum,
            },
            evidence_refs=[{
                "ref": f"artifact:{hashlib.sha256(user_content.encode('utf-8')).hexdigest()[:32]}",
                "checksum": hashlib.sha256(user_content.encode("utf-8")).hexdigest(),
            }],
            candidate_checksum=result.response_checksum,
            run_checksum=hashlib.sha256(f"{request_checksum}:{result.response_checksum}".encode()).hexdigest(),
        )
        return {
            "text": raw_text,
            "usage": {
                "promptTokenCount": result.telemetry.input_tokens,
                "candidatesTokenCount": result.telemetry.output_tokens,
            },
            "pi_receipt": dict(provider.last_receipt or {}),
        }
    except ProviderTimeout:
        return {"error": "provider outcome unknown", "error_class": "terminal", "attempts": 1}
    except ProviderError as exc:
        return {"error": "pi kernel task failed", "error_class": "terminal", "attempts": 1, "code": exc.code}


# === Item ledger 状态机 ===

def init_run_items(con: sqlite3.Connection, run_id: str, inventory_id: str) -> int:
    """为 run 的每个 inventory item 创建 pending work-item。返回 item 数。"""
    items = con.execute(
        "SELECT position, evidence_ref FROM knowledge_inventory_items "
        "WHERE inventory_id=? ORDER BY position",
        (inventory_id,),
    ).fetchall()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for pos, ref in items:
        con.execute(
            "INSERT OR IGNORE INTO knowledge_run_items "
            "(run_id, inventory_id, position, evidence_ref, status, attempt_count, "
            "lease_started_at, last_error_class, cache_key, response_hash, unit_count, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, inventory_id, pos, ref, "pending", 0, None, None, None, None, 0, now),
        )
    con.commit()
    return len(items)


def recover_expired_leases(
    con: sqlite3.Connection,
    run_id: str,
    lease_timeout: float = 600,
    force_all: bool = False,
) -> int:
    """恢复过期 in_flight lease 为 retryable。返回恢复数。

    force_all=True 时回收本 run 全部 in_flight（单 writer resume 场景）。
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff = datetime.now(timezone.utc).timestamp() - lease_timeout
    rows = con.execute(
        "SELECT id, lease_started_at FROM knowledge_run_items "
        "WHERE run_id=? AND status='in_flight'",
        (run_id,),
    ).fetchall()
    recovered = 0
    for item_id, lease_iso in rows:
        should_recover = force_all
        if not should_recover and lease_iso:
            try:
                lease_ts = datetime.fromisoformat(lease_iso.replace("Z", "+00:00")).timestamp()
                should_recover = lease_ts < cutoff
            except ValueError:
                should_recover = True
        elif not should_recover and not lease_iso:
            should_recover = True
        if should_recover:
            con.execute(
                "UPDATE knowledge_run_items SET status='retryable', updated_at=? WHERE id=?",
                (now_iso, item_id),
            )
            recovered += 1
    con.commit()
    return recovered


def get_pending_items(con: sqlite3.Connection, run_id: str, batch_size: int = 50) -> list[dict]:
    """获取 pending + retryable 的 items。

    优先低 attempt_count，避免 429 后反复抽同一批高失败项。
    """
    rows = con.execute(
        "SELECT id, position, evidence_ref, status, attempt_count "
        "FROM knowledge_run_items WHERE run_id=? AND status IN ('pending','retryable') "
        "ORDER BY attempt_count ASC, position ASC LIMIT ?",
        (run_id, batch_size),
    ).fetchall()
    return [{"row_id": r[0], "position": r[1], "evidence_ref": r[2],
             "status": r[3], "attempt_count": r[4]} for r in rows]


def _claim_item(con: sqlite3.Connection, row_id: int, now: str) -> bool:
    """test-and-set 认领 item：仅 pending/retryable 可被 claim。

    并发进程已 claim（in_flight/succeeded/...）时 UPDATE 不命中，
    返回 False，调用方应跳过该 item（Finding F-10）。
    """
    cur = con.execute(
        "UPDATE knowledge_run_items SET status='in_flight', "
        "lease_started_at=?, attempt_count=attempt_count+1, updated_at=? "
        "WHERE id=? AND status IN ('pending','retryable')",
        (now, now, row_id),
    )
    con.commit()
    return cur.rowcount > 0


def get_run_stats(con: sqlite3.Connection, run_id: str) -> dict:
    """run 的 item 状态统计。"""
    counts = {}
    for row in con.execute(
        "SELECT status, COUNT(*) FROM knowledge_run_items WHERE run_id=? GROUP BY status",
        (run_id,),
    ):
        counts[row[0]] = row[1]
    return counts


# === 主引擎 ===

def start_run(
    model: str,
    inventory_id: str,
    db_path: Path = UNIFIED_DB,
    limit: int | None = None,
    batch_size: int = 50,
    pilot_positions: list[int] | None = None,
    prompt_version: str | None = None,
) -> str:
    """启动新 production extraction run。返回 run_id。

    pilot_positions: 如果提供，只处理这些 position 的 items，其余标记 abstained。
    """
    con = connect_rw(db_path)

    # 读 inventory
    inv = con.execute(
        "SELECT dataset_hash, source_checksum, item_count FROM knowledge_inventory WHERE inventory_id=?",
        (inventory_id,),
    ).fetchone()
    if not inv:
        raise ValueError(f"inventory 不存在: {inventory_id}")
    dataset_hash, source_checksum, item_count = inv

    track = track_for_prompt_version(prompt_version or PROMPT_VERSION)
    if track.prompt_version.startswith("v2"):
        pending_elsewhere = con.execute(
            "SELECT status, COUNT(*) FROM knowledge_run_items "
            "WHERE status IN ('pending','in_flight','retryable') GROUP BY status"
        ).fetchall()
        if pending_elsewhere:
            counts = {row[0]: row[1] for row in pending_elsewhere}
            con.close()
            raise ValueError(
                f"refuse v2 prompt switch while work is active: {counts}"
            )

    prompt_text = track.prompt_path.read_text(encoding="utf-8")
    prompt_hash = hashlib.sha256(prompt_text.encode()).hexdigest()[:16]
    schema_hash = f"{track.prompt_version}_extra_forbid"
    config = {"batch_size": batch_size, "limit": limit,
              "pilot_positions": pilot_positions is not None,
              "injection": {
                  "top_k": INJECTION_MAX_UNITS,
                  "answer_chars": INJECTION_ANSWER_CHARS,
                  "scan_min_chars": 4,
              } if track.prompt_version.startswith("v2") else None}
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]

    manifest = RunManifest.create(
        run_type="extraction",
        source_build_id=source_checksum,
        input_data={"inventory_id": inventory_id, "dataset_hash": dataset_hash},
        prompt_version=track.prompt_version,
        model=model,
        config=config,
    )

    # 写 manifest（status=staging）
    con.execute(
        "INSERT OR REPLACE INTO knowledge_build_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (manifest.run_id, "extraction", manifest.generated_at, source_checksum,
         manifest.input_hash, track.prompt_version, "v1", model, "", config_hash,
         "", "", "staging", "", ""),
    )

    # init item ledger
    init_run_items(con, manifest.run_id, inventory_id)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if pilot_positions is not None:
        # pilot 模式：只保留 pilot positions 为 pending，其余 abstained
        pilot_set = set(pilot_positions)
        con.execute(
            "UPDATE knowledge_run_items SET status='abstained', updated_at=? "
            "WHERE run_id=?",
            (now, manifest.run_id),
        )
        for pos in pilot_positions:
            con.execute(
                "UPDATE knowledge_run_items SET status='pending', updated_at=? "
                "WHERE run_id=? AND position=?",
                (now, manifest.run_id, pos),
            )
    elif limit and limit < item_count:
        # limit 模式：超出范围的 abstained
        con.execute(
            "UPDATE knowledge_run_items SET status='abstained', updated_at=? "
            "WHERE run_id=? AND position >= ?",
            (now, manifest.run_id, limit),
        )

    con.commit()
    con.close()
    return manifest.run_id


def resume_run(run_id: str, model: str, db_path: Path = UNIFIED_DB,
               batch_size: int = 50) -> dict:
    """恢复 production extraction run。不调用 begin_staging（不删除旧结果）。"""
    con = connect_rw(db_path)

    # 验证 run 存在
    run = con.execute(
        "SELECT status, input_hash FROM knowledge_build_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if not run:
        raise ValueError(f"run 不存在: {run_id}")

    # 单 writer：resume 时强制回收全部 in_flight（进程中断后可继续）
    recovered = recover_expired_leases(con, run_id, force_all=True)
    stats = get_run_stats(con, run_id)

    con.close()
    return {"run_id": run_id, "recovered_leases": recovered, "stats": stats}


def _resolve_run_track(run_id: str, db_path: Path) -> TrackConfig:
    """按 run manifest 的 prompt_version 反查 TrackConfig（查不到回退 user 轨）。"""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT prompt_version FROM knowledge_build_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        con.close()
    return track_for_prompt_version(row[0] if row else "")


def _evidence_supported(quote: str, source: str) -> bool:
    """quote 是否有 ≥10 字连续片段出现在 source 中（与 L2 同规则）。"""
    if not quote or not source:
        return False
    if quote in source:
        return True
    q = quote.strip()
    if len(q) < 10:
        return q in source
    # any contiguous 10-char window of quote in source
    for i in range(0, max(1, len(q) - 9)):
        frag = q[i : i + 10]
        if frag in source:
            return True
    return False


_INVALID_JSON_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu])')


def _tolerant_parse(track, raw_text: str):
    """schema 抢救解析:严格校验失败后的第二次机会,不改变严格路径语义。

    ① json.loads 失败 → 修复非法反斜杠转义(Windows 路径等)后重试;
    ② 整体 Pydantic 失败 → 逐 unit 校验:剥掉 unit 内 extra 字段,
       坏 unit 丢弃(计数),好 unit 保留;
    ③ 一个合法 unit 都没有且非 abstain → (None, 0),维持 schema_invalid。

    返回 (result | None, dropped_unit_count)。抢救出的 unit 仍走下游
    evidence gate(_evidence_supported),不降低证据标准。
    """
    cleaned = _clean_json(raw_text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        try:
            parsed = json.loads(_INVALID_JSON_ESCAPE.sub(r"\\\\", cleaned))
        except json.JSONDecodeError:
            return None, 0
    if not isinstance(parsed, dict):
        return None, 0
    try:
        return track.result_model.model_validate(parsed), 0
    except ValidationError:
        pass
    unit_model = track.result_model.model_fields["units"].annotation.__args__[0]
    unit_fields = set(unit_model.model_fields)
    valid_units, dropped = [], 0
    raw_units = parsed.get("units")
    for u in raw_units if isinstance(raw_units, list) else []:
        if not isinstance(u, dict):
            dropped += 1
            continue
        try:
            valid_units.append(unit_model.model_validate(
                {k: v for k, v in u.items() if k in unit_fields}
            ))
        except ValidationError:
            dropped += 1
    abstain = bool(parsed.get("abstain"))
    if not valid_units and not abstain:
        return None, dropped
    result = track.result_model(
        units=valid_units,
        abstain=abstain and not valid_units,
        abstain_reason=str(parsed.get("abstain_reason") or "")[:500],
    )
    return result, dropped


# 短确认形态（<30 字且匹配确认/续跑套话才判确认；短问题如"怎么配代理？"不误伤）
_CONFIRM_RE = re.compile(
    r"^(嗯+|呃+|啊+|好[的嘞呀吧]?|行|可以|是的?|对的?|没错|ok(ay)?|yes|y|"
    r"继续|接着说|continue|go on|收到|明白|懂了|了解|谢谢|thanks?|"
    r"差不多了|就这样|没问题|可以了|同意|赞成|行吧|好吧|嗯嗯)[\s!~。．.!！…]*$",
    re.I,
)


def _is_short_confirmation(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if len(t) >= 30:
        return False
    return bool(_CONFIRM_RE.match(t))


def _load_preceding_user_context(
    canon_con: sqlite3.Connection,
    session_id: str | None,
    anchor_ordinal: int | None,
    max_back: int = 5,
) -> tuple[str, str]:
    """assistant 轨 QA 对（v2）：同 session 向前穿透短确认，找最近实质 user 提问。

    v1 只取紧邻前 1 条 user 消息——对话里"嗯/继续/好的"类短确认会让 QA 对
    挂到无信息量的上下文上。v2 在最多 max_back 条前置 user 消息里跳过
    短确认（<30 字且匹配确认套话），返回最近实质提问。

    仅供 LLM 理解上下文，不作证据（evidence_quote 回查不含此段）。
    返回 (清洗后文本, 消息 ref)；无实质前置 user / ordinal 缺失 → ("", "")。
    """
    if not session_id or anchor_ordinal is None:
        return "", ""
    rows = canon_con.execute(
        "SELECT canonical_message_id, content FROM canonical_messages "
        "WHERE canonical_session_id=? AND role='user' AND ordinal < ? "
        "AND content IS NOT NULL "
        "ORDER BY ordinal DESC LIMIT ?",
        (session_id, anchor_ordinal, max_back),
    ).fetchall()
    for row in rows:
        if _is_short_confirmation(row["content"] or ""):
            continue
        return strip_system_injections(row["content"]), str(row["canonical_message_id"])
    return "", ""


def _commit_item_result(
    con: sqlite3.Connection,
    run_id: str,
    item: dict,
    work: dict,
    model: str,
    prompt_hash: str,
    schema_hash: str,
    config_hash: str,
    stats: dict,
    track: TrackConfig = USER_TRACK,
    state_rules: dict | None = None,
) -> None:
    """主线程：把 worker 结果写入 SQLite（单 writer）。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row_id = item["row_id"]
    cache_key = work["cache_key"]
    input_hash = work["input_hash"]

    if work.get("kind") == "llm_error":
        error_class = work.get("error_class", "terminal")
        error_text = str(work.get("error") or "")
        is_429 = "429" in error_text
        # 429 不计入 attempt 额度：限流是环境问题，不是样本质量
        attempt_count = item.get("attempt_count", 0) + (0 if is_429 else 1)
        if is_429:
            # 回退 attempt_count 增量（claim 时 +1 了），避免 429 烧尽重试次数
            con.execute(
                "UPDATE knowledge_run_items SET attempt_count=MAX(attempt_count-1, 0) WHERE id=?",
                (row_id,),
            )
            new_status = "retryable"
            error_class = "retryable"
            stats["rate_limited"] = stats.get("rate_limited", 0) + 1
        elif error_class == "retryable" and attempt_count < MAX_ITEM_ATTEMPTS:
            new_status = "retryable"
        else:
            new_status = "terminal_failed"
            if error_class == "retryable":
                error_class = "max_attempts"
        con.execute(
            "UPDATE knowledge_run_items SET status=?, last_error_class=?, "
            "cache_key=?, updated_at=? WHERE id=?",
            (new_status, error_class, cache_key, now, row_id),
        )
        con.commit()
        stats["failed"] += 1
        return

    raw_text = work["raw_text"]
    if work.get("write_cache"):
        response_hash = hashlib.sha256(raw_text.encode()).hexdigest()[:32]
        put_cached_response(
            con, cache_key, model, prompt_hash, schema_hash,
            input_hash, config_hash, raw_text, response_hash, run_id,
        )

    try:
        parsed = json.loads(_clean_json(raw_text))
        result = track.result_model.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError):
        # schema 抢救(2026-07-26 实测:schema_invalid 的主因是 ① 多 unit 响应中
        # 第 2 个起缺 question / 带 extra 字段(全有全无校验连坐好 unit),
        # ② Windows 路径反斜杠产生非法 JSON 转义。逐 unit 抢救,救不回才判死。
        result, dropped = _tolerant_parse(track, raw_text)
        if result is None:
            con.execute(
                "UPDATE knowledge_run_items SET status='terminal_failed', "
                "last_error_class='schema_invalid', cache_key=?, updated_at=? WHERE id=?",
                (cache_key, now, row_id),
            )
            con.commit()
            stats["failed"] += 1
            return
        stats["schema_salvaged"] = stats.get("schema_salvaged", 0) + 1
        stats["units_dropped_schema"] = stats.get("units_dropped_schema", 0) + dropped

    # D-03（仅 assistant 轨）：确认信号只做 confidence 修饰，不做硬 gate。
    confirmation_signal = "none"
    if track.name == "assistant":
        confirmation_signal = work.get("confirmation_signal") or "none"
        key = f"confirmation_{confirmation_signal}"
        stats[key] = stats.get(key, 0) + 1

    response_hash = hashlib.sha256(raw_text.encode()).hexdigest()[:32]
    if result.abstain:
        con.execute(
            "UPDATE knowledge_run_items SET status='abstained', cache_key=?, "
            "response_hash=?, unit_count=0, updated_at=? WHERE id=?",
            (cache_key, response_hash, now, row_id),
        )
        stats["abstained"] += 1
    else:
        source = work.get("cleaned", "")
        injected_ids = set(work.get("injected_ids") or ())
        kept = 0
        for ordinal, unit in enumerate(result.units, 1):
            if not _evidence_supported(unit.evidence_quote, source):
                stats["units_dropped_no_evidence"] += 1
                continue
            kept += 1
            ev_ref = item["evidence_ref"]
            unit_id = track.unit_id_prefix + hashlib.sha256(
                f"{run_id}|{ev_ref}|{ordinal}".encode()
            ).hexdigest()[:32]
            duplicate_of = validate_duplicate_of(unit.duplicate_of, injected_ids)
            if unit.duplicate_of and duplicate_of is None:
                stats["invalid_duplicate_of"] = stats.get("invalid_duplicate_of", 0) + 1
            lifecycle = unit.lifecycle if unit.lifecycle in (
                "current", "deprecated", "superseded", "conflict"
            ) else "current"
            state_match = None
            if track.prompt_version.startswith("v2"):
                state_match = match_state_subject(
                    unit.subject,
                    state_rules if state_rules is not None else load_state_subjects(),
                )
            if state_match:
                lifecycle = "candidate"
                stats["units_downgraded_candidate"] = stats.get("units_downgraded_candidate", 0) + 1
            # PDA-41：弃用 LLM 自报置信（95% ≥0.9 无区分度），改证据派生。
            # D-03 修饰（非硬 gate）并入派生：采纳 +0.05，纠正 -0.2；
            # corrected 行是未来 lifecycle supersede 候选，自动路由属 deferred
            # （CONTEXT deferred；docs/runbooks/ku-incremental.md §3F），此处不做。
            # evidence_count 计入 question-side context ref（多证据互证）。
            has_qa_context = bool(track.name == "assistant" and str(work.get("question_ref") or ""))
            confidence = derive_confidence(
                evidence_count=1 + (1 if has_qa_context else 0),
                evidence_scope=track.evidence_scope,
                evidence_quote=unit.evidence_quote,
                confirmation_signal=confirmation_signal if track.name == "assistant" else "none",
            )
            con.execute(
                "INSERT OR REPLACE INTO knowledge_units "
                "(unit_id, run_id, unit_type, subject, question, answer, confidence, "
                "evidence_quote, lifecycle, source_session_id, source_message_ref, "
                "source_agent, evidence_scope, status, version, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (unit_id, run_id, unit.unit_type, unit.subject, unit.question,
                 unit.answer, confidence, unit.evidence_quote, lifecycle,
                 work.get("source_session_id", ""), item["evidence_ref"],
                 work.get("source_agent", ""), track.evidence_scope, "staging", 1, now),
            )
            if duplicate_of:
                con.execute(
                    "UPDATE knowledge_units SET supersedes_id=? WHERE unit_id=?",
                    (duplicate_of, unit_id),
                )
            con.execute(
                "INSERT OR IGNORE INTO knowledge_unit_evidence (unit_id, evidence_ref) VALUES (?,?)",
                (unit_id, item["evidence_ref"]),
            )
            # QA 联立 v2：question-side ref 显式落盘（role=context），
            # 供检索排序与 eval 并集匹配复用；quote 回查不覆盖此段（不作证据）。
            question_ref = str(work.get("question_ref") or "")
            if track.name == "assistant" and question_ref:
                con.execute(
                    "INSERT OR IGNORE INTO knowledge_unit_evidence (unit_id, evidence_ref, evidence_type) "
                    "VALUES (?,?,'context')",
                    (unit_id, question_ref),
                )
        con.execute(
            "UPDATE knowledge_run_items SET status='succeeded', cache_key=?, "
            "response_hash=?, unit_count=?, updated_at=? WHERE id=?",
            (cache_key, response_hash, kept, now, row_id),
        )
        stats["succeeded"] += 1
        stats["units"] += kept

    stats["processed"] += 1
    con.commit()


def process_run(
    run_id: str,
    model: str,
    db_path: Path = UNIFIED_DB,
    canonical_db: Path = AGENT_CONVERSATIONS_DB,
    batch_size: int = 50,
    max_items: int | None = None,
    workers: int = DEFAULT_WORKERS,
    min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
    track: TrackConfig = USER_TRACK,
) -> dict:
    """处理 run 的 pending/retryable items。

    多线程：worker 只调 LLM 返回纯响应；主线程单 writer 提交 SQLite。
    run 级单轨：track 注入 prompt/包装文案/evidence_scope/unit_id 前缀/结果模型。
    """
    con = connect_rw(db_path, timeout=60)
    token_provider = TokenProvider()
    rate_limiter = RequestRateLimiter(min_request_interval)
    prompt_text = track.prompt_path.read_text(encoding="utf-8")
    prompt_hash = hashlib.sha256(prompt_text.encode()).hexdigest()[:16]
    # schema_hash 含 prompt_version：assistant 轨独立 cache 命名空间（双保险，
    # prompt_hash 本身已隔离）
    schema_hash = f"{track.prompt_version}_extra_forbid"
    # 与串行路径保持同一 cache 命名空间（workers 只影响调度，不参与 cache key）
    config_hash = hashlib.sha256(json.dumps({
        "batch_size": batch_size,
        "injection": {
            "top_k": INJECTION_MAX_UNITS,
            "answer_chars": INJECTION_ANSWER_CHARS,
            "scan_min_chars": 4,
            "prompt": track.prompt_version,
        } if track.prompt_version.startswith("v2") else None,
    }, sort_keys=True).encode()).hexdigest()[:16]

    inv_row = con.execute(
        "SELECT inventory_id FROM knowledge_run_items WHERE run_id=? LIMIT 1",
        (run_id,),
    ).fetchone()
    if not inv_row:
        # Incremental prepare should have seeded items; fail closed instead of NPE
        con.close()
        raise ValueError(
            f"run {run_id} has no knowledge_run_items — refuse to process empty ledger"
        )
    inventory_id = inv_row[0]
    _ = inventory_id  # reserved for future inventory-side joins

    canon_con = sqlite3.connect(f"file:{canonical_db.as_posix()}?mode=ro", uri=True)
    canon_con.row_factory = sqlite3.Row
    subject_index = None
    state_rules = None
    if track.prompt_version.startswith("v2"):
        index_con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        subject_index = SubjectIndex(index_con)
        index_con.close()
        state_rules = load_state_subjects()

    stats = {
        "processed": 0, "succeeded": 0, "abstained": 0, "failed": 0,
        "cache_hits": 0, "units": 0, "units_dropped_no_evidence": 0,
        "workers": max(1, workers),
        "rate_limited": 0, "stopped_reason": "", "claim_skipped": 0,
        # 双轨对称截断计数（MESSAGE_MAX_CHARS）
        "truncated": 0,
    }
    if track.prompt_version.startswith("v2"):
        stats.update({"injection_embed_unavailable": 0, "invalid_duplicate_of": 0,
                      "units_downgraded_candidate": 0})
    if track.name == "assistant":
        # assistant 轨专属计数（user 轨不含这些键，零回归）
        stats["role_mismatch"] = 0
    workers = max(1, int(workers))
    # claim 窗口与并发对齐，避免一次挂起过多 in_flight
    claim_size = max(workers * 2, min(batch_size, workers * 4))
    t0 = time.time()
    consecutive_all_retryable = 0
    consecutive_zero_success = 0
    print(
        f"[process] run={run_id} track={track.name} workers={workers} "
        f"min_interval={min_request_interval}s claim_size={claim_size}",
        flush=True,
    )

    def _llm_worker(payload: dict) -> dict:
        """worker：只做 LLM 调用，不碰 SQLite。"""
        resp = call_llm_with_retry(
            prompt_text,
            payload.get("llm_input") or payload["cleaned"],
            model,
            token_provider,
            max_retries=2,  # 并行路径少重试，把节奏交给 rate limiter / 下一批
            rate_limiter=rate_limiter,
            role_label=track.role_label,
        )
        if "error" in resp:
            return {
                "row_id": payload["row_id"],
                "kind": "llm_error",
                "error_class": resp.get("error_class", "terminal"),
                "error": resp.get("error"),
                "cache_key": payload["cache_key"],
                "input_hash": payload["input_hash"],
            }
        return {
            "row_id": payload["row_id"],
            "kind": "ok",
            "raw_text": resp["text"],
            "cleaned": payload["cleaned"],
            "cache_key": payload["cache_key"],
            "input_hash": payload["input_hash"],
            "source_session_id": payload["source_session_id"],
            "source_agent": payload["source_agent"],
            "confirmation_signal": payload.get("confirmation_signal", "none"),
            "question_ref": payload.get("question_ref", ""),
            "injected_ids": payload.get("injected_ids", set()),
            "write_cache": True,
        }

    with ThreadPoolExecutor(max_workers=workers) as executor:
        while True:
            if max_items and stats["processed"] >= max_items:
                break
            take = claim_size
            if max_items:
                take = min(take, max_items - stats["processed"])
            items = get_pending_items(con, run_id, take)
            if not items:
                break

            # 1) 主线程：claim + 读 content + cache 判定
            ready_cache: list[tuple[dict, dict]] = []
            llm_payloads: list[dict] = []
            for item in items:
                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if not _claim_item(con, item["row_id"], now):
                    # 已被并发进程 claim（F-10）：不读 content、不进 payload
                    stats["claim_skipped"] += 1
                    continue

                row = canon_con.execute(
                    "SELECT m.content, m.canonical_session_id, s.agent, "
                    "m.role, m.ordinal "
                    "FROM canonical_messages m LEFT JOIN canonical_sessions s "
                    "ON m.canonical_session_id=s.canonical_session_id "
                    "WHERE m.canonical_message_id=?",
                    (item["evidence_ref"],),
                ).fetchone()
                if not row or not row["content"]:
                    con.execute(
                        "UPDATE knowledge_run_items SET status='terminal_failed', "
                        "last_error_class='missing_evidence', updated_at=? WHERE id=?",
                        (now, item["row_id"]),
                    )
                    con.commit()
                    stats["failed"] += 1
                    continue

                if track.name == "assistant" and (row["role"] or "") != "assistant":
                    # run 级单轨防混入：锚消息 role 不是 assistant → terminal_failed
                    con.execute(
                        "UPDATE knowledge_run_items SET status='terminal_failed', "
                        "last_error_class='role_mismatch', updated_at=? WHERE id=?",
                        (now, item["row_id"]),
                    )
                    con.commit()
                    stats["failed"] += 1
                    stats["role_mismatch"] += 1
                    continue

                cleaned = strip_system_injections(row["content"])
                if not is_meaningful(cleaned):
                    con.execute(
                        "UPDATE knowledge_run_items SET status='abstained', updated_at=? WHERE id=?",
                        (now, item["row_id"]),
                    )
                    con.commit()
                    stats["abstained"] += 1
                    continue

                # 单条消息尾部硬截（双轨对称；实测 >48k 仅 ~40 条，
                # 见 Phase 41 CONTEXT deferred）。quote 回查对截断后文本执行
                # （work["cleaned"] 语义一致），不会抽出来自被截部分的证据。
                if len(cleaned) > MESSAGE_MAX_CHARS:
                    cleaned = cleaned[:MESSAGE_MAX_CHARS]
                    stats["truncated"] += 1
                llm_input = cleaned
                injected_ids: set[str] = set()
                if subject_index is not None:
                    recalled = scan_subject_occurrences(subject_index, cleaned)
                    if recalled:
                        injected_ids = {str(unit["unit_id"]) for unit in recalled}
                        llm_input = (
                            format_injection_block(recalled)
                            + "\n\n---\n\n"
                            + llm_input
                        )
                confirmation_signal = "none"
                question_ref = ""
                if track.name == "assistant":
                    # QA 对（v2）：向前穿透短确认找最近实质 user 提问（仅供理解，
                    # 不作证据）；question-side ref 随 work 透传，提交时以
                    # evidence_type='context' 落盘供检索/eval 复用。无实质前置
                    # user 时该段为空（不 fail）。原文只进 LLM 输入，不写
                    # stats/日志（隐私面与 user 轨出域同级）。
                    user_ctx, question_ref = _load_preceding_user_context(
                        canon_con, row["canonical_session_id"], row["ordinal"]
                    )
                    if user_ctx:
                        llm_input = (
                            "用户问题上下文（仅供理解，不作证据）：\n"
                            f"{user_ctx}\n\n---\n\n{cleaned}"
                        )
                    # D-03 确认信号检测（confidence 修饰，非硬 gate）
                    confirmation_signal = detect_confirmation_signal(
                        canonical_db,
                        session_id=row["canonical_session_id"] or "",
                        anchor_message_ref=item["evidence_ref"],
                        con=canon_con,
                    )

                input_hash = hashlib.sha256(llm_input.encode()).hexdigest()[:32]
                # 溯源：evidence_ref → canonical session/agent（查不到保持空串）
                source_session_id = row["canonical_session_id"] or ""
                source_agent = row["agent"] or ""
                cache_key = compute_cache_key(
                    model, prompt_hash, schema_hash, input_hash, config_hash
                )
                cached = get_cached_response(con, cache_key)
                if cached is not None:
                    stats["cache_hits"] += 1
                    ready_cache.append((item, {
                        "kind": "ok",
                        "raw_text": cached,
                        "cleaned": cleaned,
                        "cache_key": cache_key,
                        "input_hash": input_hash,
                        "source_session_id": source_session_id,
                        "source_agent": source_agent,
                        "confirmation_signal": confirmation_signal,
                        "question_ref": question_ref,
                        "injected_ids": injected_ids,
                        "write_cache": False,
                    }))
                else:
                    llm_payloads.append({
                        "row_id": item["row_id"],
                        "item": item,
                        "cleaned": cleaned,
                        "llm_input": llm_input,
                        "cache_key": cache_key,
                        "input_hash": input_hash,
                        "source_session_id": source_session_id,
                        "source_agent": source_agent,
                        "confirmation_signal": confirmation_signal,
                        "question_ref": question_ref,
                        "injected_ids": injected_ids,
                    })

            # 2) 主线程先消化 cache hit
            for item, work in ready_cache:
                _commit_item_result(
                    con, run_id, item, work, model,
                    prompt_hash, schema_hash, config_hash, stats, track, state_rules,
                )

            # 3) worker 并发 LLM；主线程按完成顺序写库
            if llm_payloads:
                batch_retryable = 0
                batch_ok_before = stats["succeeded"]
                batch_abs_before = stats["abstained"]
                future_map = {
                    executor.submit(_llm_worker, p): p for p in llm_payloads
                }
                for fut in as_completed(future_map):
                    payload = future_map[fut]
                    item = payload["item"]
                    try:
                        work = fut.result()
                    except Exception as e:  # noqa: BLE001 — 保护单条，不拖垮 batch
                        work = {
                            "kind": "llm_error",
                            "error_class": "retryable",
                            "error": f"{type(e).__name__}: {e}",
                            "cache_key": payload["cache_key"],
                            "input_hash": payload["input_hash"],
                        }
                    if work.get("kind") == "llm_error" and work.get("error_class") == "retryable":
                        batch_retryable += 1
                    _commit_item_result(
                        con, run_id, item, work, model,
                        prompt_hash, schema_hash, config_hash, stats, track, state_rules,
                    )
                    done = stats["processed"] + stats["failed"] + stats["abstained"]
                    if done % 10 == 0 or done == 1:
                        elapsed = time.time() - t0
                        print(
                            f"[progress] done≈{done} "
                            f"ok={stats['succeeded']} fail={stats['failed']} "
                            f"abs={stats['abstained']} units={stats['units']} "
                            f"dropped={stats['units_dropped_no_evidence']} "
                            f"cache_hit={stats['cache_hits']} "
                            f"elapsed={elapsed:.0f}s "
                            f"interval={rate_limiter._min_interval:.1f}s",
                            flush=True,
                        )
                batch_success_delta = (
                    (stats["succeeded"] - batch_ok_before)
                    + (stats["abstained"] - batch_abs_before)
                )
                # batch 内大量 429/retryable：冷却后再取下一批，避免热循环
                if llm_payloads and batch_retryable / len(llm_payloads) >= 0.5:
                    cool = min(120.0, 20.0 + 5.0 * batch_retryable)
                    print(
                        f"[cooldown] batch retryable={batch_retryable}/"
                        f"{len(llm_payloads)} sleep {cool:.0f}s "
                        f"rate_limited={stats.get('rate_limited', 0)}",
                        flush=True,
                    )
                    time.sleep(cool)
                # 连续全 retryable（典型 429）→ 冷却后干净退出
                if llm_payloads and batch_retryable == len(llm_payloads):
                    consecutive_all_retryable += 1
                    extra = min(180.0, 30.0 * consecutive_all_retryable)
                    print(
                        f"[cooldown] all-retryable batch "
                        f"#{consecutive_all_retryable}, extra {extra:.0f}s",
                        flush=True,
                    )
                    time.sleep(extra)
                else:
                    consecutive_all_retryable = 0
                # 连续 0 成功 batch（含 terminal 烧穿）→ 熔断，避免把样本打成 terminal
                if llm_payloads and batch_success_delta == 0:
                    consecutive_zero_success += 1
                    print(
                        f"[warn] zero-success batch #{consecutive_zero_success}/"
                        f"{len(llm_payloads)}",
                        flush=True,
                    )
                else:
                    consecutive_zero_success = 0
                if consecutive_all_retryable >= 2 or consecutive_zero_success >= 3:
                    stats["stopped_reason"] = (
                        "rate_limit_circuit_breaker"
                        if consecutive_all_retryable >= 2
                        else "zero_success_circuit_breaker"
                    )
                    print(
                        f"[stop] circuit breaker ({stats['stopped_reason']}); "
                        "exit for later resume",
                        flush=True,
                    )
                    break

    canon_con.close()
    con.close()
    stats["elapsed_sec"] = round(time.time() - t0, 1)
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 14 Plan 02: production backfill engine")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--start", action="store_true", help="启动新 run")
    g.add_argument("--resume", metavar="RUN_ID", help="恢复 run")
    g.add_argument("--status", metavar="RUN_ID", help="查看 run 状态")
    p.add_argument("--inventory", help="inventory ID（--start 时必须）")
    p.add_argument("--pilot-manifest", help="pilot manifest JSON 路径（--start 时用分层 sample positions）")
    p.add_argument("--model", required=False, default=_VERTEX.model, help="模型 ID")
    p.add_argument(
        "--prompt-version",
        choices=[PROMPT_VERSION, ASSISTANT_PROMPT_VERSION, V2_PROMPT_VERSION, V2_ASSISTANT_PROMPT_VERSION],
        default=None,
        help="显式选择新 run 的 prompt 轨；v2 仅在 run 间隙启用",
    )
    p.add_argument("--limit", type=int, default=None, help="只处理前 N 条")
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--max-items", type=int, default=None, help="单次最多处理 N 条（分批模式）")
    p.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"LLM 并发线程数（默认 {DEFAULT_WORKERS}）",
    )
    p.add_argument(
        "--min-request-interval", type=float, default=DEFAULT_MIN_REQUEST_INTERVAL,
        help=f"跨线程最小请求间隔秒数（默认 {DEFAULT_MIN_REQUEST_INTERVAL}）",
    )
    p.add_argument("--db", type=Path, default=UNIFIED_DB)
    args = p.parse_args(argv)

    if args.status:
        con = connect_rw(args.db)
        stats = get_run_stats(con, args.status)
        con.close()
        print(json.dumps({"run_id": args.status, "item_stats": stats}, ensure_ascii=False, indent=2))
        return 0

    if args.start:
        # Product soft-ban: full-inventory CLI --start is not the daily path.
        # Tests and intentional backfill call start_run() API directly, or set
        # PK_KU_ALLOW_FULL_INVENTORY_START=1 after explicit human intent.
        if os.environ.get("PK_KU_ALLOW_FULL_INVENTORY_START", "").strip() != "1":
            print(
                "[error] full-inventory prod --start is soft-banned on the product path.\n"
                "  Daily KU: use incremental `pk-ku prepare` then `pk-ku extract`.\n"
                "  See: docs/runbooks/ku-incremental.md and `pk-ku workflow`.\n"
                "  Forensics/planned backfill only: set PK_KU_ALLOW_FULL_INVENTORY_START=1",
                file=sys.stderr,
            )
            return 2
        if not args.inventory:
            print("[error] --start 需要 --inventory", file=sys.stderr)
            return 2
        pilot_positions = None
        if args.pilot_manifest:
            import json as _json
            manifest = _json.loads(Path(args.pilot_manifest).read_text(encoding="utf-8"))
            pilot_positions = manifest.get("sample_positions", [])
            print(f"[pilot] {len(pilot_positions)} positions from manifest")
        run_id = start_run(args.model, args.inventory, args.db, args.limit,
                           args.batch_size, pilot_positions=pilot_positions,
                           prompt_version=args.prompt_version)
        print(f"[start] run_id: {run_id}")
        stats = process_run(
            run_id, args.model, args.db,
            max_items=args.max_items,
            batch_size=args.batch_size,
            workers=args.workers,
            min_request_interval=args.min_request_interval,
        )
        print(f"[done] {json.dumps(stats, ensure_ascii=False)}")
        return 0

    if args.resume:
        info = resume_run(args.resume, args.model, args.db, args.batch_size)
        print(f"[resume] {json.dumps(info, ensure_ascii=False)}")
        # 轨来源唯一：run manifest 的 prompt_version 反查 TrackConfig，
        # 禁止 CLI 另传 track 造成双轨混跑
        track = _resolve_run_track(args.resume, args.db)
        stats = process_run(
            args.resume, args.model, args.db,
            max_items=args.max_items,
            batch_size=args.batch_size,
            workers=args.workers,
            min_request_interval=args.min_request_interval,
            track=track,
        )
        print(f"[done] {json.dumps(stats, ensure_ascii=False)}")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
