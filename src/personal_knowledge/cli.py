"""Console entry points for the personal knowledge package."""

from __future__ import annotations

import importlib
import inspect
import asyncio
import os
import sys
from pathlib import Path


def _entry_main(canonical: str, legacy: str, function: str = "main") -> object:
    try:
        target = importlib.import_module(canonical)
        result = getattr(target, function)()
        return asyncio.run(result) if inspect.isawaitable(result) else result
    except ModuleNotFoundError as exc:
        if not (exc.name == canonical or canonical.startswith(f"{exc.name}.")):
            raise
    scripts = Path(__file__).resolve().parents[2] / "integration" / "scripts"
    if not scripts.is_dir():
        raise RuntimeError(
            "legacy scripts tree is unavailable; install the consolidated package"
        )
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    target = importlib.import_module(legacy)
    result = getattr(target, function)()
    return asyncio.run(result) if inspect.isawaitable(result) else result


def _help(command: str, description: str) -> bool:
    if not any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        return False
    print(f"usage: {command} [options]\n\n{description}")
    return True


def sync() -> object:
    """Product sync entry: pk-sync (conversations SSOT, etc.)."""
    from personal_knowledge.application.sync import main as sync_main

    raise SystemExit(sync_main())


def ku() -> object:
    """Product KU entry: pk-ku (inspect / prepare / extract / promote)."""
    from personal_knowledge.application.ku import main as ku_main

    raise SystemExit(ku_main())


def pipeline() -> object:
    """Deprecated: old rag-pipeline (integrated steps 1–12). Hidden from product use."""
    if _help(
        "rag-pipeline",
        "RETIRED. Use `pk-sync conversations [--write]` for conversation updates.\n"
        "Legacy integrated pipeline is blocked unless PK_ALLOW_LEGACY_PIPELINE=1.",
    ):
        return 0

    allow = os.environ.get("PK_ALLOW_LEGACY_PIPELINE", "").strip() in {
        "1",
        "true",
        "TRUE",
        "yes",
        "YES",
    }
    if not allow:
        print(
            "[retired] rag-pipeline (integrated steps 1–12) is no longer the product path.\n"
            "\n"
            "Use the canonical flow instead:\n"
            "  pk-sync conversations           # dry-run AgentsView → canonical\n"
            "  pk-sync conversations --write   # publish conversation SSOT\n"
            "  pk-ku inspect / prepare / extract / promote   # knowledge units\n"
            "  pk-sync help-legacy             # emergency legacy notes\n"
            "\n"
            "Knowledge SSOT: use `pk-ku` (not this command).\n"
            "\n"
            "To force the old integrated pipeline once (forensics only):\n"
            "  $env:PK_ALLOW_LEGACY_PIPELINE = '1'\n"
            "  python -m personal_knowledge.application.run_pipeline --legacy-integrated ...\n",
            file=sys.stderr,
        )
        raise SystemExit(2)

    # Even with allow flag, prefer explicit module invocation message if bare call
    print(
        "[warn] running retired integrated pipeline under PK_ALLOW_LEGACY_PIPELINE=1",
        file=sys.stderr,
    )
    # Inject --legacy-integrated if missing so main() does not refuse
    if "--legacy-integrated" not in sys.argv:
        sys.argv.insert(1, "--legacy-integrated")
    return _entry_main(
        "personal_knowledge.application.run_pipeline", "pipeline.run_pipeline"
    )


def search() -> object:
    if _help("rag-search", "Search the consolidated personal knowledge index."):
        return 0
    return _entry_main(
        "personal_knowledge.retrieval.unified_search", "vector.unified_search", "_cli"
    )


def api() -> object:
    if _help("rag-api", "Run the local personal knowledge REST API."):
        return 0
    return _entry_main("personal_knowledge.services.api_server", "services.api_server")


def mcp() -> object:
    if _help("rag-mcp", "Run the personal knowledge MCP stdio server."):
        return 0
    return _entry_main("personal_knowledge.services.mcp_server", "services.mcp_server")


def dashboard() -> object:
    if _help("rag-dashboard", "Run the local personal knowledge dashboard."):
        return 0
    return _entry_main("personal_knowledge.services.dashboard", "services.dashboard")
