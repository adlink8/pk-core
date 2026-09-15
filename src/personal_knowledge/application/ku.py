"""Product KU CLI (post Phase 14–21).

Stable operator surface for incremental knowledge units. Prefer this entry over
calling long module paths or editing code to change extract policy.

Usage::

    pk-ku inspect
    pk-ku prepare --model gemini-3.5-flash-lite --provider vertex_google \\
        --endpoint https://aiplatform.googleapis.com --auth-mode gcloud
    pk-ku extract --run <fresh_run_id> --model gemini-3.5-flash-lite --max-items 50
    pk-ku status --run <run_id>
    pk-ku canonical --run <run_id> --write
    pk-ku publish --run <run_id> --write          # staging → current (additive)
    pk-ku vector [--write]                        # candidate Chroma index
    pk-ku extract-gate --run <run_id>
    pk-ku canary --candidate-override <collection> --report path.json
    pk-ku canary --report path.json --list-critical
    pk-ku canary --report path.json --label-with-llm --only-critical
    pk-ku canary --report path.json --strict
    pk-ku promote --list
    pk-ku promote --collection <name> --eval-summary … --eval-gate …
        # eval required by default; forensics: --allow-without-eval
    pk-ku watermark                 # show committed vs current source checksum
    pk-ku watermark --advance --from-canonical --write   # after successful promote
    pk-ku reconcile [--subject S] [--since YYYY-MM-DD] [--max-subjects N]
                    [--dry-run] [--write --i-know]       # lifecycle growth line (never DELETE)
    pk-ku history --subject S [--limit N] [--include-all-lifecycle]  # growth line read
    pk-ku doctor [--json] [--skip-ports]   # read-only product health (no promote)

Hard rule: daily path is inspect → prepare → extract(resume delta run).
Full inventory + prod --start is NOT exposed here (planned backfill only via
underlying modules with explicit human intent).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _default_artifact() -> Path:
    return Path("var/reports/analysis/ai_context/knowledge_incremental_delta.json")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pk-ku",
        description=(
            "Knowledge Unit product CLI (incremental only). "
            "Policy knobs live on subcommands — do not change code for daily ops."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Daily order:\n"
            "  1) pk-sync conversations [--write]\n"
            "  2) pk-ku inspect\n"
            "  3) pk-ku prepare --model … --provider … --endpoint … --auth-mode …\n"
            "  4) pk-ku extract --run ir_* --max-items N\n"
            "  5) pk-ku extract-gate / canonical / publish / vector\n"
            "  6) pk-ku canary --candidate-override … --report …\n"
            "  7) pk-ku promote --collection … --eval-summary … --eval-gate …\n"
            "  8) pk-ku watermark --advance --from-canonical --write\n"
            "\n"
            "Full inventory backfill is intentionally NOT a pk-ku subcommand.\n"
            "See docs/runbooks/ku-incremental.md"
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    # --- inspect ---
    ins = sub.add_parser("inspect", help="Free delta report (no LLM, no writes)")
    ins.add_argument("--db", type=Path, default=None)
    ins.add_argument("--canonical-db", type=Path, default=None)
    ins.add_argument("--source-checksum", default="", help="Optional last source checksum")

    # --- prepare ---
    prep = sub.add_parser(
        "prepare",
        help="Freeze delta inventory + extract queue (no LLM). Policy via flags.",
    )
    prep.add_argument("--model", required=True, help="Model ID (required, fail-closed)")
    prep.add_argument("--provider", default="", help="vertex_google / openai / …")
    prep.add_argument("--endpoint", default="", help="LLM endpoint URL")
    prep.add_argument("--auth-mode", default="", help="gcloud / api_key")
    prep.add_argument(
        "--artifact",
        type=Path,
        default=None,
        help=f"JSON artifact path (default {_default_artifact()})",
    )
    prep.add_argument("--db", type=Path, default=None)
    prep.add_argument("--canonical-db", type=Path, default=None)
    prep.add_argument(
        "--extract-new-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Queue only change_type=new (default true)",
    )
    prep.add_argument(
        "--extract-since-watermark",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Floor session date at watermark day (default off: the floor excludes "
        "late-synced historical sessions by session date, risking permanent skips)",
    )
    prep.add_argument(
        "--since",
        default="",
        metavar="YYYY-MM-DD",
        help="Explicit session floor; overrides watermark floor",
    )
    prep.add_argument(
        "--skip-succeeded",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Drop refs already succeeded (default true)",
    )
    prep.add_argument(
        "--roles",
        default="",
        help="Comma roles e.g. user or user,assistant (default all eligible)",
    )
    prep.add_argument(
        "--baseline-inventory",
        default="",
        metavar="ID",
        help="Force before inventory_id",
    )
    prep.add_argument(
        "--max-extract-items",
        type=int,
        default=None,
        metavar="N",
        help="Cap seeded queue after filters (newest first)",
    )
    prep.add_argument(
        "--track",
        default="user",
        choices=["user", "assistant"],
        help="Extraction track (default user). assistant: watermark key "
        "committed_assistant, roles default assistant, prompt v1_assistant",
    )

    # --- extract (paid) ---
    ext = sub.add_parser(
        "extract",
        help="Paid LLM extract: resume an incremental run from prepare (not full inventory)",
    )
    ext.add_argument("--run", required=True, metavar="RUN_ID", help="fresh_run_id from prepare")
    ext.add_argument("--model", default="gemini-3.5-flash-lite")
    ext.add_argument("--max-items", type=int, default=None, help="Process at most N items this call")
    ext.add_argument("--workers", type=int, default=None)
    ext.add_argument("--min-request-interval", type=float, default=None)
    ext.add_argument("--batch-size", type=int, default=50)
    ext.add_argument("--db", type=Path, default=None)

    # --- status ---
    st = sub.add_parser("status", help="Show item ledger stats for a run")
    st.add_argument("--run", required=True, metavar="RUN_ID")
    st.add_argument("--db", type=Path, default=None)

    # --- canonical ---
    can = sub.add_parser("canonical", help="Build canonical units from an extraction run")
    can.add_argument("--run", required=True, metavar="RUN_ID")
    can.add_argument("--write", action="store_true", help="Persist (default dry-run)")
    can.add_argument("--db", type=Path, default=None)

    # --- publish (additive staging → current) ---
    pub = sub.add_parser(
        "publish",
        help="Promote this incremental run's staging units/canonical → current (no demote)",
    )
    pub.add_argument("--run", required=True, metavar="RUN_ID")
    pub.add_argument("--write", action="store_true")
    pub.add_argument("--db", type=Path, default=None)

    # --- vector candidate ---
    vec = sub.add_parser("vector", help="Build candidate KU vector store (does not touch active)")
    vec.add_argument("--write", action="store_true")
    vec.add_argument("--db", type=Path, default=None)

    # --- extraction gate ---
    eg = sub.add_parser("extract-gate", help="Strict extraction gate for a run (writes gate row)")
    eg.add_argument("--run", required=True, metavar="RUN_ID")
    eg.add_argument("--min-yield", type=float, default=None)
    eg.add_argument("--track", choices=["user", "assistant"], default=None,
                    help="抽取轨（缺省从 run manifest 推断）；min-yield 缺省按轨校准")
    eg.add_argument("--db", type=Path, default=None)

    # --- canary ---
    canary = sub.add_parser(
        "canary",
        help="Run / check / strict-gate knowledge canary (does not touch active by default)",
    )
    canary.add_argument(
        "--candidate-override",
        default="",
        help="Candidate collection name (preferred; leaves active unchanged)",
    )
    canary.add_argument("--queries", type=int, default=30, help="Query count (default 30)")
    canary.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Report JSON path (required for --strict / --check-label-completeness)",
    )
    canary.add_argument(
        "--check-label-completeness",
        action="store_true",
        help="Only check labels on an existing report",
    )
    canary.add_argument(
        "--list-critical",
        action="store_true",
        help="List wrong/stale rows (index, query_hash, top returned_ids/scores)",
    )
    canary.add_argument(
        "--strict",
        action="store_true",
        help="Compute PASS/FAIL gate (requires fully labeled report)",
    )
    canary.add_argument(
        "--label-with-llm",
        action="store_true",
        help="Fill empty labels via LLM (OpenAI key or Vertex/gcloud)",
    )
    canary.add_argument(
        "--only-critical",
        action="store_true",
        help="With --label-with-llm: only re-label wrong/stale (and empty)",
    )
    canary.add_argument("--model", default="", help="Model for --label-with-llm")
    canary.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "openai", "vertex_google"],
        help="LLM backend for labeling",
    )
    canary.add_argument("--max-items", type=int, default=None, help="Max items to label this call")
    canary.add_argument(
        "--dry-run",
        action="store_true",
        help="With --label-with-llm: do not write report",
    )

    # --- promote ---
    prom = sub.add_parser("promote", help="List or promote candidate index (active last)")
    prom.add_argument("--list", action="store_true", help="List index versions")
    prom.add_argument("--collection", default="", help="Candidate collection to promote")
    prom.add_argument(
        "--require-eval-pass",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Require Phase 17 gate PASS (default true; --no-require-eval-pass forensics only)",
    )
    prom.add_argument(
        "--allow-without-eval",
        action="store_true",
        help="Forensics only: skip eval gate (loud warning; alias of --no-require-eval-pass)",
    )
    prom.add_argument("--eval-summary", type=Path, default=None)
    prom.add_argument("--eval-gate", type=Path, default=None)

    # --- watermark ---
    wm = sub.add_parser(
        "watermark",
        help="Show or advance knowledge_source_watermark (after successful promote; "
        "fail-closed on unfinished/failed extraction items)",
    )
    wm.add_argument(
        "--advance",
        action="store_true",
        help="Advance committed watermark (requires --write + checksum source; "
        "blocked by unfinished items or unacknowledged terminal_failed)",
    )
    wm.add_argument(
        "--from-canonical",
        action="store_true",
        help="Use compute_source_checksum(canonical_db) as new watermark",
    )
    wm.add_argument(
        "--checksum",
        default="",
        help="Explicit checksum to commit (alternative to --from-canonical)",
    )
    wm.add_argument(
        "--write",
        action="store_true",
        help="Persist advance (default dry-run / show only)",
    )
    wm.add_argument(
        "--acknowledge-failures",
        action="store_true",
        help="With --advance --write: record terminal_failed items into "
        "knowledge_dead_refs, then advance (ignored without --write)",
    )
    wm.add_argument(
        "--track",
        default="user",
        choices=["user", "assistant"],
        help="Watermark key: user → 'committed' (default), "
        "assistant → 'committed_assistant'",
    )
    wm.add_argument("--db", type=Path, default=None)
    wm.add_argument("--canonical-db", type=Path, default=None)

    # --- reconcile (lifecycle growth line; never DELETE) ---
    rec = sub.add_parser(
        "reconcile",
        help="Subject-level lifecycle reconcile (default dry-run; never DELETE)",
    )
    rec.add_argument("--subject", default="", help="Limit to exact subject")
    rec.add_argument(
        "--since",
        default="",
        metavar="YYYY-MM-DD",
        help="Only subjects that have current units created on/after this date",
    )
    rec.add_argument("--max-subjects", type=int, default=None, metavar="N")
    rec.add_argument(
        "--dry-run",
        action="store_true",
        help="Report only (default when --write not set)",
    )
    rec.add_argument(
        "--write",
        action="store_true",
        help="Persist lifecycle/supersedes_id (requires --i-know)",
    )
    rec.add_argument(
        "--i-know",
        action="store_true",
        help="Confirmation required with --write",
    )
    rec.add_argument("--db", type=Path, default=None)
    rec.add_argument(
        "--artifact",
        type=Path,
        default=None,
        help="Optional full JSON report path under var/reports/…",
    )

    # --- history (growth line read; never mutates) ---
    hist = sub.add_parser(
        "history",
        help="Growth-line versions for a subject (current+superseded+…; read-only)",
    )
    hist.add_argument("--subject", required=True, help="Exact subject string")
    hist.add_argument(
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="Max rows (default 50; 0 = unlimited)",
    )
    hist.add_argument(
        "--include-all-lifecycle",
        action="store_true",
        help="Include any lifecycle value (default: current/superseded/deprecated/conflict)",
    )
    hist.add_argument("--db", type=Path, default=None)
    hist.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON only (no human table)",
    )

    # --- promote-units (human candidate/staging promotion; dry-run by default) ---
    prom_units = sub.add_parser(
        "promote-units",
        help="Promote selected candidate/staging units after evidence re-match",
    )
    prom_units.add_argument("--unit-id", action="append", required=True)
    prom_units.add_argument("--write", action="store_true", help="Persist after snapshot and re-match")
    prom_units.add_argument("--report", type=Path, default=None)

    life_prop = sub.add_parser("lifecycle-propose", help="Build bounded metadata-safe lifecycle review manifest")
    life_prop.add_argument("--subject", default="")
    life_prop.add_argument("--max-subjects", type=int, default=20)
    life_prop.add_argument("--artifact", type=Path, required=True)
    life_prop.add_argument("--db", type=Path, default=None)

    life_reg = sub.add_parser("lifecycle-register", help="Register exact human-reviewed lifecycle manifest")
    life_reg.add_argument("--manifest", type=Path, required=True)
    life_reg.add_argument("--write", action="store_true")
    life_reg.add_argument("--i-know", action="store_true")
    life_reg.add_argument("--db", type=Path, default=None)

    life_finalize = sub.add_parser("lifecycle-finalize", help="Bind human decisions to exact proposal and emit reviewed manifest")
    life_finalize.add_argument("--proposal", type=Path, required=True)
    life_finalize.add_argument("--review", type=Path, required=True)
    life_finalize.add_argument("--artifact", type=Path, required=True)
    life_finalize.add_argument("--db", type=Path, default=None)

    life_apply = sub.add_parser("lifecycle-apply", help="Atomically apply registered reviewed lifecycle manifest")
    life_apply.add_argument("--manifest", type=Path, required=True)
    life_apply.add_argument("--actor", required=True)
    life_apply.add_argument("--write", action="store_true")
    life_apply.add_argument("--i-know", action="store_true")
    life_apply.add_argument("--db", type=Path, default=None)

    life_rollback = sub.add_parser("lifecycle-rollback", help="Reverse one applied manifest with linked events")
    life_rollback.add_argument("--manifest-id", required=True)
    life_rollback.add_argument("--actor", required=True)
    life_rollback.add_argument("--write", action="store_true")
    life_rollback.add_argument("--i-know", action="store_true")
    life_rollback.add_argument("--db", type=Path, default=None)

    life_status = sub.add_parser("lifecycle-status", help="Read-only lifecycle ledger adoption status")
    life_status.add_argument("--strict", action="store_true")
    life_status.add_argument("--db", type=Path, default=None)

    # --- doctor (read-only product health; never promote / write) ---
    doc = sub.add_parser(
        "doctor",
        help="Read-only product health checks (DBs, active pointer, watermark, ports)",
    )
    doc.add_argument(
        "--json",
        action="store_true",
        help="Emit full JSON report only",
    )
    doc.add_argument(
        "--skip-ports",
        action="store_true",
        help="Skip TCP/HTTP checks for :8000/:8789 (warn-only when present)",
    )
    doc.add_argument(
        "--no-facade",
        action="store_true",
        help="Skip application→domains facade import inventory",
    )
    doc.add_argument(
        "--skip-coverage",
        action="store_true",
        help="Skip the source × role coverage matrix check (escape hatch)",
    )
    doc.add_argument("--db", type=Path, default=None)
    doc.add_argument("--canonical-db", type=Path, default=None)
    doc.add_argument("--active-pointer", type=Path, default=None)

    # --- workflow help ---
    sub.add_parser("workflow", help="Print canonical daily KU workflow + forbidden paths")

    # --- view-aware inspect/prepare/status (Phase 62-06; zero paid) ---
    view_ins = sub.add_parser(
        "view-inspect",
        help="Event/view-aware read-only inspect (active generation, policy/view "
             "counts, deterministic exclusions, semantic-gate replay coverage, "
             "pending estimates). Never pays.",
    )
    view_ins.add_argument("--conversation-db", type=Path, default=None)

    view_prep = sub.add_parser(
        "view-prepare",
        help="Zero-cost view/policy/evidence-bound candidate prepare (estimates "
             "and ledger only; no provider, no paid extraction).",
    )
    view_prep.add_argument("--conversation-db", type=Path, default=None)
    view_prep.add_argument(
        "--semantic-prompt-version", default="semantic-v1",
        help="Semantic gate prompt version bound into the run key",
    )
    view_prep.add_argument(
        "--semantic-schema-version", default="schema-v1",
        help="Semantic gate schema version bound into the run key",
    )
    view_prep.add_argument(
        "--model", default="gemini-3.5-flash-lite",
        help="Deterministic estimate model (never invoked)",
    )

    view_st = sub.add_parser(
        "view-status",
        help="Read-only delta backlog, or ledger status with --run (including legacy audit).",
    )
    view_st.add_argument("--run", metavar="RUN_ID")
    view_st.add_argument("--conversation-db", type=Path, default=None)

    consume = sub.add_parser("view-consume", help="Prepare one committed event-delta page; persist progress, never call a provider.")
    consume.add_argument("--conversation-db", type=Path, default=None)
    consume.add_argument("--max-events", type=int, default=100)
    consume.add_argument("--max-context-events", type=int, default=2000)

    baseline = sub.add_parser("view-baseline", help="Preview first event baseline; initialize only with exact preview approval.")
    baseline.add_argument("--conversation-db", type=Path, default=None)
    baseline.add_argument("--write", action="store_true")
    baseline.add_argument("--approval", default=None)

    return p


def _cmd_workflow() -> int:
    print(
        """pk-ku daily workflow (incremental only)
=====================================
1. pk-sync conversations [--write]     # dialogue SSOT if chats grew
2. pk-ku inspect                       # free; record new_refs / source_changed
3. pk-ku prepare --model … --provider … --endpoint … --auth-mode …
     Policy flags (no code edits):
       --extract-new-only / --no-extract-new-only
       --extract-since-watermark / --no-extract-since-watermark
       --since YYYY-MM-DD
       --roles user[,assistant]
       --track user|assistant   # assistant 轨：独立 watermark key
                                # (committed_assistant) + v1_assistant prompt，
                                # roles 缺省 assistant；run 级单轨不混跑
       --skip-succeeded / --no-skip-succeeded
       --baseline-inventory ID
       --max-extract-items N
     Read extract_item_count + fresh_run_id from JSON. If inspect has delta but
     prepare no_op → STOP (prepare defect). Do NOT invent full inventory path.
4. pk-ku extract --run <fresh_run_id> --max-items N --workers 4
5. pk-ku extract-gate --run <run_id> [--min-yield 0.7]
6. pk-ku canonical --run <run_id> --write
7. pk-ku publish --run <run_id> --write   # additive staging→current (not full demote)
8. pk-ku vector --write                   # candidate index only
9. pk-ku canary --candidate-override <coll> --report path.json
   # labels + triage (do NOT promote while --strict FAIL):
   pk-ku canary --report path.json --list-critical
   pk-ku canary --report path.json --label-with-llm --only-critical   # optional
   pk-ku canary --report path.json --check-label-completeness
   pk-ku canary --report path.json --strict
10. pk-ku promote --collection <cand> --eval-summary … --eval-gate …
    # only after --strict PASS + eval; --allow-without-eval forensics only
11. pk-ku watermark --advance --from-canonical --write   # only after promote OK
12. pk-ku reconcile [--subject S] [--since …] [--max-subjects N]  # default dry-run
    # write path: pk-ku reconcile --write --i-know  (lifecycle/supersedes only; never DELETE)
13. pk-ku history --subject S [--limit N]   # growth line read (all lifecycles; not retrieval)
0.  pk-ku doctor [--json] [--skip-ports]    # preflight: DBs + active pointer + ports (read-only)

Forbidden as daily ops:
  - build_knowledge_inventory --write + prod --start on full inventory
  - resume mistaken full-inventory run until pending=0
  - promote without eval (default refuse; no product waiver)
  - advance watermark before promote
  - rag-pipeline for knowledge

Docs: docs/runbooks/ku-incremental.md
"""
    )
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    from personal_knowledge.application.knowledge.refresh_knowledge_units import (
        get_committed_watermark,
        main as refresh_main,
    )
    from personal_knowledge.core.project_paths import UNIFIED_DB

    argv: list[str] = ["--inspect"]
    source_checksum = args.source_checksum or get_committed_watermark(
        args.db or UNIFIED_DB
    )
    if source_checksum:
        argv.extend(["--source-checksum", source_checksum])
    if args.db is not None:
        argv.extend(["--db", str(args.db)])
    if args.canonical_db is not None:
        argv.extend(["--canonical-db", str(args.canonical_db)])
    return int(refresh_main(argv) or 0)


def _cmd_prepare(args: argparse.Namespace) -> int:
    from personal_knowledge.application.knowledge.refresh_knowledge_units import main as refresh_main

    artifact = args.artifact or _default_artifact()
    argv: list[str] = [
        "--prepare",
        "--model",
        args.model,
        "--provider",
        args.provider,
        "--endpoint",
        args.endpoint,
        "--auth-mode",
        args.auth_mode,
        "--artifact",
        str(artifact),
    ]
    if args.db is not None:
        argv.extend(["--db", str(args.db)])
    if args.canonical_db is not None:
        argv.extend(["--canonical-db", str(args.canonical_db)])
    if args.extract_new_only:
        argv.append("--extract-new-only")
    else:
        argv.append("--no-extract-new-only")
    if args.extract_since_watermark:
        argv.append("--extract-since-watermark")
    else:
        argv.append("--no-extract-since-watermark")
    if args.since:
        argv.extend(["--since", args.since])
    if args.skip_succeeded:
        argv.append("--skip-succeeded")
    else:
        argv.append("--no-skip-succeeded")
    if args.roles:
        argv.extend(["--roles", args.roles])
    if args.baseline_inventory:
        argv.extend(["--baseline-inventory", args.baseline_inventory])
    if args.max_extract_items is not None:
        argv.extend(["--max-extract-items", str(args.max_extract_items)])
    if args.track != "user":
        argv.extend(["--track", args.track])
    return int(refresh_main(argv) or 0)


def _cmd_extract(args: argparse.Namespace) -> int:
    import os

    # Guard before heavy imports: incremental prepare mints ir_* run ids.
    run_id = (args.run or "").strip()
    if not run_id:
        print("[error] --run is required", file=sys.stderr)
        return 2

    from personal_knowledge.application.knowledge.view_candidate_prepare import (
        is_legacy_run_id,
        is_view_run_id,
    )

    # Phase 62-06 D-31: view-policy runs are never extractable in this phase.
    if is_view_run_id(run_id):
        print(
            f"[blocked] run_id={run_id!r} is a view-policy candidate run. "
            "No paid extraction is authorized in Phase 62: a separate user cost "
            "approval and a representative LLM pilot are required before any "
            "extract can run.",
            file=sys.stderr,
        )
        return 2
    # D-30: every ``ir_*`` run uses the superseded message-level queue
    # contract.  The audit table is useful evidence, but it must never be the
    # authority for the spend guard: a missing/corrupt ledger must fail closed
    # before the paid executor is imported.
    if is_legacy_run_id(run_id):
        print(
            f"[blocked] run_id={run_id!r} is a legacy message-level prepare "
            "run whose queue semantics are superseded (D-30). Its audit "
            "history is preserved but it is non-executable under the "
            "view-policy contract.",
            file=sys.stderr,
        )
        return 2

    allow_non_inc = os.environ.get("PK_KU_ALLOW_NON_INCREMENTAL_RUN", "").strip() in {
        "1",
        "true",
        "TRUE",
        "yes",
        "YES",
    }
    if not run_id.startswith("ir_") and not allow_non_inc:
        print(
            f"[warn] run_id={run_id!r} does not look like an incremental prepare id (ir_*).\n"
            "  Daily extract should use fresh_run_id from `pk-ku prepare`.\n"
            "  To force (forensics): set PK_KU_ALLOW_NON_INCREMENTAL_RUN=1",
            file=sys.stderr,
        )
        return 2

    from personal_knowledge.application.knowledge.build_knowledge_units_prod import (
        DEFAULT_MIN_REQUEST_INTERVAL,
        DEFAULT_WORKERS,
        main as prod_main,
    )

    argv: list[str] = ["--resume", run_id, "--model", args.model]
    if args.max_items is not None:
        argv.extend(["--max-items", str(args.max_items)])
    workers = args.workers if args.workers is not None else DEFAULT_WORKERS
    argv.extend(["--workers", str(workers)])
    interval = (
        args.min_request_interval
        if args.min_request_interval is not None
        else DEFAULT_MIN_REQUEST_INTERVAL
    )
    argv.extend(["--min-request-interval", str(interval)])
    argv.extend(["--batch-size", str(args.batch_size)])
    if args.db is not None:
        argv.extend(["--db", str(args.db)])
    return int(prod_main(argv) or 0)


def _cmd_status(args: argparse.Namespace) -> int:
    from personal_knowledge.application.knowledge.build_knowledge_units_prod import main as prod_main

    argv: list[str] = ["--status", args.run]
    if args.db is not None:
        argv.extend(["--db", str(args.db)])
    return int(prod_main(argv) or 0)


def _cmd_canonical(args: argparse.Namespace) -> int:
    from personal_knowledge.application.knowledge.build_canonical_knowledge_units import (
        main as canonical_main,
    )

    argv: list[str] = ["--run", args.run]
    if args.write:
        argv.append("--write")
    else:
        argv.append("--dry-run")
    if args.db is not None:
        argv.extend(["--db", str(args.db)])
    return int(canonical_main(argv) or 0)


def _cmd_publish(args: argparse.Namespace) -> int:
    from personal_knowledge.application.knowledge.publish_incremental_run import main as pub_main

    argv: list[str] = ["--run", args.run]
    if args.write:
        argv.append("--write")
    else:
        argv.append("--dry-run")
    if args.db is not None:
        argv.extend(["--db", str(args.db)])
    return int(pub_main(argv) or 0)


def _cmd_vector(args: argparse.Namespace) -> int:
    from personal_knowledge.application.knowledge.build_knowledge_unit_vector_store import (
        main as vector_main,
    )

    argv: list[str] = []
    if args.write:
        argv.append("--write")
    else:
        argv.append("--dry-run")
    if args.db is not None:
        argv.extend(["--db", str(args.db)])
    return int(vector_main(argv) or 0)


def _cmd_extract_gate(args: argparse.Namespace) -> int:
    from personal_knowledge.evaluation.knowledge.evaluate_knowledge_unit_extraction import (
        main as gate_main,
    )

    argv: list[str] = ["--run", args.run]
    if args.min_yield is not None:
        argv.extend(["--min-yield", str(args.min_yield)])
    if getattr(args, "track", None):
        argv.extend(["--track", args.track])
    if args.db is not None:
        argv.extend(["--db", str(args.db)])
    return int(gate_main(argv) or 0)


def _cmd_canary(args: argparse.Namespace) -> int:
    from personal_knowledge.evaluation.knowledge.evaluate_knowledge_canary import (
        main as canary_main,
    )

    argv: list[str] = []
    if args.candidate_override:
        argv.extend(["--candidate-override", args.candidate_override])
    if args.queries is not None:
        argv.extend(["--queries", str(args.queries)])
    if args.report is not None:
        argv.extend(["--report", str(args.report)])
    if args.check_label_completeness:
        argv.append("--check-label-completeness")
    if getattr(args, "list_critical", False):
        argv.append("--list-critical")
    if args.strict:
        argv.append("--strict")
    if args.label_with_llm:
        argv.append("--label-with-llm")
    if getattr(args, "only_critical", False):
        argv.append("--only-critical")
    if args.model:
        argv.extend(["--model", args.model])
    if args.backend:
        argv.extend(["--backend", args.backend])
    if args.max_items is not None:
        argv.extend(["--max-items", str(args.max_items)])
    if args.dry_run:
        argv.append("--dry-run")
    return int(canary_main(argv) or 0)


def _cmd_promote(args: argparse.Namespace) -> int:
    from personal_knowledge.application.knowledge.promote_knowledge_index import (
        promote_main,
    )

    argv: list[str] = []
    if args.list:
        argv.append("--list")
    if args.collection:
        argv.extend(["--promote", args.collection])
    # Default is require-eval-pass=True; only forward explicit waiver.
    if getattr(args, "allow_without_eval", False) or not getattr(
        args, "require_eval_pass", True
    ):
        argv.append("--allow-without-eval")
    if args.eval_summary is not None:
        argv.extend(["--eval-summary", str(args.eval_summary)])
    if args.eval_gate is not None:
        argv.extend(["--eval-gate", str(args.eval_gate)])
    if not argv:
        print(
            "usage: pk-ku promote --list | --collection NAME "
            "[--eval-summary PATH] [--eval-gate PATH] "
            "[--allow-without-eval forensics only]",
            file=sys.stderr,
        )
        return 2
    return int(promote_main(argv) or 0)


def _cmd_reconcile(args: argparse.Namespace) -> int:
    """Lifecycle reconcile: default dry-run; --write requires --i-know."""
    if args.write and not args.i_know:
        print(
            "[error] --write requires --i-know (lifecycle updates are deliberate; never DELETE)",
            file=sys.stderr,
        )
        return 2
    if args.write:
        print(
            "[error] direct heuristic writes are retired; use lifecycle-propose -> human review -> lifecycle-register -> lifecycle-apply",
            file=sys.stderr,
        )
        return 2

    from personal_knowledge.application.knowledge.reconcile_knowledge_lifecycle import (
        main as reconcile_main,
    )

    argv: list[str] = []
    if args.subject:
        argv.extend(["--subject", args.subject])
    if args.since:
        argv.extend(["--since", args.since])
    if args.max_subjects is not None:
        argv.extend(["--max-subjects", str(args.max_subjects)])
    if args.write:
        argv.append("--write")
        argv.append("--i-know")
    else:
        argv.append("--dry-run")
    if args.db is not None:
        argv.extend(["--db", str(args.db)])
    if args.artifact is not None:
        argv.extend(["--artifact", str(args.artifact)])
    return int(reconcile_main(argv) or 0)


def _cmd_lifecycle(args: argparse.Namespace) -> int:
    from personal_knowledge.application.knowledge.lifecycle_events import (
        LifecycleError,
        apply_manifest,
        finalize_review,
        lifecycle_status,
        load_manifest,
        propose_reconcile_manifest,
        register_manifest,
        rollback_manifest,
    )
    from personal_knowledge.core.project_paths import UNIFIED_DB

    db_path = args.db or UNIFIED_DB
    try:
        if args.command == "lifecycle-propose":
            manifest = propose_reconcile_manifest(
                db_path, subject=args.subject or None,
                max_subjects=args.max_subjects, artifact=args.artifact,
            )
            result = {"ok": True, "manifest_id": manifest["manifest_id"], "manifest_checksum": manifest["manifest_checksum"], "action_count": len(manifest["actions"]), "artifact": str(args.artifact), "write": False}
        elif args.command == "lifecycle-register":
            if args.write and not args.i_know:
                raise LifecycleError("--write requires --i-know")
            result = register_manifest(db_path, load_manifest(args.manifest), write=bool(args.write))
        elif args.command == "lifecycle-finalize":
            reviewed = finalize_review(args.proposal, args.review, args.artifact)
            if reviewed.get("review_status") == "no_actions_approved":
                result = {
                    "ok": True,
                    "review_status": reviewed["review_status"],
                    "proposal_manifest_id": reviewed["proposal_manifest_id"],
                    "receipt_checksum": reviewed["receipt_checksum"],
                    "approved": 0,
                    "rejected": len(reviewed.get("rejected_unit_ids") or []),
                    "artifact": str(args.artifact),
                }
            else:
                result = {"ok": True, "manifest_id": reviewed["manifest_id"], "manifest_checksum": reviewed["manifest_checksum"], "approved": len(reviewed["actions"]), "rejected": len((reviewed.get("review_receipt") or {}).get("rejected_unit_ids") or []), "artifact": str(args.artifact)}
        elif args.command == "lifecycle-apply":
            if not (args.write and args.i_know):
                raise LifecycleError("lifecycle apply requires --write --i-know")
            result = apply_manifest(db_path, load_manifest(args.manifest), actor_id=args.actor)
        elif args.command == "lifecycle-rollback":
            if not (args.write and args.i_know):
                raise LifecycleError("lifecycle rollback requires --write --i-know")
            result = rollback_manifest(db_path, args.manifest_id, actor_id=args.actor)
        else:
            result = lifecycle_status(db_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "lifecycle-status" and args.strict and not result.get("ok"):
            return 1
        return 0
    except (LifecycleError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def _cmd_history(args: argparse.Namespace) -> int:
    """Growth-line history for a subject (read-only; never mutates)."""
    from personal_knowledge.application.knowledge.history_knowledge_units import (
        main as history_main,
    )

    argv: list[str] = ["--subject", args.subject]
    if args.limit is not None:
        argv.extend(["--limit", str(args.limit)])
    if args.include_all_lifecycle:
        argv.append("--include-all-lifecycle")
    if args.db is not None:
        argv.extend(["--db", str(args.db)])
    if args.json:
        argv.append("--json")
    return int(history_main(argv) or 0)


def _cmd_promote_units(args: argparse.Namespace) -> int:
    """Human-invoked per-unit promotion; dry-run unless --write is explicit."""
    from personal_knowledge.application.knowledge.promote_units import main as promote_units_main

    argv: list[str] = []
    for unit_id in args.unit_id:
        argv.extend(["--unit-id", unit_id])
    if args.write:
        argv.append("--write")
    if args.report is not None:
        argv.extend(["--report", str(args.report)])
    return int(promote_units_main(argv) or 0)


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Read-only product health; never promote / watermark write / DELETE."""
    from personal_knowledge.application.knowledge.doctor_ku import main as doctor_main

    argv: list[str] = []
    if args.json:
        argv.append("--json")
    if args.skip_ports:
        argv.append("--skip-ports")
    if args.skip_coverage:
        argv.append("--skip-coverage")
    if args.no_facade:
        argv.append("--no-facade")
    if args.db is not None:
        argv.extend(["--db", str(args.db)])
    if args.canonical_db is not None:
        argv.extend(["--canonical-db", str(args.canonical_db)])
    if args.active_pointer is not None:
        argv.extend(["--active-pointer", str(args.active_pointer)])
    return int(doctor_main(argv) or 0)


def _cmd_watermark(args: argparse.Namespace) -> int:
    """Show or advance source watermark. Advance is opt-in and fail-closed.

    --advance --write refuses (exit 2) while any extraction run still has
    pending/in_flight/retryable items, and requires --acknowledge-failures to
    record terminal_failed items into knowledge_dead_refs before advancing, so
    unprocessed refs are never silently dropped from the next delta.
    """
    import json
    import sqlite3

    from personal_knowledge.application.knowledge.refresh_knowledge_units import (
        acknowledge_dead_refs,
        advance_watermark,
        check_watermark_advance_preconditions,
        compute_source_checksum,
        get_committed_watermark,
    )
    from personal_knowledge.core.project_paths import (
        AGENT_CONVERSATIONS_DB,
        UNIFIED_DB,
    )

    db_path = args.db or UNIFIED_DB
    canonical_db = args.canonical_db or AGENT_CONVERSATIONS_DB
    # watermark key 选择集中一处：user → committed，assistant → committed_assistant
    wm_key = "committed_assistant" if args.track == "assistant" else "committed"
    committed = get_committed_watermark(db_path, key=wm_key)
    current = compute_source_checksum(canonical_db) if canonical_db.exists() else ""
    wm_updated = ""
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        row = con.execute(
            "SELECT updated_at FROM knowledge_source_watermark WHERE key=?",
            (wm_key,),
        ).fetchone()
        wm_updated = row[0] if row else ""
        con.close()
    except sqlite3.Error:
        pass

    if not args.advance:
        doc = {
            "track": args.track,
            "watermark_key": wm_key,
            "committed": committed,
            "committed_updated_at": wm_updated,
            "current_source_checksum": current,
            "source_matches_watermark": bool(committed and current and committed == current),
            "write": False,
        }
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0

    # advance path
    if args.from_canonical and args.checksum:
        print("[error] use only one of --from-canonical or --checksum", file=sys.stderr)
        return 2
    if args.from_canonical:
        new_cs = current
        if not new_cs:
            print("[error] cannot compute source checksum (canonical missing?)", file=sys.stderr)
            return 2
    elif args.checksum:
        new_cs = args.checksum.strip()
    else:
        print(
            "[error] --advance requires --from-canonical or --checksum",
            file=sys.stderr,
        )
        return 2

    preconditions = check_watermark_advance_preconditions(db_path)
    preview = {
        "action": "advance",
        "track": args.track,
        "watermark_key": wm_key,
        "before": committed,
        "after": new_cs,
        "changed": committed != new_cs,
        "write": bool(args.write),
        "preconditions": preconditions,
    }
    if not args.write:
        note = "dry-run only; pass --write to persist"
        if args.acknowledge_failures:
            note += " (--acknowledge-failures ignored without --write)"
        preview["note"] = note
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0

    unfinished = preconditions["unfinished"]
    if unfinished:
        detail = ", ".join(
            f"{u['run_id']}:{u['status']}={u['count']}" for u in unfinished
        )
        print(
            f"[error] unfinished extraction items block watermark advance: {detail}; "
            "finish or clean up these runs first",
            file=sys.stderr,
        )
        return 2
    failed = preconditions["failed"]
    if failed and not args.acknowledge_failures:
        detail = ", ".join(f"{f['run_id']}={f['count']}" for f in failed)
        print(
            f"[error] terminal_failed items block watermark advance: {detail}; "
            "rerun with --acknowledge-failures to record them as dead refs and advance",
            file=sys.stderr,
        )
        return 2
    dead_refs_recorded = acknowledge_dead_refs(db_path) if failed else 0

    result = advance_watermark(db_path, new_cs, key=wm_key)
    out = {**preview, **result, "write": True}
    if failed:
        out["dead_refs_recorded"] = dead_refs_recorded
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _cmd_view_inspect(args: argparse.Namespace) -> int:
    """Read-only event/view-aware inspect (never writes, never pays)."""
    from personal_knowledge.application.knowledge.view_candidate_prepare import (
        inspect_candidate_state,
    )
    from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB

    conversation_db = args.conversation_db or AGENT_CONVERSATIONS_DB
    state = inspect_candidate_state(conversation_db)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def _cmd_view_prepare(args: argparse.Namespace) -> int:
    """Zero-cost view/policy/evidence-bound prepare (estimates only)."""
    from personal_knowledge.application.knowledge.view_candidate_prepare import (
        CandidatePrepareError,
        prepare_view_candidates,
    )
    from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB

    conversation_db = args.conversation_db or AGENT_CONVERSATIONS_DB
    try:
        result = prepare_view_candidates(
            conversation_db,
            semantic_prompt_version=args.semantic_prompt_version,
            semantic_schema_version=args.semantic_schema_version,
            model=args.model,
        )
    except CandidatePrepareError as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "write": False},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


def _cmd_view_status(args: argparse.Namespace) -> int:
    """Ledger status for a view-policy run or legacy superseded audit."""
    from personal_knowledge.application.knowledge.view_candidate_prepare import (
        view_run_status,
    )
    from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB

    conversation_db = args.conversation_db or AGENT_CONVERSATIONS_DB
    if not args.run:
        from personal_knowledge.application.knowledge.delta_backlog_status import inspect_delta_backlog
        status = inspect_delta_backlog(conversation_db)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 2 if status["status"] in {"degraded", "uninitialized"} else 0
    status = view_run_status(conversation_db, args.run)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "view-baseline":
        from personal_knowledge.application.conversation.delta_baseline import baseline_command
        return baseline_command(args)

    if args.command == "view-consume":
        import sqlite3
        from personal_knowledge.application.knowledge.delta_candidate_consumer import consume_delta_candidates
        from personal_knowledge.application.knowledge.view_candidate_prepare import CandidatePrepareError
        from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB
        try:
            result = consume_delta_candidates(args.conversation_db or AGENT_CONVERSATIONS_DB,
                                              max_events=args.max_events, max_context_events=args.max_context_events)
        except (ValueError, CandidatePrepareError, sqlite3.Error) as exc:
            print(json.dumps({"ok": False, "error": str(exc), "cursor_advanced": False}), file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.command == "workflow":
        return _cmd_workflow()
    if args.command == "inspect":
        return _cmd_inspect(args)
    if args.command == "prepare":
        return _cmd_prepare(args)
    if args.command == "extract":
        return _cmd_extract(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "canonical":
        return _cmd_canonical(args)
    if args.command == "publish":
        return _cmd_publish(args)
    if args.command == "vector":
        return _cmd_vector(args)
    if args.command == "extract-gate":
        return _cmd_extract_gate(args)
    if args.command == "canary":
        return _cmd_canary(args)
    if args.command == "promote":
        return _cmd_promote(args)
    if args.command == "watermark":
        return _cmd_watermark(args)
    if args.command == "reconcile":
        return _cmd_reconcile(args)
    if args.command.startswith("lifecycle-"):
        return _cmd_lifecycle(args)
    if args.command == "history":
        return _cmd_history(args)
    if args.command == "promote-units":
        return _cmd_promote_units(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "view-inspect":
        return _cmd_view_inspect(args)
    if args.command == "view-prepare":
        return _cmd_view_prepare(args)
    if args.command == "view-status":
        return _cmd_view_status(args)

    print(f"unknown command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
