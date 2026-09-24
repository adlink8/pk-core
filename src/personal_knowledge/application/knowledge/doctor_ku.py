"""Phase 22-04: read-only product health checks for KU daily ops.

Never promotes, advances watermark, or mutates knowledge rows.

Usage::

    python -m personal_knowledge.application.knowledge.doctor_ku
    pk-ku doctor
    pk-ku doctor --json
    pk-ku doctor --skip-ports
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sqlite3
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from personal_knowledge.core.project_paths import (
    AGENT_CONVERSATIONS_DB,
    AI_CONTEXT_DIR,
    KNOWLEDGE_ACTIVE_POINTER,
    PACKAGE_DIR,
    ROOT,
    SRC_DIR,
    UNIFIED_DB,
)

# Phase 41 Plan 03 (D-06)：覆盖矩阵历史快照（count/hash-only，隐私安全）。
COVERAGE_SNAPSHOT_PATH = AI_CONTEXT_DIR / "ku_coverage_latest.json"

# Ports used by product REST / MCP (warn-only when down).
DEFAULT_HEALTH_ENDPOINTS: tuple[tuple[str, int, str], ...] = (
    ("rest_api", 8000, "http://127.0.0.1:8000/health"),
    ("mcp_app", 8789, "http://127.0.0.1:8789/health"),
)

CRITICAL_CHECK_IDS = frozenset(
    {
        "import_personal_knowledge",
        "unified_db",
        "active_pointer",
        "artifact_registry",
        "serving_snapshot",
        "snapshot_pointer_parity",
        "evidence_resolver",
        "source_watermarks",
    }
)

WATERMARK_ROLES = frozenset(
    {
        "canonical_conversation", "canonical_message", "turn_summary",
        "google_normalized", "google_assertion", "canonical_knowledge",
        "turn_retrieval", "knowledge_retrieval",
    }
)


@dataclass
class CheckResult:
    id: str
    ok: bool
    severity: str  # critical | warn | info
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoctorReport:
    ok: bool
    exit_code: int
    checks: list[CheckResult] = field(default_factory=list)
    facade: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, int] = field(default_factory=dict)
    paths: dict[str, str] = field(default_factory=dict)
    note: str = (
        "read-only doctor: no promote, no watermark --write, no knowledge DELETE"
    )


def _check_import() -> CheckResult:
    try:
        import personal_knowledge  # noqa: F401

        pk_file = getattr(personal_knowledge, "__file__", None) or ""
        return CheckResult(
            id="import_personal_knowledge",
            ok=True,
            severity="critical",
            message="personal_knowledge importable",
            detail={
                "module_file": pk_file,
                "pythonpath_has_src": str(SRC_DIR) in sys.path
                or str(SRC_DIR.resolve()) in {str(Path(p).resolve()) for p in sys.path if p},
                "sys_path_head": [p for p in sys.path[:5] if p],
            },
        )
    except Exception as exc:  # noqa: BLE001 — surface any import failure
        return CheckResult(
            id="import_personal_knowledge",
            ok=False,
            severity="critical",
            message=f"cannot import personal_knowledge: {exc}",
            detail={"error": str(exc), "hint": f'Set PYTHONPATH="{SRC_DIR}"'},
        )


def _check_path_exists(
    check_id: str,
    path: Path,
    *,
    severity: str,
    label: str,
) -> CheckResult:
    exists = path.exists()
    return CheckResult(
        id=check_id,
        ok=exists,
        severity=severity,
        message=f"{label} {'present' if exists else 'MISSING'}: {path}",
        detail={"path": str(path), "exists": exists},
    )


def _check_active_pointer(pointer_path: Path) -> CheckResult:
    if not pointer_path.exists():
        return CheckResult(
            id="active_pointer",
            ok=False,
            severity="critical",
            message=f"active pointer missing: {pointer_path}",
            detail={"path": str(pointer_path), "exists": False},
        )
    try:
        raw = pointer_path.read_text(encoding="utf-8")
        name = raw.strip()
    except OSError as exc:
        return CheckResult(
            id="active_pointer",
            ok=False,
            severity="critical",
            message=f"active pointer unreadable: {exc}",
            detail={"path": str(pointer_path), "error": str(exc)},
        )
    if not name:
        return CheckResult(
            id="active_pointer",
            ok=False,
            severity="critical",
            message="active pointer is empty",
            detail={"path": str(pointer_path), "collection": ""},
        )
    return CheckResult(
        id="active_pointer",
        ok=True,
        severity="critical",
        message=f"active collection: {name}",
        detail={"path": str(pointer_path), "collection": name},
    )


def _check_artifact_registry(registry_path: Path | None = None) -> CheckResult:
    try:
        from personal_knowledge.governance.artifact_registry import DEFAULT_REGISTRY, registry_report
        report = registry_report(registry_path or DEFAULT_REGISTRY)
        return CheckResult(
            id="artifact_registry",
            ok=bool(report["ok"]),
            severity="critical",
            message="artifact registry valid" if report["ok"] else "artifact registry invalid",
            detail=report,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("artifact_registry", False, "critical", f"artifact registry check failed: {exc}", {"error": str(exc)})


def _default_collection_inspector(name: str) -> Mapping[str, Any]:
    from personal_knowledge.application.knowledge.promote_knowledge_index import _compute_collection_checksum
    from personal_knowledge.core.chroma_client import ChromaClient

    client = ChromaClient(port=8001)
    existing = {str(row["name"]) for row in client.list_collections()}
    if name not in existing:
        return {"exists": False, "count": -1, "checksum": ""}
    collection = client.get_or_create_collection(name)
    return {"exists": True, "count": int(collection.count()), "checksum": _compute_collection_checksum(name)}


def _check_serving_snapshot(
    db_path: Path,
    *,
    collection_inspector: Callable[[str], Mapping[str, Any]] | None = None,
) -> CheckResult:
    errors: list[str] = []
    detail: dict[str, Any] = {"db": str(db_path)}
    try:
        from personal_knowledge.application.serving.snapshots import canonical_json, manifest_hash
        from personal_knowledge.governance.artifact_registry import load_registry

        con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        required_tables = {"serving_authority", "serving_snapshots", "serving_snapshot_members", "artifact_versions", "artifact_registry_entries", "source_watermarks"}
        present = {str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing_tables = sorted(required_tables - present)
        if missing_tables:
            con.close()
            return CheckResult("serving_snapshot", False, "critical", "serving schema incomplete", {**detail, "errors": ["missing_tables:" + ",".join(missing_tables)]})
        snap = con.execute(
            "SELECT s.* FROM serving_authority a JOIN serving_snapshots s ON s.snapshot_id=a.active_snapshot_id WHERE a.singleton_id=1"
        ).fetchone()
        if snap is None:
            con.close()
            return CheckResult("serving_snapshot", False, "critical", "no active serving snapshot", {**detail, "errors": ["no_active_snapshot"]})
        manifest = json.loads(str(snap["manifest_json"]))
        if manifest_hash(manifest) != str(snap["manifest_hash"]):
            errors.append("manifest_hash_mismatch")
        if str(snap["status"]) != "validated":
            errors.append("snapshot_not_validated")
        rows = con.execute(
            "SELECT m.serving_role,m.watermark_id,v.*,r.authority_role,r.definition_hash FROM serving_snapshot_members m "
            "JOIN artifact_versions v ON v.artifact_version_id=m.artifact_version_id "
            "JOIN artifact_registry_entries r ON r.registry_id=v.registry_id WHERE m.snapshot_id=? ORDER BY m.serving_role",
            (snap["snapshot_id"],),
        ).fetchall()
        roles = {str(row["serving_role"]) for row in rows}
        registry_doc = load_registry()
        required = set(registry_doc.get("required_serving_roles") or [])
        definitions = {str(item["authority_role"]): item for item in registry_doc.get("artifacts") or []}
        manifest_members = manifest.get("members") if isinstance(manifest, dict) else {}
        if not isinstance(manifest_members, dict):
            manifest_members = {}
        missing_roles = sorted(required - roles)
        if missing_roles:
            errors.append("missing_roles:" + ",".join(missing_roles))
        if set(manifest_members) != roles:
            errors.append("manifest_member_roles")
        inspector = collection_inspector or _default_collection_inspector
        collection_detail: dict[str, Any] = {}
        for row in rows:
            role = str(row["serving_role"])
            if str(row["authority_role"]) != role:
                errors.append(f"registry_role_mismatch:{role}")
            definition = definitions.get(role) or {}
            expected_definition_hash = hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()
            if str(row["definition_hash"]) != expected_definition_hash:
                errors.append(f"runtime_registry_hash:{role}")
            if str(row["location_kind"]) != str(definition.get("kind") or ""):
                errors.append(f"registry_kind_mismatch:{role}")
            if str(row["privacy_class"]) != str(definition.get("privacy") or ""):
                errors.append(f"registry_privacy_mismatch:{role}")
            declared = manifest_members.get(role) or {}
            for key in ("artifact_version_id", "version", "checksum", "location_kind", "location_ref", "watermark_id"):
                if str(declared.get(key) or "") != str(row[key] or ""):
                    errors.append(f"manifest_member_mismatch:{role}:{key}")
            if str(row["location_kind"]) == "chroma_collection":
                actual = dict(inspector(str(row["location_ref"])))
                collection_detail[role] = actual
                if not actual.get("exists"):
                    errors.append(f"collection_missing:{role}")
                if str(actual.get("checksum") or "") != str(row["checksum"]):
                    errors.append(f"collection_checksum:{role}")
                expected_count = json.loads(row["metadata_json"] or "{}").get("count")
                if expected_count is None:
                    expected_count = json.loads(row["metadata_json"] or "{}").get("unit_count")
                if expected_count is not None and int(actual.get("count", -1)) != int(expected_count):
                    errors.append(f"collection_count:{role}")
        detail.update({"snapshot_id": str(snap["snapshot_id"]), "roles": sorted(roles), "collections": collection_detail, "errors": errors})
        con.close()
        return CheckResult("serving_snapshot", not errors, "critical", "active serving snapshot integral" if not errors else "active serving snapshot invalid", detail)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("serving_snapshot", False, "critical", f"serving snapshot check failed: {exc}", {**detail, "error": str(exc)})


def _check_pointer_parity(db_path: Path, pointer_path: Path) -> CheckResult:
    try:
        from personal_knowledge.retrieval.serving import ServingSnapshotResolver
        state = ServingSnapshotResolver(db_path, pointer_path).resolve()
        ok = not state.legacy and not state.drift
        return CheckResult("snapshot_pointer_parity", ok, "critical", "SQLite authority matches compatibility pointer" if ok else "serving authority/pointer drift", {"snapshot_id": state.snapshot_id, "legacy": state.legacy, "drift": state.drift})
    except Exception as exc:  # noqa: BLE001
        return CheckResult("snapshot_pointer_parity", False, "critical", f"pointer parity check failed: {exc}", {"error": str(exc)})


def _check_source_watermarks(db_path: Path) -> CheckResult:
    errors: list[str] = []
    try:
        con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
        active_row = con.execute(
            "SELECT active_snapshot_id FROM serving_authority WHERE singleton_id=1"
        ).fetchone()
        latest_event = (
            con.execute(
                "SELECT action FROM serving_snapshot_events WHERE snapshot_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (active_row[0],),
            ).fetchone()
            if active_row
            else None
        )
        rollback_active = bool(latest_event and str(latest_event[0]) == "rollback")
        rows = con.execute(
            "SELECT m.serving_role,m.watermark_id,w.artifact_version_id,m.artifact_version_id,"
            "w.recorded_at,(SELECT MAX(w2.recorded_at) FROM source_watermarks w2 WHERE w2.registry_id=v.registry_id AND w2.source_key=w.source_key) "
            "FROM serving_authority a JOIN serving_snapshot_members m ON m.snapshot_id=a.active_snapshot_id "
            "JOIN artifact_versions v ON v.artifact_version_id=m.artifact_version_id "
            "LEFT JOIN source_watermarks w ON w.watermark_id=m.watermark_id WHERE a.singleton_id=1"
        ).fetchall()
        by_role = {str(row[0]): row for row in rows}
        for role in sorted(WATERMARK_ROLES):
            row = by_role.get(role)
            if row is None:
                errors.append(f"missing_member:{role}")
            elif not row[1]:
                errors.append(f"missing_watermark:{role}")
            elif str(row[2]) != str(row[3]):
                errors.append(f"watermark_version_mismatch:{role}")
            elif str(row[4]) != str(row[5]) and not rollback_active:
                errors.append(f"stale_watermark:{role}")
        con.close()
        return CheckResult(
            "source_watermarks",
            not errors,
            "critical",
            "source watermarks current and version-bound" if not errors else "source watermark drift",
            {"errors": errors, "rollback_active": rollback_active},
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("source_watermarks", False, "critical", f"source watermark check failed: {exc}", {"error": str(exc)})


def _check_coverage_matrix(
    db_path: Path,
    canonical_db: Path,
    *,
    skip: bool = False,
    snapshot_path: Path | None = None,
) -> CheckResult:
    """Phase 41 Plan 03 (D-06)：source × role 覆盖矩阵，WARN-only。

    ok 恒 True、severity='warn'、不进 hard_fail_ids——覆盖是观测问题
    不是正确性问题（D-06 "WARN 不 FAIL"），exit code 不受影响。

    唯一的状态变更例外：doctor 文档自称 "Never mutates state"，本 check
    会把当次矩阵摘要写回 ``ku_coverage_latest.json`` 供下次运行比对分级
    （新 source 首现 INFO / 已知 source 连续零覆盖 WARN）。快照内容全
    count/hash-only（隐私安全，无任何消息原文或 evidence_ref 清单）。
    """
    if skip:
        return CheckResult(
            id="coverage_matrix",
            ok=True,
            severity="warn",
            message="coverage matrix skipped (--skip-coverage)",
            detail={"skipped": True},
        )
    snap = snapshot_path or COVERAGE_SNAPSHOT_PATH
    try:
        from personal_knowledge.application.knowledge.coverage_matrix import (
            compute_coverage_matrix,
        )
        from personal_knowledge.application.knowledge.eligibility import (
            compute_source_checksum,
        )

        previous: dict | None = None
        if snap.exists():
            try:
                loaded = json.loads(snap.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    previous = loaded
            except (OSError, json.JSONDecodeError):
                previous = None

        current_checksum = compute_source_checksum(canonical_db)
        if previous and previous.get("source_checksum") == current_checksum:
            matrix = previous
            cached = True
        else:
            matrix = compute_coverage_matrix(
                db_path, canonical_db, previous_snapshot=previous
            )
            cached = False
            # 快照写文件是 "Never mutates state" 的唯一例外（见 docstring）；
            # 内容 count/hash-only，隐私安全。
            snap.parent.mkdir(parents=True, exist_ok=True)
            snap.write_text(
                json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        rows = matrix.get("rows") or []
        totals = matrix.get("totals") or {}
        warn_rows = sum(1 for r in rows if r.get("level") == "warn")
        info_rows = sum(1 for r in rows if r.get("level") == "info")
        if warn_rows:
            msg = f"coverage matrix: {warn_rows} zero-coverage WARN row(s) of {len(rows)}"
        elif info_rows:
            msg = f"coverage matrix: {info_rows} new source(s) INFO, no WARN rows"
        else:
            msg = f"coverage matrix: {len(rows)} rows, no zero-coverage warnings"
        detail: dict[str, Any] = {
            "cached": cached,
            "source_checksum": str(matrix.get("source_checksum") or ""),
            "rows": rows,
            "totals": totals,
            "warn_rows": warn_rows,
            "info_rows": info_rows,
            "snapshot_path": str(snap),
        }
        return CheckResult(
            id="coverage_matrix",
            ok=True,
            severity="warn",
            message=msg,
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 — 观测 check 失败不阻断 doctor
        return CheckResult(
            id="coverage_matrix",
            ok=True,
            severity="warn",
            message=f"coverage matrix check failed: {exc}",
            detail={"error": str(exc)},
        )


def _check_session_dedup(canonical_db: Path) -> CheckResult:
    """稳定键/双份率观测，WARN-only；不进入 hard_fail_ids。

    ok 恒 True、severity='warn'——双份率是观测问题，不是正确性 gate。
    任何 schema 或读取异常也只报告，不阻断 doctor。
    """
    try:
        con = sqlite3.connect(
            f"file:{canonical_db.resolve().as_posix()}?mode=ro", uri=True
        )
        try:
            duplicate_stable_key_groups = con.execute(
                "SELECT COUNT(*) FROM (SELECT s.source, s.source_session_id "
                "FROM session_source_links s "
                "JOIN canonical_sessions c USING(canonical_session_id) "
                "WHERE c.lifecycle IS NULL OR c.lifecycle='active' "
                "GROUP BY 1,2 HAVING COUNT(DISTINCT s.canonical_session_id)>1)"
            ).fetchone()[0]
            av_ids = {
                str(row[0])
                for row in con.execute(
                    "SELECT DISTINCT source_session_id FROM session_source_links "
                    "WHERE source='agentsview'"
                )
            }
            legacy_rows = [
                str(row[0])
                for row in con.execute(
                    "SELECT l.source_session_id FROM session_source_links l "
                    "JOIN canonical_sessions c USING(canonical_session_id) "
                    "WHERE l.source='legacy' AND c.primary_source='legacy' "
                    "AND c.evidence_eligible=1"
                )
            ]
        finally:
            con.close()

        uuid_pattern = re.compile(
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12})$",
            re.IGNORECASE,
        )
        av_native = {
            (uuid_pattern.search(value).group(1).lower() if uuid_pattern.search(value) else value.lower())
            for value in av_ids
        }
        codex_dup_pairs = sum(
            bool(uuid_pattern.search(value) and uuid_pattern.search(value).group(1).lower() in av_native)
            for value in legacy_rows
        )
        detail = {
            "duplicate_stable_key_groups": int(duplicate_stable_key_groups),
            "codex_dup_pairs": int(codex_dup_pairs),
        }
        message = "session dedup clean" if not any(detail.values()) else (
            "session dedup observations: "
            f"duplicate_stable_key_groups={detail['duplicate_stable_key_groups']}, "
            f"codex_dup_pairs={detail['codex_dup_pairs']}"
        )
        return CheckResult("session_dedup", True, "warn", message, detail)
    except Exception as exc:  # noqa: BLE001 — observation must not block doctor
        return CheckResult(
            "session_dedup", True, "warn", f"session dedup check failed: {exc}", {"error": str(exc)}
        )


def _check_evidence_probe(
    db_path: Path,
    conversation_db: Path,
    *,
    probe: Callable[[], Mapping[str, Any]] | None = None,
) -> CheckResult:
    try:
        if probe is not None:
            result = dict(probe())
        else:
            from personal_knowledge.retrieval.evidence import EvidenceResolver
            con = sqlite3.connect(f"file:{conversation_db.resolve().as_posix()}?mode=ro", uri=True)
            row = con.execute("SELECT canonical_message_id FROM canonical_messages ORDER BY canonical_message_id LIMIT 1").fetchone()
            con.close()
            if row is None:
                result = {"status": "missing", "error": "no_evidence_probe"}
            else:
                result = EvidenceResolver(unified_db=db_path, conversation_db=conversation_db).resolve(str(row[0]), artifact_type="canonical_message")
        ok = result.get("status") in {"ok", "ineligible"}
        safe = {k: v for k, v in result.items() if k != "content"}
        return CheckResult("evidence_resolver", ok, "critical", "typed evidence probe resolved" if ok else "typed evidence probe failed", safe)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("evidence_resolver", False, "critical", f"evidence probe failed: {exc}", {"error": str(exc)})


def _check_watermark(
    *,
    db_path: Path,
    canonical_db: Path,
) -> CheckResult:
    """Informational: committed watermark vs current source checksum."""
    detail: dict[str, Any] = {
        "db": str(db_path),
        "canonical_db": str(canonical_db),
    }
    if not db_path.exists():
        return CheckResult(
            id="watermark",
            ok=True,  # non-critical; unified_db check already fails hard
            severity="info",
            message="watermark skipped (unified db missing)",
            detail=detail,
        )
    try:
        from personal_knowledge.application.knowledge.refresh_knowledge_units import (
            compute_source_checksum,
            get_committed_watermark,
        )

        committed = get_committed_watermark(db_path)
        current = (
            compute_source_checksum(canonical_db) if canonical_db.exists() else ""
        )
        matches = bool(committed and current and committed == current)
        detail.update(
            {
                "committed": committed,
                "current_source_checksum": current,
                "source_matches_watermark": matches,
            }
        )
        if not committed:
            msg = "no committed watermark (info)"
        elif matches:
            msg = "watermark matches current source checksum"
        else:
            msg = "watermark differs from current source (delta may be pending)"
        return CheckResult(
            id="watermark",
            ok=True,
            severity="info",
            message=msg,
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            id="watermark",
            ok=True,
            severity="warn",
            message=f"watermark check failed: {exc}",
            detail={**detail, "error": str(exc)},
        )


def _check_sqlite_foreign_keys(db_path: Path) -> CheckResult:
    """Fail closed when the live unified DB contains FK violations."""
    detail: dict[str, Any] = {"path": str(db_path)}
    if not db_path.exists():
        return CheckResult(
            id="sqlite_foreign_keys",
            ok=True,
            severity="info",
            message="foreign-key check skipped (unified db missing)",
            detail=detail,
        )
    try:
        con = sqlite3.connect(
            f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True
        )
        initial = int(con.execute("PRAGMA foreign_keys").fetchone()[0])
        con.execute("PRAGMA foreign_keys = ON")
        enabled = int(con.execute("PRAGMA foreign_keys").fetchone()[0])
        rows = con.execute(
            'SELECT "table", COUNT(*) FROM pragma_foreign_key_check '
            'GROUP BY "table" ORDER BY "table"'
        ).fetchall()
        con.close()
        by_table = {str(table): int(count) for table, count in rows}
        total = sum(by_table.values())
        detail.update(
            {
                "connection_initially_enabled": bool(initial),
                "check_connection_enabled": bool(enabled),
                "violations_total": total,
                "violations_by_table": by_table,
            }
        )
        ok = bool(enabled) and total == 0
        message = (
            "foreign-key integrity clean"
            if ok
            else f"foreign-key violations: {total}"
        )
        return CheckResult(
            id="sqlite_foreign_keys",
            ok=ok,
            severity="critical",
            message=message,
            detail=detail,
        )
    except sqlite3.Error as exc:
        return CheckResult(
            id="sqlite_foreign_keys",
            ok=False,
            severity="critical",
            message=f"foreign-key check failed: {exc}",
            detail={**detail, "error": str(exc)},
        )


def _tcp_listening(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_health(url: str, timeout: float = 1.5) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read(256).decode("utf-8", errors="replace")
            return True, f"HTTP {resp.status} {body[:80]!r}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _check_ports(
    endpoints: tuple[tuple[str, int, str], ...] = DEFAULT_HEALTH_ENDPOINTS,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for name, port, url in endpoints:
        listening = _tcp_listening("127.0.0.1", port)
        detail: dict[str, Any] = {
            "name": name,
            "port": port,
            "url": url,
            "listening": listening,
        }
        if not listening:
            results.append(
                CheckResult(
                    id=f"port_{port}",
                    ok=True,  # warn-only: do not fail doctor
                    severity="warn",
                    message=f"{name} :{port} not listening (start services if needed)",
                    detail=detail,
                )
            )
            continue
        healthy, info = _http_health(url)
        detail["health"] = info
        detail["healthy"] = healthy
        results.append(
            CheckResult(
                id=f"port_{port}",
                ok=True,  # still warn-only even if HTTP fails
                severity="info" if healthy else "warn",
                message=(
                    f"{name} :{port} healthy"
                    if healthy
                    else f"{name} :{port} listening but health check weak: {info}"
                ),
                detail=detail,
            )
        )
    return results


_DOMAIN_IMPORT_RE = re.compile(
    r"^\s*(?:from|import)\s+personal_knowledge\.domains\b"
)


def scan_facade_imports(
    application_root: Path | None = None,
) -> dict[str, Any]:
    """Count ``personal_knowledge.domains`` imports under application/."""
    root = application_root or (PACKAGE_DIR / "application")
    per_file: dict[str, int] = {}
    total = 0
    if not root.is_dir():
        return {
            "total_import_lines": 0,
            "files_with_imports": 0,
            "top_files": [],
            "application_root": str(root),
            "error": "application root missing",
        }
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        count = 0
        for line in text.splitlines():
            if _DOMAIN_IMPORT_RE.search(line):
                count += 1
        if count:
            rel = path.relative_to(root).as_posix()
            per_file[rel] = count
            total += count
    ranked = sorted(per_file.items(), key=lambda kv: (-kv[1], kv[0]))
    top = [{"path": p, "count": c} for p, c in ranked[:10]]
    return {
        "total_import_lines": total,
        "files_with_imports": len(per_file),
        "top_files": top,
        "application_root": str(root),
        "retire_window": "2026-08-13",
    }


def run_doctor(
    *,
    unified_db: Path | None = None,
    conversations_db: Path | None = None,
    active_pointer: Path | None = None,
    skip_ports: bool = False,
    include_facade: bool = True,
    application_root: Path | None = None,
    composite_checks: bool = True,
    registry_path: Path | None = None,
    collection_inspector: Callable[[str], Mapping[str, Any]] | None = None,
    evidence_probe: Callable[[], Mapping[str, Any]] | None = None,
    skip_coverage: bool = False,
    coverage_snapshot_path: Path | None = None,
) -> DoctorReport:
    """Run read-only product checks. Never mutates state.

    唯一例外：coverage_matrix check 会把当次矩阵摘要写回
    ``ku_coverage_latest.json``（count/hash-only，隐私安全）供下次
    运行比对分级；除此之外不 promote、不推进 watermark、不改知识行。
    """
    db = unified_db or UNIFIED_DB
    conv = conversations_db or AGENT_CONVERSATIONS_DB
    pointer = active_pointer or KNOWLEDGE_ACTIVE_POINTER

    checks: list[CheckResult] = []
    checks.append(_check_import())
    checks.append(
        _check_path_exists(
            "unified_db",
            db,
            severity="critical",
            label="UNIFIED_DB",
        )
    )
    checks.append(_check_sqlite_foreign_keys(db))
    # Conversations DB missing is warn if import/db OK — product can still run
    # with stale knowledge, but daily sync needs it. Plan: "report missing".
    # Treat as critical for product daily ops integrity.
    checks.append(
        _check_path_exists(
            "agent_conversations_db",
            conv,
            severity="critical",
            label="AGENT_CONVERSATIONS_DB",
        )
    )
    checks.append(_check_active_pointer(pointer))
    checks.append(_check_watermark(db_path=db, canonical_db=conv))
    if composite_checks:
        checks.append(_check_artifact_registry(registry_path))
        checks.append(_check_serving_snapshot(db, collection_inspector=collection_inspector))
        checks.append(_check_pointer_parity(db, pointer))
        checks.append(_check_evidence_probe(db, conv, probe=evidence_probe))
        checks.append(_check_source_watermarks(db))
        checks.append(
            _check_coverage_matrix(
                db,
                conv,
                skip=skip_coverage,
                snapshot_path=coverage_snapshot_path,
            )
        )
        checks.append(_check_session_dedup(conv))

    if not skip_ports:
        checks.extend(_check_ports())

    facade: dict[str, Any] = {}
    if include_facade:
        facade = scan_facade_imports(application_root)

    critical_failed = [
        c for c in checks if c.severity == "critical" and not c.ok
    ]
    # Plan: exit 1 if active pointer missing or DB missing
    # Phase 42 的 session_dedup 是 WARN-only 观测，不加入下面的 hard-fail 集合。
    hard_fail_ids = {
        "import_personal_knowledge",
        "unified_db",
        "agent_conversations_db",
        "active_pointer",
        "sqlite_foreign_keys",
        "artifact_registry",
        "serving_snapshot",
        "snapshot_pointer_parity",
        "evidence_resolver",
        "source_watermarks",
        # D-06 "WARN 不 FAIL"：coverage_matrix 永不加入本集合——
        # 覆盖是观测问题不是正确性问题，exit code 不受矩阵内容影响。
    }
    hard_failed = [c for c in checks if c.id in hard_fail_ids and not c.ok]
    ok = not hard_failed
    exit_code = 0 if ok else 1

    summary = {
        "total": len(checks),
        "critical_ok": sum(
            1 for c in checks if c.severity == "critical" and c.ok
        ),
        "critical_fail": len(critical_failed),
        "warn": sum(1 for c in checks if c.severity == "warn"),
        "info": sum(1 for c in checks if c.severity == "info"),
    }

    return DoctorReport(
        ok=ok,
        exit_code=exit_code,
        checks=checks,
        facade=facade,
        summary=summary,
        paths={
            "root": str(ROOT),
            "src": str(SRC_DIR),
            "unified_db": str(db),
            "agent_conversations_db": str(conv),
            "active_pointer": str(pointer),
            "python": sys.executable,
            "cwd": str(Path.cwd()),
        },
    )


def report_to_dict(report: DoctorReport) -> dict[str, Any]:
    return {
        "ok": report.ok,
        "exit_code": report.exit_code,
        "note": report.note,
        "summary": report.summary,
        "paths": report.paths,
        "checks": [asdict(c) for c in report.checks],
        "facade": report.facade,
    }


def format_human(report: DoctorReport) -> str:
    lines: list[str] = [
        "pk-ku doctor (read-only)",
        "========================",
        f"status: {'OK' if report.ok else 'FAIL'}  exit={report.exit_code}",
        "",
    ]
    for c in report.checks:
        mark = "OK" if c.ok else "FAIL"
        if c.severity == "warn" and c.ok:
            mark = "WARN"
        elif c.severity == "info" and c.ok:
            mark = "INFO"
        lines.append(f"  [{mark}] {c.id}: {c.message}")
    coverage = next((c for c in report.checks if c.id == "coverage_matrix"), None)
    if coverage is not None and not coverage.detail.get("skipped"):
        rows = coverage.detail.get("rows") or []
        warn_rows = [r for r in rows if r.get("level") == "warn"]
        lines.append("")
        if warn_rows:
            lines.append(
                f"coverage WARN rows (top 10 of {len(warn_rows)}; "
                "zero coverage two runs in a row):"
            )
            lines.append("  source         role        eligible  covered  not_queued")
            for r in warn_rows[:10]:
                lines.append(
                    f"  {str(r.get('source')):<14} {str(r.get('role')):<11} "
                    f"{int(r.get('eligible_count') or 0):>8} "
                    f"{int(r.get('covered_count') or 0):>8} "
                    f"{int(r.get('not_queued_count') or 0):>10}"
                )
        elif rows:
            lines.append(
                f"coverage matrix: {len(rows)} rows, all ok (no zero-coverage warnings)"
            )
    if report.facade:
        lines.append("")
        lines.append(
            f"facade imports (application → domains): "
            f"{report.facade.get('total_import_lines', 0)} lines in "
            f"{report.facade.get('files_with_imports', 0)} files"
        )
        for item in report.facade.get("top_files") or []:
            lines.append(f"  - {item['path']}: {item['count']}")
        lines.append(
            f"  retire window: {report.facade.get('retire_window', 'n/a')}"
        )
    lines.append("")
    lines.append(report.note)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pk-ku-doctor",
        description="Read-only KU product health checks (no promote / no write).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit full JSON report only",
    )
    p.add_argument(
        "--skip-ports",
        action="store_true",
        help="Skip TCP/HTTP checks for :8000/:8789",
    )
    p.add_argument(
        "--skip-coverage",
        action="store_true",
        help="Skip the source × role coverage matrix check (escape hatch)",
    )
    p.add_argument(
        "--no-facade",
        action="store_true",
        help="Skip domains facade import inventory",
    )
    p.add_argument("--db", type=Path, default=None, help="Override UNIFIED_DB")
    p.add_argument(
        "--canonical-db",
        type=Path,
        default=None,
        help="Override AGENT_CONVERSATIONS_DB",
    )
    p.add_argument(
        "--active-pointer",
        type=Path,
        default=None,
        help="Override knowledge_index_active.txt path",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_doctor(
        unified_db=args.db,
        conversations_db=args.canonical_db,
        active_pointer=args.active_pointer,
        skip_ports=args.skip_ports,
        include_facade=not args.no_facade,
        skip_coverage=args.skip_coverage,
    )
    if args.json:
        print(json.dumps(report_to_dict(report), ensure_ascii=False, indent=2))
    else:
        print(format_human(report))
        # Also dump compact JSON line for scripting when not --json
        if os.environ.get("PK_KU_DOCTOR_JSON_SIDE") == "1":
            print(json.dumps(report_to_dict(report), ensure_ascii=False))
    return int(report.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
