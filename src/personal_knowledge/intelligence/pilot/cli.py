"""Local read-only CLI and metadata-only acceptance for project pilots."""
from __future__ import annotations

import argparse
from typing import Any

from personal_knowledge.intelligence.analysis.schema import canonical_json

from .service import acceptance_report, controls, explain, get_case, history, list_cases


def _safe(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_safe(item) for item in value]
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    case = sub.add_parser("case"); case.add_argument("case_id")
    sub.add_parser("list")
    explain_cmd = sub.add_parser("explain"); explain_cmd.add_argument("case_id"); explain_cmd.add_argument("--as-of")
    history_cmd = sub.add_parser("history"); history_cmd.add_argument("case_id")
    controls_cmd = sub.add_parser("controls"); controls_cmd.add_argument("case_id")
    acceptance = sub.add_parser("acceptance")
    acceptance.add_argument("--knowledge-authority", required=True)
    acceptance.add_argument("--personal-db", required=True)
    acceptance.add_argument("--external-db", required=True)
    acceptance.add_argument("--analysis-db", required=True)
    acceptance.add_argument("--as-of", required=True)
    acceptance.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "case": result = get_case(args.db, args.case_id)
        elif args.command == "list": result = list_cases(args.db)
        elif args.command == "explain": result = explain(args.db, args.case_id, as_of=args.as_of)
        elif args.command == "history": result = history(args.db, args.case_id)
        elif args.command == "controls": result = controls(args.db, args.case_id)
        else:
            if not args.metadata_only:
                raise ValueError("metadata_only_required")
            result = acceptance_report(
                pilot_db_path=args.db, knowledge_authority_path=args.knowledge_authority,
                personal_db_path=args.personal_db, external_db_path=args.external_db,
                analysis_db_path=args.analysis_db, as_of=args.as_of,
            )
    except Exception as exc:
        print(canonical_json({"ok": False, "error": str(getattr(exc, "code", type(exc).__name__))}))
        return 1
    print(canonical_json(_safe(result)))
    return 0 if not isinstance(result, dict) or result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
