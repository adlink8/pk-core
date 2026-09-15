"""Thin re-export of the LLM provider hub (now owned by ``core.providers``).

The provider implementations moved to :mod:`personal_knowledge.core.providers`
to break the ``application -> intelligence`` package dependency (OC-10).
This module exists so existing consumers can keep using
``from personal_knowledge.intelligence.analysis.providers import X`` unchanged.
"""
from __future__ import annotations

from personal_knowledge.core.providers import (  # noqa: F401
    AnalysisProvider,
    CodexCliProvider,
    CodexRunner,
    LegacyProviderAdapter,
    OpenAICompatibleProvider,
    PiKernelProvider,
    ProviderError,
    ProviderRequest,
    ProviderResult,
    ProviderTelemetry,
    ProviderTimeout,
    ReplayProvider,
    Transport,
    codex_cli_preflight,
    resolve_codex_command,
)


__all__ = [
    "AnalysisProvider", "OpenAICompatibleProvider", "ProviderError", "ProviderRequest",
    "CodexCliProvider", "ProviderResult", "ProviderTelemetry", "ProviderTimeout",
    "codex_cli_preflight", "resolve_codex_command",
    "ReplayProvider", "PiKernelProvider", "LegacyProviderAdapter",
]
