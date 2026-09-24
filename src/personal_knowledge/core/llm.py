"""Generic OpenAI-compatible LLM client primitives (shared library layer).

Extracted from build_conversation_summary to break the cross-domain hub coupling (Phase 21 D-01).
"""

from __future__ import annotations

import os
import json
import sys
import time
from types import SimpleNamespace

from personal_knowledge.core.providers import (
    PiKernelProvider,
    ProviderRequest,
    checksum,
)

MAX_RETRY = 4  # 429/网络错误最大重试次数


class _PiCompletionClient:
    """最小 OpenAI-compatible facade backed by one Pi task per completion."""

    def __init__(self, *, purpose: str) -> None:
        self._provider = PiKernelProvider(purpose=purpose)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    @staticmethod
    def _render_messages(messages: list[dict]) -> str:
        parts = []
        for message in messages or []:
            role = str(message.get("role") or "user")
            content = str(message.get("content") or "")
            parts.append(f"[{role}]\n{content}")
        return "\n\n".join(parts).strip()

    def _create(self, *, model: str, messages: list[dict], **kwargs):
        prompt = self._render_messages(messages)
        if not prompt:
            raise ValueError("messages must not be empty")
        temperature = float(kwargs.get("temperature", 0.2) or 0.0)
        max_tokens = int(kwargs.get("max_tokens", kwargs.get("max_output_tokens", 1024)) or 1024)
        request_checksum = checksum({
            "purpose": self._provider.purpose,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        request = ProviderRequest(
            prompt=prompt,
            request_checksum=request_checksum,
            temperature=min(max(temperature, 0.0), 0.3),
            max_output_tokens=min(max(max_tokens, 1), 4096),
            timeout_seconds=min(float(kwargs.get("timeout", 120) or 120), 120.0),
        )
        result = self._provider.generate(request)
        payload = result.response_payload
        if isinstance(payload, dict) and isinstance(payload.get("text"), str):
            content = payload["text"]
        elif isinstance(payload, str):
            content = payload
        else:
            content = json.dumps(payload, ensure_ascii=False)
        usage = SimpleNamespace(
            prompt_tokens=result.telemetry.input_tokens,
            completion_tokens=result.telemetry.output_tokens,
        )
        message = SimpleNamespace(role="assistant", content=content)
        choice = SimpleNamespace(index=0, message=message, finish_reason="stop")
        return SimpleNamespace(
            id=self._provider.last_task_id,
            model=result.telemetry.model,
            choices=[choice],
            usage=usage,
        )


def _make_legacy_openai_client():
    """Construct the compatibility client only for an explicit rollback mode."""
    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("[error] 未安装 openai 库,请运行: pip install openai")
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("MEM0_API_KEY")
    if not api_key:
        sys.exit("[error] 未设置 OPENAI_API_KEY / MEM0_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    kw = {"base_url": base_url, "api_key": api_key, "timeout": 120,
          "default_headers": {"User-Agent": "curl/8.0"}}
    if proxy:
        try:
            import httpx
            kw["http_client"] = httpx.Client(
                proxy=proxy, timeout=120, headers={"User-Agent": "curl/8.0"})
        except ImportError:
            pass  # 无 httpx 则退回默认连接(直连场景)
    return OpenAI(**kw)


def make_llm_client(*, purpose: str = "generic_generation"):
    """Return the Pi-backed compatibility client; legacy requires explicit rollback."""
    if os.environ.get("PI_KERNEL_LEGACY_MODE", "").strip() == "1":
        return _make_legacy_openai_client()
    if os.environ.get("PI_KERNEL_AI_WORKFLOW", "").strip() == "1" or os.environ.get("PI_KERNEL_INTERNAL_CAPABILITY"):
        return _PiCompletionClient(purpose=purpose)
    sys.exit("[error] Pi Kernel 未启动或缺少内部能力；请先启动 agent stack")


def _chat_with_retry(client, model: str, messages: list[dict], **kwargs) -> str:
    """带 429/网络错误指数退避重试的 chat 调用,返回 content 文本。

    MiMo token-plan 端点限流较硬(实测并发>4 路会批量 429),并发场景下
    必须重试才能保证成功率。退避间隔 2/4/8/16s,最多 MAX_RETRY 次。
    """
    last_exc = None
    for attempt in range(MAX_RETRY):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, **kwargs
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            last_exc = exc
            # 429 限流或网络瞬断才重试;其他错误(鉴权/参数)直接抛
            name = type(exc).__name__
            if "RateLimit" in name or "Timeout" in name or "APIConnection" in name \
               or "APITimeout" in name or "ServiceUnavailable" in name:
                wait = 2 ** (attempt + 1)  # 2,4,8,16s
                time.sleep(wait)
                continue
            raise
    raise last_exc  # 重试用尽仍失败则抛最后错误
