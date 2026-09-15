"""Metadata-only local interface for External Context source/schema inspection."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from personal_knowledge.core.project_paths import EXTERNAL_CONTEXT_DB
from .registry import DEFAULT_REGISTRY, ExternalContextService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect the local External Context authority")
    parser.add_argument("--db", type=Path, default=EXTERNAL_CONTEXT_DB)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List allowlisted source metadata")
    listing.add_argument("--json", action="store_true")
    get = commands.add_parser("get", help="Get one allowlisted source definition")
    get.add_argument("source_id")
    get.add_argument("--json", action="store_true")
    status = commands.add_parser("schema-status", help="Inspect the independent schema read-only")
    status.add_argument("--json", action="store_true")
    return parser


def invoke(args: argparse.Namespace) -> dict:
    service = ExternalContextService(args.registry, args.db)
    if args.command == "list":
        return service.invoke("sources.list")
    if args.command == "get":
        return service.invoke("sources.get", source_id=args.source_id)
    return service.invoke("schema.status")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = invoke(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "invoke", "main"]
