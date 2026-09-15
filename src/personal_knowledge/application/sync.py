"""Canonical product sync entry (post Phase 14–21).

Replaces day-to-day use of the legacy integrated ``rag-pipeline`` (steps 1–12).
Primary job: pull local conversation evidence from AgentsView into project DBs.

Usage::

    pk-sync conversations              # dry-run inventory + normalized + canonical
    pk-sync conversations --write      # actually publish DBs
    python -m personal_knowledge.application.sync conversations --write
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import urllib.request
from urllib.error import HTTPError, URLError

from personal_knowledge.core.project_paths import (
    AGENTSVIEW_DB,
    AGENT_CONVERSATIONS_DB,
    AI_CONTEXT_DIR,
    GOOGLE_DB,
    UNIFIED_DB,
)


TURN_SUMMARIES = AI_CONTEXT_DIR / "conversation_summaries.json"

# Plan 61-06: the sole committed conversation-delta producer is the fixed
# internal kernel route; the publisher is metadata-only by construction.
CONVERSATION_DELTA_TYPE = "conversation.delta.committed"
CONVERSATION_DELTA_ROUTE = "/internal/v1/conversation-deltas"
CONVERSATION_DELTA_HEADER = "x-pi-internal-capability"
_SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")


def _record_conversation_versions() -> list[dict]:
    from personal_knowledge.application.serving.versions import (
        record_conversation_publications,
    )

    return record_conversation_publications(UNIFIED_DB, AGENT_CONVERSATIONS_DB)


def _now_utc_iso() -> str:
    """UTC instant in the EventJournal `YYYY-MM-DDTHH:MM:SS.mmmZ` format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _kernel_delta_endpoint() -> str:
    host = os.environ.get("PI_KERNEL_HOST", "127.0.0.1")
    port = os.environ.get("PI_KERNEL_PORT", "8790")
    return f"http://{host}:{port}"


def _kernel_delta_capability() -> str | None:
    return os.environ.get("PI_KERNEL_INTERNAL_CAPABILITY") or None


def _agentsview_source_checksum() -> str | None:
    """Source -> AgentView binding checksum (AgentsView DB content checksum)."""
    from personal_knowledge.application.serving.versions import file_checksum

    try:
        if AGENTSVIEW_DB.exists():
            return file_checksum(AGENTSVIEW_DB)
    except Exception:  # noqa: BLE001 - a probe failure never blocks canonical sync
        return None
    return None


def _delta_fail_closed(reason: str) -> dict[str, object]:
    return {"published": False, "reason": reason, "event_type": CONVERSATION_DELTA_TYPE}


def _post_conversation_delta(endpoint: str, body: dict, internal_capability: str) -> tuple[int, dict]:
    url = endpoint.rstrip("/") + CONVERSATION_DELTA_ROUTE
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            CONVERSATION_DELTA_HEADER: internal_capability,
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except (ValueError, OSError):
            payload = {}
        return exc.code, payload
    except URLError:
        return 0, {}


def publish_conversation_delta_committed(
    *,
    canonical_checksum: str,
    source_checksum: str | None = None,
    watermark: str | None = None,
    publication_version: str | None = None,
    source: str = "pk-sync",
    scope: str = "agent.conversation",
    idempotency_key: str,
    occurred_at: str | None = None,
    committed: bool = True,
    endpoint: str | None = None,
    internal_capability: str | None = None,
) -> dict[str, object]:
    """Post-commit metadata-only publisher for ``conversation.delta.committed``.

    Strictly keyword-only metadata; the seam can never be handed a conversation
    body, prompt, credential, SQL statement or private path. Publishing happens
    only after canonical records and the watermark are committed and the
    observed canonical checksum equals the committed watermark; every dry-run,
    uncommitted, missing, mismatched or ambiguous pre-commit state publishes
    nothing and returns a fail-closed reason.
    """
    if committed is not True:
        return _delta_fail_closed("uncommitted_or_dry_run")
    if not isinstance(canonical_checksum, str) or not _SHA256_HEX.fullmatch(canonical_checksum):
        return _delta_fail_closed("missing_or_invalid_canonical_checksum")
    if not isinstance(watermark, str) or not _SHA256_HEX.fullmatch(watermark):
        return _delta_fail_closed("missing_or_invalid_watermark")
    if watermark != canonical_checksum:
        return _delta_fail_closed("mismatched_watermark")
    if not source_checksum:
        return _delta_fail_closed("missing_source_checksum")
    if not endpoint:
        return _delta_fail_closed("endpoint_missing")
    if not internal_capability:
        return _delta_fail_closed("internal_capability_missing")

    body = {
        "producer": source,
        "scope": scope,
        "source_checksum": source_checksum,
        "canonical_checksum": canonical_checksum,
        "watermark": watermark,
        "publication_version": publication_version or "1",
        "occurred_at": occurred_at or _now_utc_iso(),
        "idempotency_key": idempotency_key,
        "committed": True,
    }
    status, payload = _post_conversation_delta(endpoint, body, internal_capability)
    if status in (200, 201) and isinstance(payload, dict) and payload.get("ok") is True:
        return {
            "published": True,
            "status": payload.get("status", "appended"),
            "replay": bool(payload.get("replay")),
            "duplicate": bool(payload.get("duplicate")),
            "event_type": CONVERSATION_DELTA_TYPE,
            "event_id": payload.get("event_id"),
            "sequence": payload.get("sequence"),
        }
    code = ""
    if isinstance(payload, dict):
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        code = error.get("code") or ""
    return _delta_fail_closed(f"rejected:{code or status or 'transport_error'}")


def _cmd_conversations(write: bool, args) -> int:
    from personal_knowledge.application.run_pipeline import run_agentsview_stage
    from personal_knowledge.application.serving.versions import file_checksum

    # Milestone 3: live sync / background listener (stage -> incremental apply).
    # Opt-in and mutually exclusive with the v2 generation modes: mixing them
    # would silently ignore one of the two, so it fails closed instead.
    live_flags = (
        getattr(args, "live_sync", False)
        or getattr(args, "live_dry_run", False)
        or getattr(args, "watch", False)
        or getattr(args, "live_status", False)
    )
    if live_flags:
        if (args.v2_dry_run or args.v2_shadow or args.v2_activate or
                args.v2_native or args.v2_native_dry_run):

            print(
                "[error] --live-* / --watch cannot be combined with a --v2-* mode",
                file=sys.stderr,
            )
            return 2
        from personal_knowledge.application.conversation.watch import (
            cmd_conversations_live,
        )

        return cmd_conversations_live(args)

    # Phase 62-04: explicit v2 dry-run / shadow / activation. These modes are
    # opt-in and never change the default canonical service behavior (62-04
    # Task 3: default stays as-is until Plan 62-08).
    if (args.v2_dry_run or args.v2_shadow or args.v2_activate or
            args.v2_native or args.v2_native_dry_run):
        from personal_knowledge.application.conversation.v2_sync import (
            cmd_conversations_v2,
        )

        return cmd_conversations_v2(args)

    ok = run_agentsview_stage(write=write)
    if not ok:
        return 1
    publications = _record_conversation_versions() if write else []
    delta = None
    if write:
        # Strictly post-commit: canonical records and watermark are committed by
        # _record_conversation_versions, and publishing requires the observed
        # canonical checksum to equal the committed watermark.
        observed_checksum = file_checksum(AGENT_CONVERSATIONS_DB)
        committed_watermark = publications[0]["checksum"] if publications else observed_checksum
        try:
            delta = publish_conversation_delta_committed(
                canonical_checksum=observed_checksum,
                source_checksum=_agentsview_source_checksum(),
                watermark=committed_watermark,
                publication_version=f"{_now_utc_iso()}#1",
                source="pk-sync",
                scope="agent.conversation",
                idempotency_key=f"pk-sync-conversations-{observed_checksum}",
                occurred_at=_now_utc_iso(),
                committed=True,
                endpoint=_kernel_delta_endpoint(),
                internal_capability=_kernel_delta_capability(),
            )
        except Exception as exc:  # noqa: BLE001 - kernel may be offline; canonical sync is committed
            delta = {"published": False, "reason": f"delta_publish_failed:{type(exc).__name__}"}
        if not delta.get("published"):
            print(f"[warn] conversation delta not published: {delta.get('reason')}", file=sys.stderr)
    mode = "write" if write else "dry-run"
    print(f"\n[done] pk-sync conversations ({mode}) finished.")
    print("  SSOT: data/canonical/agent/structured/db/agent_conversations.sqlite")
    print("  Next (optional): pk-ku inspect → prepare → extract — not part of this command.")
    if publications:
        print(f"  Versions: {sum(int(x['created']) for x in publications)} new / {len(publications)} recorded")
    if delta and delta.get("published"):
        print(f"  Delta: conversation.delta.committed ({delta.get('status')}, event {delta.get('event_id')})")
    return 0


def _cmd_turns(write: bool) -> int:
    from personal_knowledge.application.conversation.build_conversation_vector_store import (
        COLLECTION_NAME,
        build,
    )
    from personal_knowledge.application.serving.versions import file_checksum, record_publication

    stats = build(write=write)
    publications: list[dict] = []
    if write:
        if not TURN_SUMMARIES.exists():
            print(f"[error] turn publication missing: {TURN_SUMMARIES}", file=sys.stderr)
            return 1
        if not stats.get("sample_search_ok"):
            print("[error] turn publication verification failed; versions not advanced", file=sys.stderr)
            return 1
        checksum = file_checksum(TURN_SUMMARIES)
        summary = record_publication(
            UNIFIED_DB,
            registry_id="s.turn_summary",
            version=checksum,
            checksum=checksum,
            location_kind="json_artifact",
            location_ref=str(TURN_SUMMARIES),
            source_key="canonical_message",
            watermark_value=checksum,
            metadata={"turn_count": stats.get("total_turns", stats.get("units_total", 0))},
        )
        vector = record_publication(
            UNIFIED_DB,
            registry_id="r.turn_vector",
            version=checksum,
            checksum=checksum,
            location_kind="chroma_collection",
            location_ref=COLLECTION_NAME,
            source_key="turn_summary",
            watermark_value=checksum,
            evidence_version_id=summary["artifact_version_id"],
            metadata={"count": stats.get("final_collection_count", 0)},
        )
        publications = [summary, vector]
    print(json.dumps({"command": "turns", "mode": "write" if write else "dry-run", "stats": stats, "publications": publications}, ensure_ascii=False, indent=2))
    return 0


def _cmd_google(write: bool, db_path: Path) -> int:
    from personal_knowledge.application.build_google_light_assertions import build as build_assertions
    from personal_knowledge.application.build_google_normalized_events import build as build_normalized
    from personal_knowledge.application.serving.versions import record_publication

    normalized = build_normalized(db_path, write=write)
    assertions, _ = build_assertions(db_path, write=write)
    publications: list[dict] = []
    if write:
        if not assertions.gate_passed or not assertions.promoted:
            print("[error] Google privacy/lifecycle gate failed; versions not advanced", file=sys.stderr)
            return 1
        norm = record_publication(
            UNIFIED_DB,
            registry_id="d.google_normalized",
            version=normalized.dataset_hash,
            checksum=normalized.dataset_hash,
            location_kind="sqlite_store",
            location_ref=f"{db_path}#normalized_events",
            source_key="google_activities",
            watermark_value=normalized.input_hash,
            producer_run_id=normalized.run_id,
            metadata={"count": normalized.after_count},
        )
        assertion = record_publication(
            UNIFIED_DB,
            registry_id="s.google_assertion",
            version=assertions.dataset_hash,
            checksum=assertions.dataset_hash,
            location_kind="sqlite_table",
            location_ref=f"{db_path}#google_light_assertions",
            source_key="google_normalized",
            watermark_value=assertions.input_hash,
            producer_run_id=assertions.run_id,
            evidence_version_id=norm["artifact_version_id"],
            metadata={"count": assertions.assertions},
        )
        publications = [norm, assertion]
    print(json.dumps({"command": "google", "mode": "write" if write else "dry-run", "normalized": normalized.to_dict(), "assertions": assertions.to_dict(), "publications": publications}, ensure_ascii=False, indent=2))
    return 0


def _cmd_status(json_output: bool) -> int:
    from personal_knowledge.application.knowledge.doctor_ku import _default_collection_inspector
    from personal_knowledge.application.serving.versions import file_checksum, json_checksum, publication_status

    report = publication_status(UNIFIED_DB)
    current: dict[str, str] = {}
    for registry_id, path in {
        "d.canonical_conversation": AGENT_CONVERSATIONS_DB,
        "d.canonical_message": AGENT_CONVERSATIONS_DB,
        "s.turn_summary": TURN_SUMMARIES,
    }.items():
        if path.exists():
            current[registry_id] = file_checksum(path)
    for registry_id, checksum in current.items():
        item = (report.get("artifacts") or {}).get(registry_id) or {}
        item["current_checksum"] = checksum
        item["drift"] = bool(item.get("checksum") and item["checksum"] != checksum)
    if TURN_SUMMARIES.exists():
        turn_source = file_checksum(TURN_SUMMARIES)
        item = (report.get("artifacts") or {}).get("r.turn_vector") or {}
        item["current_source_checksum"] = turn_source
        item["drift"] = bool(item.get("watermark_value") and item["watermark_value"] != turn_source)
    if GOOGLE_DB.exists():
        con = sqlite3.connect(f"file:{GOOGLE_DB.resolve().as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            runs = {str(row["run_type"]): dict(row) for row in con.execute("SELECT run_type,input_hash,dataset_hash FROM google_structure_runs WHERE status='current'")}
        except sqlite3.Error:
            runs = {}
        con.close()
        for registry_id, run_type in (("d.google_normalized", "normalized_events"), ("s.google_assertion", "light_assertions")):
            item = (report.get("artifacts") or {}).get(registry_id) or {}
            run = runs.get(run_type) or {}
            item["current_checksum"] = run.get("dataset_hash")
            item["current_source_checksum"] = run.get("input_hash")
            item["drift"] = bool(
                item.get("checksum") and (
                    item["checksum"] != run.get("dataset_hash")
                    or item.get("watermark_value") != run.get("input_hash")
                )
            )
    if UNIFIED_DB.exists():
        con = sqlite3.connect(f"file:{UNIFIED_DB.resolve().as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            canonical_ids = [str(row[0]) for row in con.execute("SELECT canonical_unit_id FROM canonical_knowledge_units WHERE status='current' ORDER BY canonical_unit_id")]
            index = con.execute("SELECT collection_name,checksum FROM knowledge_index_versions WHERE status='active' ORDER BY activated_at DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            canonical_ids, index = [], None
        con.close()
        canonical = (report.get("artifacts") or {}).get("s.knowledge_unit") or {}
        canonical_current = json_checksum(canonical_ids) if canonical_ids else ""
        canonical["current_checksum"] = canonical_current
        canonical["drift"] = bool(canonical.get("checksum") and canonical["checksum"] != canonical_current)
        retrieval = (report.get("artifacts") or {}).get("r.knowledge_index") or {}
        if index is not None:
            try:
                actual = dict(_default_collection_inspector(str(index["collection_name"])))
            except Exception as exc:  # noqa: BLE001
                actual = {"exists": False, "error": str(exc), "checksum": ""}
            retrieval["current_checksum"] = actual.get("checksum")
            retrieval["current_collection"] = str(index["collection_name"])
            retrieval["drift"] = bool(
                retrieval.get("checksum") and (
                    retrieval["checksum"] != actual.get("checksum")
                    or retrieval.get("location_ref") != str(index["collection_name"])
                )
            )
    drift = sorted(aid for aid, item in (report.get("artifacts") or {}).items() if item.get("drift"))
    report["drift"] = drift
    report["ok"] = bool(report.get("ok")) and not drift
    report["sources"] = {"conversation": str(AGENT_CONVERSATIONS_DB), "turns": str(TURN_SUMMARIES), "google": str(GOOGLE_DB), "knowledge": str(UNIFIED_DB)}
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"pk-sync status: {'OK' if report.get('ok') else 'NOT READY'}")
        for aid, item in (report.get("artifacts") or {}).items():
            print(f"  {aid}: {item.get('version') or '-'} drift={item.get('drift', False)}")
    return 0 if report.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pk-sync",
        description=(
            "Product data sync (canonical paths). "
            "Does NOT run legacy integrated steps 1–12 (personal_events / memory batch)."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    conv = sub.add_parser(
        "conversations",
        help="AgentsView → normalized → canonical conversation SSOT",
    )
    conv.add_argument(
        "--write",
        action="store_true",
        help="Publish normalized + canonical DBs (default is dry-run)",
    )
    # Phase 62-04: additive v2 orchestration flags. The default command
    # behavior is unchanged; these flags are explicit and opt-in only.
    from personal_knowledge.application.conversation.v2_sync import (
        add_conversations_v2_args,
    )

    add_conversations_v2_args(conv)

    # Milestone 3: live-sync / watch flags (additive, opt-in).
    from personal_knowledge.application.conversation.watch import (
        add_conversation_watch_args,
    )

    add_conversation_watch_args(conv)

    turns = sub.add_parser("turns", help="Turn summaries → conversation_turns vector publication")
    turns.add_argument("--write", action="store_true", help="Publish after build verification (default dry-run)")
    turns.add_argument("--dry-run", action="store_true")

    google = sub.add_parser("google", help="Google normalized events + privacy-gated assertions")
    google.add_argument("--write", action="store_true", help="Publish after privacy gate (default dry-run)")
    google.add_argument("--dry-run", action="store_true")
    google.add_argument("--db", type=Path, default=GOOGLE_DB)

    status = sub.add_parser("status", help="Read-only publication version/watermark status")
    status.add_argument("--json", action="store_true", help="Emit JSON")
    conv.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run (default when --write is absent)",
    )

    sub.add_parser(
        "help-legacy",
        help="Show how to invoke the retired integrated pipeline if ever needed",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "help-legacy":
        print(
            "Legacy integrated pipeline (personal_system / memory / PE vectors) is retired\n"
            "from product use. Modules remain in application/* for forensics only.\n\n"
            "Emergency re-run (not recommended):\n"
            "  set PK_ALLOW_LEGACY_PIPELINE=1\n"
            "  python -m personal_knowledge.application.run_pipeline --legacy-integrated --dry-run\n"
        )
        return 0

    if args.command == "conversations":
        write = bool(args.write)
        return _cmd_conversations(write=write, args=args)

    if args.command == "turns":
        return _cmd_turns(write=bool(args.write))

    if args.command == "google":
        return _cmd_google(write=bool(args.write), db_path=args.db)

    if args.command == "status":
        return _cmd_status(json_output=bool(args.json))

    print(f"unknown command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
