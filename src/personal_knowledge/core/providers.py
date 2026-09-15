"""Dependency-injected provider boundaries; no provider is live by default.

This module was relocated from ``intelligence/analysis/providers.py`` (OC-10)
so the shared LLM provider hub lives in the neutral ``core`` layer and the
application layer no longer needs to import into ``intelligence``.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import tomllib
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import HTTPError, URLError


def checksum(value: Any) -> str:
    """Canonical JSON checksum (mirrors the analysis schema contract)."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


class ProviderError(RuntimeError):
    def __init__(self, code: str, detail: str = "", *, retryable: bool = False) -> None:
        self.code = code
        self.detail = detail
        self.retryable = retryable
        super().__init__(f"{code}: {detail}" if detail else code)


class ProviderTimeout(ProviderError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("provider_timeout", detail, retryable=True)


class TokenProvider:
    """Run-scoped Vertex AI token provider (thread-safe).

    Relocated from ``application/knowledge/build_knowledge_units_prod.py``
    (OC-10) so the evaluation layer can acquire Vertex tokens without importing
    into the application layer. Uses the same runtime config discovery as the
    production backfill engine; never live by default.
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires: float = 0
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires:
                return self._token
            self._token = self._fetch()
            self._expires = time.time() + 3000  # 50 min
            return self._token

    def refresh(self) -> str:
        with self._lock:
            self._token = None
        return self.get()

    @staticmethod
    def _fetch() -> str:
        from personal_knowledge.core.runtime_config import gcloud_access_token, vertex_config
        return gcloud_access_token(vertex_config())


@dataclass(frozen=True)
class ProviderRequest:
    prompt: str
    request_checksum: str
    temperature: float
    max_output_tokens: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        # Budget ceilings are read from configuration (env / pi-budget.json)
        # so operators can tune them without code changes; defaults stay 0.3 /
        # 4096 / 120. The checks themselves remain mandatory safety gates.
        from personal_knowledge.core.runtime_config import (
            provider_max_output_tokens,
            provider_max_temperature,
            provider_timeout_seconds,
        )
        if not self.prompt or len(self.request_checksum) != 64:
            raise ProviderError("provider_request_invalid")
        if (not 0 <= self.temperature <= provider_max_temperature()
                or not 1 <= self.max_output_tokens <= int(provider_max_output_tokens())):
            raise ProviderError("provider_budget_invalid")
        if not 0 < self.timeout_seconds <= provider_timeout_seconds():
            raise ProviderError("provider_timeout_invalid")


@dataclass(frozen=True)
class ProviderTelemetry:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_amount: float
    cost_currency: str
    latency_ms: int
    status: str

    def __post_init__(self) -> None:
        if any(not str(getattr(self, field)).strip() for field in ("provider", "model", "cost_currency", "status")):
            raise ProviderError("provider_telemetry_invalid")
        if min(self.input_tokens, self.output_tokens, self.latency_ms) < 0 or self.cost_amount < 0:
            raise ProviderError("provider_telemetry_invalid")


@dataclass(frozen=True)
class ProviderResult:
    response_payload: Mapping[str, Any]
    response_checksum: str
    telemetry: ProviderTelemetry

    def __post_init__(self) -> None:
        if checksum(self.response_payload) != self.response_checksum:
            raise ProviderError("provider_response_checksum_mismatch")


class AnalysisProvider(Protocol):
    def generate(self, request: ProviderRequest) -> ProviderResult: ...


class LegacyProviderAdapter:
    """Compatibility wrapper; normal Pi mode cannot instantiate legacy control."""

    def __init__(self, provider: AnalysisProvider, *, mode: str = "normal") -> None:
        if mode != "rollback":
            raise ProviderError("legacy_provider_rollback_only")
        self.provider = provider

    def generate(self, request: ProviderRequest) -> ProviderResult:
        return self.provider.generate(request)


class ReplayProvider:
    """Deterministic fixture/replay provider using only caller-supplied payloads."""

    def __init__(
        self,
        responses: Mapping[str, Any] | list[Mapping[str, Any] | Exception],
        *,
        model: str = "replay-v1",
        input_tokens: int = 1,
        output_tokens: int = 1,
    ) -> None:
        self._responses = list(responses) if isinstance(responses, list) else [responses]
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls = 0

    def generate(self, request: ProviderRequest) -> ProviderResult:
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        if not self._responses:
            raise ProviderError("replay_response_missing")
        selected = self._responses[index]
        if isinstance(selected, Exception):
            raise selected
        payload = dict(selected)
        return ProviderResult(
            response_payload=payload, response_checksum=checksum(payload),
            telemetry=ProviderTelemetry(
                provider="replay", model=self.model, input_tokens=self.input_tokens,
                output_tokens=self.output_tokens, cost_amount=0.0, cost_currency="USD",
                latency_ms=0, status="completed",
            ),
        )


class PiKernelProvider:
    """Python contract adapter backed by the loopback Pi Kernel task route.

    The Kernel returns the provider payload only to this explicitly
    capability-authenticated local adapter. The payload is parsed in memory
    and is never written to Pi stores; all durable receipts remain metadata.
    """

    _MAX_OUTPUT = {
        "structured_analysis": 1024,
        "guarded_generation": 2048,
        "extraction_summary": 1024,
        "generic_generation": 4096,
        "conversation_summary": 4096,
        "memory_candidate_extraction": 4096,
        "memory_repair": 4096,
    }

    def __init__(
        self,
        *,
        purpose: str = "structured_analysis",
        base_url: str | None = None,
        capability: str | None = None,
        timeout_seconds: float = 120.0,
        transport: Callable[[Mapping[str, Any], float, str], Mapping[str, Any]] | None = None,
    ) -> None:
        self.purpose = str(purpose)
        self.base_url = str(base_url or os.environ.get("PI_KERNEL_URL", "http://127.0.0.1:8790")).rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ProviderError("provider_endpoint_invalid")
        self.capability = capability if capability is not None else os.environ.get("PI_KERNEL_INTERNAL_CAPABILITY", "")
        from personal_knowledge.core.runtime_config import provider_timeout_seconds
        self.timeout_seconds = min(max(float(timeout_seconds), 0.1), provider_timeout_seconds())
        self.transport = transport
        self.calls = 0
        self.last_task_id: str | None = None
        self.last_session_id: str | None = None
        self.last_receipt: dict[str, Any] | None = None

    def _request(self, body: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
        if self.transport is not None:
            return self.transport(body, timeout, self.capability)
        if not self.capability:
            raise ProviderError("provider_internal_capability_missing")
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = UrlRequest(
            f"{self.base_url}/v1/tasks", data=payload, method="POST",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
                "X-PI-Internal-Capability": self.capability,
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                error = json.loads(exc.read().decode("utf-8"))
            except Exception:
                error = {}
            code = str((error.get("error") or {}).get("code") or "provider_transport_error")
            if code in {"provider_timeout", "provider_transport_error"}:
                raise ProviderTimeout() from exc
            raise ProviderError(code) from exc
        except (TimeoutError, URLError, OSError, json.JSONDecodeError) as exc:
            error = ProviderTimeout() if isinstance(exc, TimeoutError) else ProviderError("provider_transport_error", retryable=True)
            raise error from exc

    def generate(self, request: ProviderRequest) -> ProviderResult:
        if self.purpose not in self._MAX_OUTPUT:
            raise ProviderError("model_route_unknown")
        task_id = f"pi_task_py_{checksum({'purpose': self.purpose, 'request_checksum': request.request_checksum})[:24]}"
        session_id = f"pi_session_py_{checksum({'purpose': self.purpose, 'request_checksum': request.request_checksum})[:24]}"
        idempotency_key = f"pi-idem-py-{self.purpose}-{request.request_checksum[:40]}"
        body = {
            "task_id": task_id,
            "session_id": session_id,
            "idempotency_key": idempotency_key,
            "purpose": self.purpose,
            "prompt": request.prompt,
            "include_response": True,
        }
        self.calls += 1
        raw = self._request(body, min(self.timeout_seconds, request.timeout_seconds))
        if not isinstance(raw, Mapping) or raw.get("ok") is not True:
            code = str((raw.get("error") or {}).get("code") if isinstance(raw, Mapping) else "provider_response_invalid")
            raise ProviderError(code or "provider_response_invalid")
        payload = raw.get("response")
        receipt = raw.get("receipt") or {}
        if not isinstance(payload, Mapping) or not isinstance(receipt, Mapping):
            raise ProviderError("provider_response_invalid")
        telemetry = ProviderTelemetry(
            provider=str(receipt.get("provider") or "pi-kernel"),
            model=str(receipt.get("model") or "unknown"),
            input_tokens=int(receipt.get("input_tokens") or 0),
            output_tokens=int(receipt.get("output_tokens") or 0),
            cost_amount=float(receipt.get("cost") or 0.0),
            cost_currency=str(receipt.get("currency") or "CNY"),
            latency_ms=0,
            status="completed",
        )
        self.last_task_id, self.last_session_id = task_id, session_id
        self.last_receipt = {
            "task_id": task_id, "session_id": session_id,
            "request_checksum": request.request_checksum,
            "response_checksum": str(receipt.get("response_checksum") or checksum(payload)),
            "usage_checksum": str(receipt.get("usage_checksum") or ""),
            "route": self.purpose, "provider": telemetry.provider,
            "model": telemetry.model, "cost": telemetry.cost_amount,
            "currency": telemetry.cost_currency,
        }
        return ProviderResult(response_payload=dict(payload), response_checksum=checksum(payload), telemetry=telemetry)

    def stage_candidate(
        self, *, candidate_id: str, proposal: Mapping[str, Any], evidence_refs: list[Mapping[str, Any]],
        candidate_checksum: str, run_checksum: str,
    ) -> Mapping[str, Any]:
        if not self.last_receipt or not self.last_task_id or not self.last_session_id:
            raise ProviderError("provider_receipt_missing")
        if not self.capability:
            raise ProviderError("provider_internal_capability_missing")
        body = {
            "task_id": self.last_task_id, "session_id": self.last_session_id,
            "idempotency_key": f"{self.last_receipt['request_checksum']}:candidate",
            "candidate_id": candidate_id,
            "proposal": {"kind": "analysis_candidate", "candidate_checksum": candidate_checksum, "run_checksum": run_checksum, **dict(proposal)},
            "evidence_refs": evidence_refs,
            "model_receipt": self.last_receipt,
        }
        if self.transport is not None:
            return self.transport(body, self.timeout_seconds, self.capability)
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = UrlRequest(
            f"{self.base_url}/internal/v1/candidates", data=payload, method="POST",
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload)), "X-PI-Internal-Capability": self.capability},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
            raise ProviderError("candidate_stage_failed") from exc
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise ProviderError("candidate_stage_failed")
        return result


Transport = Callable[[Mapping[str, Any], float], Mapping[str, Any]]


class OpenAICompatibleProvider:
    """Authorized boundary around an injected transport; contains no HTTP client or secret."""

    def __init__(
        self,
        *,
        model: str,
        transport: Transport | None = None,
        enabled: bool = False,
        credential_present: bool = False,
        provider_name: str = "openai-compatible",
    ) -> None:
        self.model = model
        self.transport = transport
        self.enabled = enabled
        self.credential_present = credential_present
        self.provider_name = provider_name

    def generate(self, request: ProviderRequest) -> ProviderResult:
        if not self.enabled:
            raise ProviderError("provider_not_authorized")
        if not self.credential_present:
            raise ProviderError("provider_credential_missing")
        if self.transport is None:
            raise ProviderError("provider_transport_missing")
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": request.prompt}],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        started = time.monotonic()
        try:
            raw = self.transport(body, request.timeout_seconds)
        except TimeoutError as exc:
            raise ProviderTimeout() from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("provider_transport_error", type(exc).__name__, retryable=True) from exc
        latency = int((time.monotonic() - started) * 1000)
        try:
            content = raw["choices"][0]["message"]["content"]
            payload = json.loads(content) if isinstance(content, str) else dict(content)
            usage = raw.get("usage") or {}
            input_tokens = int(usage.get("prompt_tokens", 0))
            output_tokens = int(usage.get("completion_tokens", 0))
            cost = float(raw.get("cost_amount", 0.0))
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("provider_response_invalid") from exc
        return ProviderResult(
            response_payload=payload, response_checksum=checksum(payload),
            telemetry=ProviderTelemetry(
                provider=self.provider_name, model=str(raw.get("model") or self.model),
                input_tokens=input_tokens, output_tokens=output_tokens,
                cost_amount=cost, cost_currency=str(raw.get("cost_currency") or "USD"),
                latency_ms=latency, status="completed",
            ),
        )


CodexRunner = Callable[..., subprocess.CompletedProcess[str]]


def _classify_codex_failure(stderr: str) -> str:
    """Reduce Codex stderr to a stable code without retaining diagnostic text."""
    error_text = stderr.lower()
    if "input is not valid utf-8" in error_text or "input appears to be" in error_text:
        return "codex_stdin_encoding_invalid"
    if ("invalid utf-8 in streamed bytes" in error_text
            or "incomplete utf-8 code point" in error_text):
        return "codex_response_stream_encoding_invalid"
    if "schema" in error_text or "response_format" in error_text:
        return "codex_output_schema_rejected"
    if "model" in error_text and any(
        token in error_text for token in ("not found", "unsupported", "unavailable")
    ):
        return "provider_model_unavailable"
    if any(token in error_text for token in ("unauthorized", "authentication", "login required")):
        return "provider_credential_missing"
    if "unexpected argument" in error_text or "unrecognized option" in error_text:
        return "codex_cli_argument_invalid"
    if ("utf-8" in error_text or "utf8" in error_text) and "config" in error_text:
        return "codex_config_encoding_invalid"
    if ("utf-8" in error_text or "utf8" in error_text) and any(
        token in error_text for token in ("agents.md", "instructions", "rules")
    ):
        return "codex_instruction_encoding_invalid"
    if "utf-8" in error_text or "utf8" in error_text:
        return "codex_runtime_text_encoding_invalid"
    return "codex_cli_failed"


def resolve_codex_command(*, runner: CodexRunner = subprocess.run) -> tuple[str | None, str | None]:
    """Prefer the newest direct executable over an older npm cmd wrapper."""
    candidates: list[str] = []
    vscode_root = Path.home() / ".vscode" / "extensions"
    if vscode_root.is_dir():
        candidates.extend(str(path) for path in vscode_root.glob(
            "openai.chatgpt-*-win32-x64/bin/windows-x86_64/codex.exe"
        ))
    on_path = shutil.which("codex")
    if on_path:
        candidates.append(on_path)
    versions: list[tuple[tuple[int, ...], str, str]] = []
    for command in dict.fromkeys(candidates):
        try:
            completed = runner(
                [command, "--version"], text=True, capture_output=True, timeout=5,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        match = re.search(r"codex-cli\s+(\d+(?:\.\d+)+)", completed.stdout)
        if completed.returncode == 0 and match:
            version = match.group(1)
            versions.append((tuple(int(item) for item in version.split(".")), command, version))
    if not versions:
        return None, None
    _, command, version = max(versions, key=lambda item: item[0])
    return command, version


def codex_cli_preflight(
    model: str,
    *,
    runner: CodexRunner = subprocess.run,
    command_path: str | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Check ChatGPT login and the public model catalog without generating."""
    resolved_version: str | None = None
    if command_path:
        command = command_path
    else:
        command, resolved_version = resolve_codex_command(runner=runner)
    findings: list[str] = []
    available_models: tuple[str, ...] = ()
    credential_present = False
    if not command:
        findings.append("codex_cli_unavailable")
    else:
        try:
            login = runner(
                [command, "login", "status"], text=True, capture_output=True,
                timeout=timeout_seconds, encoding="utf-8", errors="replace",
            )
            credential_present = login.returncode == 0
            if not credential_present:
                findings.append("provider_credential_missing")
            catalog = runner(
                [command, "debug", "models"], text=True, capture_output=True,
                timeout=timeout_seconds, encoding="utf-8", errors="replace",
            )
            if catalog.returncode != 0:
                findings.append("provider_model_catalog_unavailable")
            else:
                raw = json.loads(catalog.stdout)
                items = raw.get("models", ()) if isinstance(raw, Mapping) else raw
                if not isinstance(items, list):
                    raise ValueError("model catalog must be a list")
                available_models = tuple(sorted({
                    str(item.get("slug") or item.get("id") or item.get("model"))
                    for item in items if isinstance(item, Mapping)
                    and (item.get("slug") or item.get("id") or item.get("model"))
                }))
                if model not in available_models:
                    findings.append("provider_model_unavailable")
        except (OSError, subprocess.TimeoutExpired):
            findings.append("codex_cli_preflight_failed")
        except (json.JSONDecodeError, TypeError, ValueError):
            findings.append("provider_model_catalog_invalid")
    return {
        "ok": not findings,
        "model": model,
        "credential_present": credential_present,
        "model_available": model in available_models,
        "command_path": command,
        "cli_version": resolved_version,
        "available_models": available_models,
        "findings": tuple(sorted(set(findings))),
        "provider_calls": 0,
    }


class CodexCliProvider:
    """One-shot existing-ChatGPT boundary using Codex JSONL and a frozen schema."""

    def __init__(
        self,
        *,
        model: str,
        output_schema_path: Path | str,
        working_directory: Path | str,
        enabled: bool = False,
        credential_present: bool = False,
        max_calls: int = 1,
        runner: CodexRunner = subprocess.run,
        preflight_runner: CodexRunner = subprocess.run,
        command_path: str | None = None,
        config_path: Path | str | None = None,
    ) -> None:
        self.model = model
        self.output_schema_path = Path(output_schema_path)
        self.working_directory = Path(working_directory)
        self.enabled = enabled
        self.credential_present = credential_present
        self.max_calls = max_calls
        self.runner = runner
        self.preflight_runner = preflight_runner
        self.command_path = command_path
        self.config_path = Path(config_path) if config_path else Path.home() / ".codex" / "config.toml"
        self.calls = 0

    def _mcp_disable_overrides(self) -> list[str]:
        if not self.config_path.is_file():
            return []
        try:
            config = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ProviderError("codex_config_isolation_invalid") from exc
        overrides: list[str] = []
        for name in sorted(str(name) for name in (config.get("mcp_servers") or {})):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ProviderError("codex_config_isolation_invalid")
            overrides.extend(("-c", f"mcp_servers.{name}.enabled=false"))
        return overrides

    @staticmethod
    def _jsonl_error_text(stdout: str) -> str:
        messages: list[str] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, Mapping) or event.get("type") not in {"error", "turn.failed"}:
                continue
            raw = event.get("message") or event.get("error")
            if isinstance(raw, str):
                messages.append(raw)
            elif isinstance(raw, Mapping):
                messages.append(json.dumps(raw, sort_keys=True))
        return "\n".join(messages)

    @staticmethod
    def _events(stdout: str) -> tuple[dict[str, Any], dict[str, int]]:
        final_text: str | None = None
        usage: dict[str, int] = {}
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProviderError("codex_jsonl_invalid") from exc
            if not isinstance(event, Mapping):
                continue
            item = event.get("item")
            if (event.get("type") == "item.completed" and isinstance(item, Mapping)
                    and item.get("type") == "agent_message" and isinstance(item.get("text"), str)):
                final_text = str(item["text"])
            if event.get("type") in {"agent_message", "message.completed"} and isinstance(event.get("text"), str):
                final_text = str(event["text"])
            raw_usage = event.get("usage") or event.get("token_usage")
            if isinstance(raw_usage, Mapping):
                for target, sources in {
                    "input_tokens": ("input_tokens", "prompt_tokens"),
                    "output_tokens": ("output_tokens", "completion_tokens"),
                }.items():
                    for source in sources:
                        if source in raw_usage:
                            usage[target] = int(raw_usage[source])
                            break
        if final_text is None:
            raise ProviderError("codex_final_message_missing")
        try:
            payload = json.loads(final_text)
        except json.JSONDecodeError as exc:
            raise ProviderError("codex_response_json_invalid") from exc
        if not isinstance(payload, dict):
            raise ProviderError("codex_response_json_invalid")
        return payload, usage

    def generate(self, request: ProviderRequest) -> ProviderResult:
        if not self.enabled:
            raise ProviderError("provider_not_authorized")
        if not self.credential_present:
            raise ProviderError("provider_credential_missing")
        if self.max_calls != 1 or self.calls >= self.max_calls:
            raise ProviderError("provider_call_budget_exhausted")
        if not self.output_schema_path.is_file() or not self.working_directory.is_dir():
            raise ProviderError("provider_runtime_path_invalid")
        preflight = codex_cli_preflight(
            self.model, runner=self.preflight_runner,
            command_path=self.command_path,
            timeout_seconds=min(request.timeout_seconds, 15.0),
        )
        if not preflight["ok"]:
            raise ProviderError(str(preflight["findings"][0]))
        self.calls += 1
        command_path = str(preflight["command_path"])
        command = [
            command_path, "exec", "--ephemeral", "--ignore-rules",
            "--disable", "codex_hooks", "--disable", "multi_agent",
            "--disable", "memories", "--disable", "plugins",
            "--disable", "remote_plugin", "-c", "mcp_servers={}",
            *self._mcp_disable_overrides(),
            "-c", "notify=[]", "-c", 'approval_policy="never"',
            "--skip-git-repo-check", "--sandbox", "read-only", "--model", self.model,
            "--output-schema", str(self.output_schema_path.resolve()), "--json",
            "--color", "never", "--cd", str(self.working_directory.resolve()), "-",
        ]
        started = time.monotonic()
        child_env = os.environ.copy()
        child_env.pop("CODEX_API_KEY", None)
        child_env.pop("OPENAI_API_KEY", None)
        child_env.update({"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "LC_CTYPE": "C.UTF-8"})
        try:
            completed = self.runner(
                command, input=request.prompt.encode("utf-8"), text=False,
                capture_output=True, timeout=request.timeout_seconds, env=child_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderTimeout("codex_cli") from exc
        except OSError as exc:
            raise ProviderError("codex_cli_unavailable", type(exc).__name__) from exc
        latency = int((time.monotonic() - started) * 1000)
        if completed.returncode != 0:
            stdout_text = (
                completed.stdout.decode("utf-8", errors="replace")
                if isinstance(completed.stdout, bytes) else str(completed.stdout)
            )
            stderr_text = (
                completed.stderr.decode("utf-8", errors="replace")
                if isinstance(completed.stderr, bytes) else str(completed.stderr)
            )
            error_text = self._jsonl_error_text(stdout_text) or stderr_text
            code = _classify_codex_failure(error_text)
            raise ProviderError(code, f"exit={completed.returncode}")
        try:
            stdout = (
                completed.stdout.decode("utf-8", errors="strict")
                if isinstance(completed.stdout, bytes) else str(completed.stdout)
            )
        except UnicodeDecodeError as exc:
            raise ProviderError("codex_output_encoding_invalid") from exc
        payload, usage = self._events(stdout)
        return ProviderResult(
            response_payload=payload, response_checksum=checksum(payload),
            telemetry=ProviderTelemetry(
                provider="codex-chatgpt", model=self.model,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cost_amount=0.0, cost_currency="USD", latency_ms=latency,
                status="completed",
            ),
        )


__all__ = [
    "AnalysisProvider", "OpenAICompatibleProvider", "ProviderError", "ProviderRequest",
    "CodexCliProvider", "ProviderResult", "ProviderTelemetry", "ProviderTimeout",
    "codex_cli_preflight", "resolve_codex_command",
    "ReplayProvider", "PiKernelProvider", "LegacyProviderAdapter",
    "TokenProvider",
]
