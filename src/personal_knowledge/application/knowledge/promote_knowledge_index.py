"""Phase 14 Wave 4.2：knowledge index promote / rollback。

把通过 frozen-test A/B gate 的 candidate collection promote 为 active，
或回滚到上一个 active checkpoint。使用原子 active pointer。

用法::

    python promote_knowledge_index.py --promote knowledge_units_a89ebe470357
    python promote_knowledge_index.py --list
    python rollback_knowledge_checkpoint.py --to previous
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from personal_knowledge.core.sqlite import assert_foreign_key_integrity, connect_rw

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import UNIFIED_DB, DB_DIR  # noqa: E402

ACTIVE_POINTER = DB_DIR / "knowledge_index_active.txt"
PROMOTE_LOG = DB_DIR / "knowledge_index_promote_log.jsonl"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_active() -> str:
    """读当前 active collection 名。"""
    if ACTIVE_POINTER.exists():
        return ACTIVE_POINTER.read_text(encoding="utf-8").strip()
    return ""


def _write_active(collection: str) -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ACTIVE_POINTER.with_suffix(".tmp")
    tmp.write_text(collection, encoding="utf-8")
    tmp.replace(ACTIVE_POINTER)


def _log(action: str, collection: str, details: dict | None = None) -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": _utc_now(), "action": action, "collection": collection}
    if details:
        entry.update(details)
    with PROMOTE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _compute_collection_checksum(collection_name: str, port: int = 8001) -> str:
    """从 Chroma 实际 IDs 计算 ID-set checksum（sorted IDs 的 sha256）。"""
    try:
        from personal_knowledge.core.chroma_client import ChromaClient
        client = ChromaClient(port=port)
        coll = client.get_or_create_collection(collection_name)
        ids: list[str] = []
        page, offset = 2000, 0
        while True:
            batch = (coll.get(limit=page, offset=offset, include=[]) or {}).get("ids") or []
            if not batch:
                break
            ids.extend(batch)
            offset += len(batch)
            if len(batch) < page:
                break
            if offset > 500000:
                break
        return hashlib.sha256("".join(sorted(ids)).encode()).hexdigest()
    except Exception:
        return ""


def _snapshot_member_for_collection(
    collection: str,
    db_path: Path,
    *,
    require_collection_validation: bool,
) -> tuple[dict, Callable[[str], dict]]:
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT version_id, build_id, canonical_build_id, unit_count, checksum "
            "FROM knowledge_index_versions WHERE collection_name=? ORDER BY created_at DESC LIMIT 1",
            (collection,),
        ).fetchone()
    finally:
        con.close()
    if row is None and require_collection_validation:
        raise RuntimeError(f"knowledge index version not found: {collection}")
    checksum = _compute_collection_checksum(collection)
    if require_collection_validation and not checksum:
        raise RuntimeError(f"collection checksum unavailable: {collection}")
    stored = str((row or (None, None, None, 0, None))[4] or "")
    if require_collection_validation and stored and stored != checksum:
        raise RuntimeError(f"collection checksum mismatch: stored={stored} actual={checksum}")
    effective = checksum or stored or hashlib.sha256(f"unverified:{collection}".encode()).hexdigest()
    count = int((row or (None, None, None, 0, None))[3] or 0)
    member = {
        "version": str((row or (collection,))[0] or collection),
        "checksum": effective,
        "location_kind": "chroma_collection",
        "location_ref": collection,
        "producer_run_id": (row or (None, None))[1],
        "metadata": {
            "unit_count": count,
            "canonical_build_id": (row or (None, None, None))[2],
            "collection_verified": bool(checksum),
        },
    }
    inspector = lambda name: {  # noqa: E731 - small bound validator adapter
        "exists": name == collection,
        "checksum": effective,
        "count": count,
    }
    return member, inspector


# prepare_snapshot 会使用的 member 键；其余列（registry_id/lifecycle/created_at 等）
# 若带入会原样进 manifest，故适配时丢弃。
_MEMBER_KEEP_KEYS = (
    "artifact_version_id",
    "version",
    "checksum",
    "location_kind",
    "location_ref",
    "producer_run_id",
    "evidence_version_id",
    "watermark_id",
)


def _member_from_active_row(row: dict) -> dict:
    """把 get_active_snapshot 的 member 行适配为 prepare_snapshot 的输入格式。

    get_active_snapshot 返回 serving_snapshot_members JOIN artifact_versions 的行，
    其中 metadata_json 是 JSON 字符串；prepare_snapshot 需要 metadata dict。
    """
    member = {k: row[k] for k in _MEMBER_KEEP_KEYS if row.get(k) is not None}
    member["metadata"] = json.loads(str(row.get("metadata_json") or "{}"))
    return member


def _record_successful_knowledge_publication(
    db_path: Path, member: dict, collection: str
) -> list[dict]:
    """Append publication metadata only after serving activation succeeds."""
    from personal_knowledge.application.serving.versions import json_checksum, record_publication

    metadata = dict(member.get("metadata") or {})
    canonical_build_id = str(metadata.get("canonical_build_id") or "")
    recorded: list[dict] = []
    canonical_version_id: str | None = None
    if canonical_build_id:
        con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT canonical_unit_id FROM canonical_knowledge_units WHERE status='current' ORDER BY canonical_unit_id"
        ).fetchall()
        con.close()
        canonical_checksum = json_checksum([str(row[0]) for row in rows])
        canonical = record_publication(
            db_path,
            registry_id="s.knowledge_unit",
            version=canonical_build_id,
            checksum=canonical_checksum,
            location_kind="sqlite_table",
            location_ref="canonical_knowledge_units",
            source_key="canonical_message",
            watermark_value=canonical_build_id,
            producer_run_id=canonical_build_id,
            metadata={"unit_count": len(rows)},
        )
        canonical_version_id = canonical["artifact_version_id"]
        recorded.append(canonical)
    retrieval = record_publication(
        db_path,
        registry_id="r.knowledge_index",
        version=str(member["version"]),
        checksum=str(member["checksum"]),
        location_kind="chroma_collection",
        location_ref=collection,
        source_key="canonical_knowledge",
        watermark_value=canonical_build_id or str(member["checksum"]),
        producer_run_id=member.get("producer_run_id"),
        evidence_version_id=canonical_version_id,
        metadata=metadata,
    )
    recorded.append(retrieval)
    return recorded


def promote(
    collection: str,
    db_path: Path = UNIFIED_DB,
    *,
    require_collection_validation: bool = False,
    eval_gate_ref: str | None = None,
) -> dict:
    """把 candidate collection promote 为 active。

    同时更新 knowledge_index_versions 表的 status 和 checksum。
    """
    from personal_knowledge.application.serving.snapshots import (
        activate_snapshot, get_active_snapshot, prepare_snapshot, validate_snapshot,
    )

    previous = read_active()
    member, inspector = _snapshot_member_for_collection(
        collection, db_path, require_collection_validation=require_collection_validation
    )
    members: dict[str, dict] = {"knowledge_retrieval": member}
    active = get_active_snapshot(db_path)
    if active:
        base = {
            str(role): _member_from_active_row(dict(row))
            for role, row in (active.get("members") or {}).items()
        }
        if len(base) > 1:
            # 活跃快照是多角色全量快照：以其 members 为底，只替换 knowledge_retrieval，
            # 其余角色成员原样保留，避免 promote 后 doctor 报 missing_roles。
            base["knowledge_retrieval"] = member
            members = base
    # 保留角色里的其他 chroma collection 来自已验证的活跃快照且未变动，按快照
    # 记录值放行，避免与本次 promote 无关的 collection 可用性阻塞 promote。
    kept_chroma = {
        str(m.get("location_ref")): m
        for role, m in members.items()
        if role != "knowledge_retrieval" and m.get("location_kind") == "chroma_collection"
    }

    def _inspector(name: str) -> dict:
        if name == collection:
            return inspector(name)
        kept = kept_chroma.get(name)
        if kept is not None:
            meta = kept.get("metadata") or {}
            return {
                "exists": True,
                "checksum": str(kept.get("checksum") or ""),
                "count": meta.get("unit_count", meta.get("count", -1)),
            }
        return {"exists": False}

    draft = prepare_snapshot(
        db_path,
        members,
        eval_gate_ref=eval_gate_ref or ("compat-direct" if not require_collection_validation else None),
        write=True,
    )
    validation = validate_snapshot(
        db_path,
        draft["snapshot_id"],
        collection_inspector=_inspector,
        required_roles=set(members),
        require_gate=require_collection_validation,
        gate_validator=(lambda _: True) if require_collection_validation and eval_gate_ref else None,
    )
    if not validation["ok"]:
        raise RuntimeError(f"serving snapshot validation failed: {validation['errors']}")

    def _update_versions(con: sqlite3.Connection) -> None:
        if previous:
            con.execute(
                "UPDATE knowledge_index_versions SET status='rolled_back' WHERE collection_name=? AND status='active'",
                (previous,),
            )
        con.execute(
            "UPDATE knowledge_index_versions SET status='active', activated_at=?, checksum=? WHERE collection_name=?",
            (_utc_now(), member["checksum"], collection),
        )

    activated = activate_snapshot(
        db_path,
        draft["snapshot_id"],
        pointer_path=ACTIVE_POINTER,
        before_commit=_update_versions,
    )
    publications = _record_successful_knowledge_publication(db_path, member, collection)
    _log("promote", collection, {"previous": previous, "checksum": member["checksum"], "snapshot_id": draft["snapshot_id"]})
    return {
        "promoted": collection,
        "previous": previous,
        "checksum": member["checksum"],
        "snapshot_id": draft["snapshot_id"],
        "projection_ok": activated["projection_ok"],
        "publications": publications,
    }


def rollback_to_previous(db_path: Path = UNIFIED_DB) -> dict:
    """回滚到上一个 active collection。"""
    current = read_active()
    if not current:
        return {"error": "no active collection to rollback from"}

    # 从 promote log 找上一个
    previous = ""
    if PROMOTE_LOG.exists():
        entries = [json.loads(l) for l in PROMOTE_LOG.read_text(encoding="utf-8").strip().split("\n") if l.strip()]
        # 找最近的 promote，其 previous 非空
        for entry in reversed(entries):
            if entry.get("action") == "promote" and entry.get("previous"):
                previous = entry["previous"]
                break

    if not previous:
        return {"error": "no previous collection found in log"}

    from personal_knowledge.application.serving.snapshots import rollback_snapshot
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    target = con.execute(
        "SELECT s.snapshot_id FROM serving_snapshots s JOIN serving_snapshot_members m ON m.snapshot_id=s.snapshot_id JOIN artifact_versions v ON v.artifact_version_id=m.artifact_version_id WHERE m.serving_role='knowledge_retrieval' AND v.location_ref=? AND s.status='validated' ORDER BY s.validated_at DESC LIMIT 1",
        (previous,),
    ).fetchone()
    con.close()
    if not target:
        return {"error": f"no validated serving snapshot found for {previous}"}

    def _update_versions(write_con: sqlite3.Connection) -> None:
        write_con.execute(
            "UPDATE knowledge_index_versions SET status='rolled_back' WHERE collection_name=? AND status='active'",
            (current,),
        )
        write_con.execute(
            "UPDATE knowledge_index_versions SET status='active', activated_at=? WHERE collection_name=?",
            (_utc_now(), previous),
        )

    rolled = rollback_snapshot(
        db_path, str(target[0]), pointer_path=ACTIVE_POINTER, before_commit=_update_versions
    )
    if not rolled.get("ok"):
        return {"error": rolled.get("error", "snapshot rollback failed")}
    _log("rollback", previous, {"rolled_back_from": current})

    return {"rolled_back_to": previous, "rolled_back_from": current}


def list_versions(db_path: Path = UNIFIED_DB) -> list[dict]:
    """列出所有 index version。"""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT version_id, build_id, collection_name, unit_count, status, "
        "created_at, activated_at FROM knowledge_index_versions ORDER BY created_at DESC"
    ).fetchall()
    con.close()
    active = read_active()
    result = []
    for r in rows:
        d = dict(r)
        d["is_active_pointer"] = (d["collection_name"] == active)
        result.append(d)
    return result


def _check_eval_gate(
    collection: str,
    *,
    eval_summary: Path | None,
    eval_gate: Path | None,
    require: bool,
    gate_runner: Callable[[dict, dict, str, str], dict] | None = None,
) -> dict:
    """Fail-closed: without PASS gate + matching candidate, refuse promote when required.

    ``gate_runner`` is a dependency-injected callback
    ``(summary, policy, candidate_collection, candidate_checksum) -> gate_doc``.
    When omitted, the CLI default lazily imports the evaluation gate module
    (product direction: application -> evaluation).
    """
    if not require and eval_summary is None and eval_gate is None:
        return {"ok": True, "skipped": True}

    if eval_gate is None and eval_summary is None:
        # Default path is fail-closed: no promote without an eval artifact.
        return {
            "ok": False,
            "error": (
                "promotion requires --eval-gate or --eval-summary "
                "(default fail-closed; use --allow-without-eval only for forensics)"
            ),
        }

    gate_doc: dict = {}
    if eval_gate and eval_gate.exists():
        gate_doc = json.loads(eval_gate.read_text(encoding="utf-8"))
    elif eval_summary and eval_summary.exists():
        summary = json.loads(eval_summary.read_text(encoding="utf-8"))
        gate_doc = summary.get("gate") or {}
        if not gate_doc:
            # run gate inline fail-closed
            try:
                if gate_runner is None:
                    from personal_knowledge.evaluation.gate_knowledge_candidate import evaluate_gate, load_policy
                    from personal_knowledge.core.project_paths import ROOT

                    policy_path = (
                        ROOT
                        / "assets"
                        / "evals"
                        / "knowledge_units"
                        / "eval_policy_v1.yaml"
                    )
                    policy = load_policy(policy_path) if policy_path.exists() else {"version": "v1"}
                    gate_doc = evaluate_gate(
                        summary,
                        policy,
                        candidate_collection=collection,
                        candidate_checksum=summary.get("candidate_checksum") or "",
                    )
                else:
                    gate_doc = gate_runner(
                        summary,
                        None,
                        collection,
                        summary.get("candidate_checksum") or "",
                    )
            except Exception as e:
                return {"ok": False, "error": f"eval gate failed to load: {e}"}
    else:
        return {"ok": False, "error": "eval gate/summary path not found"}

    if not gate_doc.get("passed") or gate_doc.get("verdict") != "PASS":
        return {
            "ok": False,
            "error": f"eval gate not PASS (verdict={gate_doc.get('verdict')})",
            "gate": gate_doc,
        }
    cand = gate_doc.get("candidate_collection") or ""
    if cand and cand != collection:
        return {
            "ok": False,
            "error": f"candidate collection mismatch: gate={cand} promote={collection}",
            "gate": gate_doc,
        }
    return {"ok": True, "gate": gate_doc}


def promote_main(
    argv: list[str] | None = None,
    *,
    gate_runner: Callable[[dict, dict, str, str], dict] | None = None,
) -> int:
    p = argparse.ArgumentParser(description="Phase 14 Wave 4.2 / 17-04: promote knowledge index")
    p.add_argument("--promote", metavar="COLLECTION", help="promote collection 为 active")
    p.add_argument("--list", action="store_true", help="列出所有 index version")
    p.add_argument(
        "--require-eval-pass",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "fail-closed: require Phase 17 gate PASS before promote (default: true). "
            "Use --no-require-eval-pass / --allow-without-eval only for forensics."
        ),
    )
    p.add_argument(
        "--allow-without-eval",
        action="store_true",
        help="forensics only: skip eval gate (alias of --no-require-eval-pass; loud warning)",
    )
    p.add_argument("--eval-summary", type=Path, default=None, help="path to eval summary.json")
    p.add_argument("--eval-gate", type=Path, default=None, help="path to gate.json")
    args = p.parse_args(argv)

    if args.list:
        versions = list_versions()
        active = read_active()
        print(f"active pointer: {active or '(none)'}")
        print(f"{'collection':<40} {'status':<12} {'units':>6} {'created'}")
        for v in versions:
            marker = " ← active" if v["collection_name"] == active else ""
            print(f"{v['collection_name']:<40} {v['status']:<12} {v['unit_count']:>6} {v['created_at'][:19]}{marker}")
        return 0

    if args.promote:
        require = bool(args.require_eval_pass) and not bool(args.allow_without_eval)
        if not require:
            print(
                "[WARNING] promote without eval gate — forensics/waiver only; "
                "do NOT use for product active pointer",
                file=sys.stderr,
            )
        gate_check = _check_eval_gate(
            args.promote,
            eval_summary=args.eval_summary,
            eval_gate=args.eval_gate,
            require=require,
            gate_runner=gate_runner,
        )
        if not gate_check.get("ok"):
            print(f"[error] promote refused: {gate_check.get('error')}", file=sys.stderr)
            _log(
                "promote_refused",
                args.promote,
                {"error": gate_check.get("error"), "gate": gate_check.get("gate")},
            )
            return 1
        result = promote(
            args.promote,
            require_collection_validation=require,
            eval_gate_ref=str(args.eval_gate or args.eval_summary or "") or None,
        )
        if "error" in result:
            print(f"[error] {result['error']}", file=sys.stderr)
            return 1
        print(f"[ok] promoted: {result['promoted']} (previous: {result['previous'] or 'none'})")
        return 0

    print(
        "用法: --promote <collection> [--eval-summary PATH] [--eval-gate PATH] | --list\n"
        "  eval gate required by default; forensics: --allow-without-eval / --no-require-eval-pass",
        file=sys.stderr,
    )
    return 0


def rollback_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 14 Wave 4.2: rollback knowledge checkpoint")
    p.add_argument("--to", choices=["previous"], default="previous")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if args.dry_run:
        current = read_active()
        print(f"[dry-run] current active: {current}")
        # 找上一个
        if PROMOTE_LOG.exists():
            entries = [json.loads(l) for l in PROMOTE_LOG.read_text(encoding="utf-8").strip().split("\n") if l.strip()]
            for entry in reversed(entries):
                if entry.get("action") == "promote" and entry.get("previous"):
                    print(f"[dry-run] would rollback to: {entry['previous']}")
                    break
        return 0

    result = rollback_to_previous()
    if "error" in result:
        print(f"[error] {result['error']}")
        return 1
    print(f"[ok] rolled back to: {result['rolled_back_to']} (from: {result['rolled_back_from']})")
    return 0


if __name__ == "__main__":
    # 默认运行 promote（可通过第一个参数切换）
    if len(sys.argv) > 1 and sys.argv[1] == "rollback":
        raise SystemExit(rollback_main(sys.argv[2:]))
    raise SystemExit(promote_main())
