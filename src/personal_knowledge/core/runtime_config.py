"""Portable runtime configuration and dependency discovery.

Machine-specific values must come from environment variables or executable
discovery.  Importing this module never probes private data or mutates state.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, NamedTuple


DEFAULT_VERTEX_LOCATION = "global"
DEFAULT_VERTEX_MODEL = "gemini-3.5-flash-lite"


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else None


class VertexConfig(NamedTuple):
    project: str
    location: str
    model: str
    gcloud: str
    sdk_root: Path | None = None


def vertex_config() -> VertexConfig:
    executable = os.environ.get("PERSONAL_DATA_GCLOUD", "").strip() or shutil.which("gcloud")
    # Delay the actionable dependency error until token acquisition so modules
    # remain importable for dry runs, schema checks and unit tests.
    executable = executable or "gcloud"
    return VertexConfig(
        project=os.environ.get("PERSONAL_DATA_GCP_PROJECT", "project-c5cbd608-1b00-454e-80f"),
        location=os.environ.get("PERSONAL_DATA_VERTEX_LOCATION", DEFAULT_VERTEX_LOCATION),
        model=os.environ.get("PERSONAL_DATA_VERTEX_MODEL", DEFAULT_VERTEX_MODEL),
        gcloud=executable,
        sdk_root=_env_path("CLOUDSDK_ROOT_DIR"),
    )


def vertex_generate_content_url(config: VertexConfig, model: str | None = None) -> str:
    selected_model = model or config.model
    return (
        f"https://aiplatform.googleapis.com/v1/projects/{config.project}"
        f"/locations/{config.location}/publishers/google/models/"
        f"{selected_model}:generateContent"
    )


def vertex_generation_config(model: str, max_output_tokens: int) -> dict[str, object]:
    """Return generation settings accepted by the selected Vertex model."""
    config: dict[str, object] = {"maxOutputTokens": max_output_tokens}
    if not model.lower().startswith("gemini-3.5-flash-lite"):
        config.update({"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}})
    return config


def gcloud_access_token(config: VertexConfig | None = None) -> str:
    config = config or vertex_config()
    command = [config.gcloud, "auth", "print-access-token"]
    if Path(config.gcloud).suffix.lower() == ".py":
        command.insert(0, sys.executable)
    env = dict(os.environ)
    env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
    if config.sdk_root:
        env["CLOUDSDK_ROOT_DIR"] = str(config.sdk_root)
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Unable to run gcloud ({config.gcloud}): {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "gcloud is not authenticated"
        raise RuntimeError(f"Unable to obtain Vertex AI access token: {detail}")
    return result.stdout.strip()


def _candidate_embedding_paths() -> list[Path]:
    """Return conventional local model locations without binding a drive/user."""
    name = "bge-small-zh-v1.5"
    candidates = [
        Path.home() / "models" / name,
        Path.home() / ".cache" / "modelscope" / "hub" / "models" / "BAAI" / name,
        Path.home() / ".cache" / "huggingface" / "hub" / f"models--BAAI--{name}",
    ]
    if os.name == "nt":
        # Derive available volume roots; no machine-specific drive is encoded.
        for codepoint in range(ord("A"), ord("Z") + 1):
            root = Path(f"{chr(codepoint)}:/")
            if root.exists():
                candidates.append(root / "models" / name)
    return candidates


def embedding_model_path() -> Path:
    configured = _env_path("PERSONAL_DATA_EMBED_MODEL_PATH")
    if configured:
        return configured
    cache = _env_path("SENTENCE_TRANSFORMERS_HOME")
    if cache:
        candidate = cache / "bge-small-zh-v1.5"
        if (candidate / "config.json").is_file():
            return candidate
    for candidate in _candidate_embedding_paths():
        # HF/modelscope 缓存壳目录（models--BAAI--*）也是目录但没有根级
        # config.json，sentence-transformers 无法加载；历史上它先于真实模型
        # 目录命中，导致每次 embed 抛错、向量检索被静默降级成关键词。
        if candidate.is_dir() and (candidate / "config.json").is_file():
            return candidate.resolve()
    raise RuntimeError(
        "Local embedding model not configured. Set PERSONAL_DATA_EMBED_MODEL_PATH "
        "to the bge-small-zh-v1.5 directory."
    )


def semantic_api_url() -> str:
    return os.environ.get("PERSONAL_DATA_SEMANTIC_API", "http://127.0.0.1:8000/search/semantic")


# ---------------------------------------------------------------------------
# Provider / analysis budget limits.
#
# Resolution order (fail-safe): environment variable -> optional JSON budget
# file -> built-in default. A missing or malformed budget file is ignored so
# import and request construction never raise because of configuration.
# Budget values may be raised through configuration (operator decision) but
# the safety rails that prevent leaks and timeouts are NOT touched by this
# mechanism (see the plan: evidence MAX_ROWS/MAX_BYTES/TIMEOUT_MS remain
# hardcoded safety constants).
# ---------------------------------------------------------------------------

DEFAULT_BUDGET_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "governance" / "config" / "pi-budget.json"
)


def _load_budget_file() -> dict[str, Any]:
    """Return the optional JSON budget document; {} when absent or invalid."""
    path = os.environ.get("PI_BUDGET_CONFIG", "").strip()
    try:
        source = Path(path) if path else DEFAULT_BUDGET_CONFIG_PATH
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _budget_number(
    section: str,
    key: str,
    env_name: str,
    default: float,
    *,
    lower: float | None = None,
    upper: float | None = None,
) -> float:
    """Env-first, config-file fallback, built-in default; never raises.

    Values outside the optional ``[lower, upper]`` window are ignored so a
    misconfigured budget fails back to the default instead of breaking request
    construction.
    """

    def valid(parsed: float) -> bool:
        if not math.isfinite(parsed):
            return False
        if lower is not None and parsed < lower:
            return False
        if upper is not None and parsed > upper:
            return False
        return True

    raw = os.environ.get(env_name, "").strip()
    if raw:
        try:
            parsed = float(raw)
            if valid(parsed):
                return parsed
        except ValueError:
            pass
    section_value = _load_budget_file().get(section)
    if isinstance(section_value, dict):
        value = section_value.get(key)
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and valid(float(value))):
            return float(value)
    return default


def provider_max_temperature() -> float:
    """Ceiling enforced by ProviderRequest; defaults to 0.3."""
    return _budget_number("provider", "max_temperature", "PI_PROVIDER_MAX_TEMPERATURE", 0.3, lower=0)


def provider_max_output_tokens() -> float:
    """Ceiling enforced by ProviderRequest; defaults to 4096."""
    return _budget_number("provider", "max_output_tokens", "PI_PROVIDER_MAX_OUTPUT_TOKENS", 4096.0, lower=1)


def provider_timeout_seconds() -> float:
    """Ceiling enforced by ProviderRequest / PiKernelProvider; defaults to 120."""
    return _budget_number("provider", "timeout_seconds", "PI_PROVIDER_TIMEOUT_SECONDS", 120.0, lower=0.1)


def analysis_max_attempts() -> int:
    """Default retry budget for decision-analysis execution; defaults to 2."""
    return int(_budget_number(
        "analysis", "max_attempts", "PI_ANALYSIS_MAX_ATTEMPTS", 2.0,
        lower=1, upper=3,
    ))


def analysis_temperature_max() -> float:
    """Default sampling temperature ceiling when the policy omits it; defaults to 0.3."""
    return _budget_number("analysis", "temperature_max", "PI_ANALYSIS_TEMPERATURE_MAX", 0.3, lower=0)


def analysis_max_output_tokens() -> float:
    """Default sampling output-token ceiling when the policy omits it; defaults to 4096."""
    return _budget_number("analysis", "max_output_tokens", "PI_ANALYSIS_MAX_OUTPUT_TOKENS", 4096.0, lower=1)
