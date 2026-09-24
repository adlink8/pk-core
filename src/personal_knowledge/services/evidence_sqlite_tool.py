"""Phase 61 Wave 0: bounded descriptor-only evidence SQLite authority Tool.

HARNESS-03 / D-16 / D-17 / D-26 / D-29. Python exclusively owns a direct-but-
governed evidence query slice over canonical conversation SSOT. The model,
renderer and Node Kernel can never supply SQL, database paths, callable names,
``statement_display`` overrides or a Node-side connection; every approved read
returns stable identity/checksum/freshness/bounds and an immutable receipt.

The only Phase 61 query is ``conversation.evidence_messages.v1`` backed by the
canonical conversation repository schema. Physical SQL/table/column mapping is
private to this adapter; only approved descriptor fields are ever exposed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB  # noqa: E402
from personal_knowledge.core.canonical_visibility import (  # noqa: E402
    canonical_projection_predicate,
)

EVIDENCE_SQLITE_OPERATION = "evidence.sqlite_query"
EVIDENCE_SQLITE_SCHEMA = "pi_evidence_sqlite_v1"
EVIDENCE_SQLITE_RECEIPT_SCHEMA = "pi_evidence_sqlite_receipt_v1"
QUERY_ID = "conversation.evidence_messages.v1"
DESCRIPTOR_VERSION = "1.0.0"
DATABASE_ID = "canonical_conversation_v1"
LEASE_SKILL_ID = "knowledge.research"
PRIVACY_CEILING = "R1"
MAX_ROWS = 50
MAX_BYTES = 16384
TIMEOUT_MS = 3000
# Authoritative ordered parameter-name set for the only approved query. The
# order is part of the approved descriptor and feeds statement_display; the
# checksum binds the *sorted* set.
EVIDENCE_MESSAGES_PARAMETERS = ("session_id", "after", "limit")

_THIS_DIR = Path(__file__).resolve().parent
_ROOT = _THIS_DIR.parents[2]
SKILLS_MANIFEST_PATH = _ROOT / "governance" / "manifests" / "ai" / "pi-skills.json"

_QUERY_ALLOWLIST: frozenset[str] = frozenset({QUERY_ID})
_ALLOWED_DESCRIPTOR_KEYS: frozenset[str] = frozenset({
    "database_id", "query_id", "version", "parameters", "scope", "binding",
    "skill_id", "supporting_skills", "manifest_checksum", "privacy_ceiling",
})
_SCOPE_KEYS: tuple[str, ...] = ("session_id",)
_REQUIRED_TABLES: tuple[str, ...] = ("canonical_sessions", "canonical_messages")
_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "canonical_messages": (
        "canonical_message_id", "canonical_session_id", "ordinal", "role",
        "timestamp", "source_message_ref",
    ),
    "canonical_sessions": ("canonical_session_id",),
}
_SQLITE_SUFFIXES: tuple[str, ...] = (".sqlite", ".db", ".sqlite3")
_SQL_FRAGMENTS: tuple[str, ...] = (
    "select", "insert", "update", "delete", "drop", "alter", "attach",
    "pragma", "create", "replace", "load_extension", "from", "where", "join",
    "union", "vacuum", "reindex", "with ",
)
_SQL_TOKENS: tuple[str, ...] = (";", "--", "/*", "*/", "'", '"', "(", ")")


class EvidenceSqliteError(Exception):
    """Typed fail-closed error; ``code`` is the stable transport-safe code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def knowledge_research_checksum(path: Path | None = None) -> str:
    """Current checksum of the ``knowledge.research`` Skill.

    Replicates the Kernel Skill-engine canonicalization (recursive key sort,
    compact JSON, sha256) so manifest drift is detected with the same value the
    Node runtime validates.
    """
    manifest = json.loads((path or SKILLS_MANIFEST_PATH).read_text(encoding="utf-8"))
    for skill in manifest["skills"]:
        if skill.get("id") == LEASE_SKILL_ID:
            payload = {key: value for key, value in skill.items() if key != "checksum"}
            return hashlib.sha256(_canonical_json(payload)).hexdigest()
    raise EvidenceSqliteError("lease_missing")


def derive_statement_display(query_id: str, parameter_names: Mapping | tuple | list) -> str:
    """Deterministic logical statement display from the approved descriptor.

    Built only from the query ID/version and the descriptor's ordered
    parameter-name set; never from physical SQL, schema, values or callers.
    """
    return f"{query_id}({', '.join(parameter_names)})"


def query_checksum(*, query_id: str, version: str, parameter_names: Mapping | tuple | list,
                   statement_display: str) -> str:
    """Checksum binding query ID, version, sorted parameter-name set and display."""
    return _sha256_hex({
        "query_id": query_id,
        "version": version,
        "parameter_names": sorted(parameter_names),
        "statement_display": statement_display,
    })


def database_fingerprint(path: Path) -> str:
    """Stable sha256 fingerprint of a database file (missing -> empty hash)."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        data = b""
    return hashlib.sha256(data).hexdigest()


def _read_only_uri(path: Path) -> str:
    return f"file:{Path(path).as_posix()}?mode=ro"


def _contains_sql_fragment(value: str) -> bool:
    lowered = value.lower()
    return any(fragment in lowered for fragment in _SQL_FRAGMENTS) or any(token in lowered for token in _SQL_TOKENS)


class EvidenceSqliteTool:
    """Descriptor-only SQLite authority: allowlist, RO URI, query_only, bounds.

    Policy validation happens entirely before any database open; the adapter
    repeats query-ID/scope/binding denial after the Domain Gateway so a request
    can never reach the database without passing both gates.
    """

    def __init__(
        self,
        db_path: Path = AGENT_CONVERSATIONS_DB,
        *,
        max_rows: int = MAX_ROWS,
        max_bytes: int = MAX_BYTES,
        timeout_ms: int = TIMEOUT_MS,
        sleep_hook: Callable[[float], None] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.max_rows = max_rows
        self.max_bytes = max_bytes
        self.timeout_ms = timeout_ms
        self.sleep_hook = sleep_hook

    # ------------------------------------------------------------------
    # Policy gate (fail closed before any database access)
    # ------------------------------------------------------------------

    def _validate_lease_and_privacy(self, params: Mapping[str, Any]) -> None:
        if params.get("database_id") != DATABASE_ID:
            raise EvidenceSqliteError("database_unknown")
        if params.get("query_id") not in _QUERY_ALLOWLIST:
            raise EvidenceSqliteError("unknown_query")
        if params.get("version") != DESCRIPTOR_VERSION:
            raise EvidenceSqliteError("version_mismatch")
        if params.get("skill_id") != LEASE_SKILL_ID:
            raise EvidenceSqliteError("lease_invalid")
        if params.get("supporting_skills"):
            raise EvidenceSqliteError("supporting_skill_rejected")
        if params.get("manifest_checksum") != knowledge_research_checksum():
            raise EvidenceSqliteError("manifest_drift")
        if params.get("privacy_ceiling") != PRIVACY_CEILING:
            raise EvidenceSqliteError("privacy_ceiling_mismatch")
        if not params.get("binding"):
            raise EvidenceSqliteError("binding_required")

    def _validate_scope(self, params: Mapping[str, Any]) -> None:
        scope = params.get("scope")
        if not isinstance(scope, Mapping):
            raise EvidenceSqliteError("scope_denied")
        if set(scope) - set(_SCOPE_KEYS):
            raise EvidenceSqliteError("scope_denied")
        parameters = params.get("parameters")
        for key, value in scope.items():
            if not isinstance(value, str):
                raise EvidenceSqliteError("scope_denied")
            if _contains_sql_fragment(value):
                raise EvidenceSqliteError("sql_forbidden")
            if isinstance(parameters, Mapping) and parameters.get(key) is not None and parameters[key] != value:
                raise EvidenceSqliteError("scope_denied")

    def _validate_parameters(self, parameters: object) -> dict[str, Any]:
        if not isinstance(parameters, Mapping):
            raise EvidenceSqliteError("descriptor_invalid")
        if set(parameters) - set(EVIDENCE_MESSAGES_PARAMETERS):
            raise EvidenceSqliteError("parameter_invalid")

        session_id = parameters.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise EvidenceSqliteError("parameter_invalid")
        if ".." in session_id or any(ch in session_id for ch in ("/", "\\", " ", "\t", "\n", "\r")):
            raise EvidenceSqliteError("path_forbidden")
        if _contains_sql_fragment(session_id):
            raise EvidenceSqliteError("sql_forbidden")

        after = parameters.get("after")
        if after is not None:
            if not isinstance(after, str) or not after:
                raise EvidenceSqliteError("parameter_invalid")
            if _contains_sql_fragment(after) or any(ch in after for ch in (" ", "\t", "\n")):
                raise EvidenceSqliteError("sql_forbidden")

        limit = parameters.get("limit")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise EvidenceSqliteError("parameter_invalid")
        if not (1 <= limit <= self.max_rows):
            raise EvidenceSqliteError("limit_exceeded" if limit > self.max_rows else "parameter_invalid")

        return {"session_id": session_id, "after": after, "limit": limit}

    def invoke(self, descriptor: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(descriptor, Mapping):
            raise EvidenceSqliteError("descriptor_invalid")
        params = dict(descriptor)
        if set(params) - _ALLOWED_DESCRIPTOR_KEYS:
            raise EvidenceSqliteError("undeclared_input")
        self._validate_lease_and_privacy(params)
        typed = self._validate_parameters(params.get("parameters"))
        self._validate_scope(params)
        statement_display = derive_statement_display(QUERY_ID, EVIDENCE_MESSAGES_PARAMETERS)
        checksum = query_checksum(
            query_id=QUERY_ID, version=DESCRIPTOR_VERSION,
            parameter_names=EVIDENCE_MESSAGES_PARAMETERS, statement_display=statement_display,
        )
        return self._execute(typed, statement_display, checksum)

    # ------------------------------------------------------------------
    # Bounded read-only execution
    # ------------------------------------------------------------------

    def _execute(self, typed: Mapping[str, Any], statement_display: str, checksum: str) -> dict[str, Any]:
        if not self.db_path.exists():
            if self.db_path.suffix.lower() in _SQLITE_SUFFIXES:
                raise EvidenceSqliteError("database_unavailable")
            return self._unavailable_envelope(statement_display, checksum)

        start = time.monotonic()
        timeout_seconds = max(0.001, self.timeout_ms / 1000.0)
        aborted = {"flag": False}
        try:
            con = sqlite3.connect(_read_only_uri(self.db_path), uri=True)
        except sqlite3.Error as exc:
            raise EvidenceSqliteError("database_unavailable") from exc
        try:
            con.execute("PRAGMA query_only=ON")
            con.row_factory = sqlite3.Row
            schema_checksum = self._schema_gate(con)
            projection_filter, projection_params = canonical_projection_predicate(
                con, "canonical_session_id"
            )
            visible_session = con.execute(
                "SELECT 1 FROM canonical_sessions "
                f"WHERE canonical_session_id=? AND {projection_filter} LIMIT 1",
                (typed["session_id"], *projection_params),
            ).fetchone()
            if visible_session is None:
                raise EvidenceSqliteError("scope_denied")
            latest_ts = self._latest_message_timestamp(con)

            def _progress() -> int:
                if time.monotonic() - start > timeout_seconds:
                    aborted["flag"] = True
                    return 1
                return 0

            con.set_progress_handler(_progress, 250)
            limit = typed["limit"]
            sql = (
                "SELECT canonical_message_id, canonical_session_id, ordinal, role, "
                "timestamp, source_message_ref FROM canonical_messages "
                "WHERE canonical_session_id = ? AND "
                f"{projection_filter} AND timestamp >= ? "
                "ORDER BY ordinal ASC LIMIT ?"
            )
            bound = (
                typed["session_id"], *projection_params,
                typed["after"] or "", limit + 1,
            )
            try:
                rows = con.execute(sql, bound).fetchall()
            except sqlite3.OperationalError:
                if aborted["flag"]:
                    raise EvidenceSqliteError("query_timeout")
                raise
            finally:
                con.set_progress_handler(None, 0)
        except sqlite3.Error as exc:
            raise EvidenceSqliteError("domain_unavailable") from exc
        finally:
            con.close()

        if self.sleep_hook is not None:
            self.sleep_hook(time.monotonic() - start)
        elapsed = time.monotonic() - start
        duration_ms = int(elapsed * 1000)
        if aborted["flag"] or elapsed > timeout_seconds:
            raise EvidenceSqliteError("query_timeout")

        truncated = len(rows) == limit + 1
        source_rows = rows[:limit] if truncated else rows
        projected: list[dict[str, Any]] = []
        total_bytes = 0
        for row in source_rows:
            item = {
                "message_id": row["canonical_message_id"],
                "session_id": row["canonical_session_id"],
                "ordinal": row["ordinal"],
                "role": row["role"],
                "timestamp": row["timestamp"],
                "source_ref": row["source_message_ref"],
            }
            blob = json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")
            if total_bytes + len(blob) > self.max_bytes:
                truncated = True
                break
            projected.append(item)
            total_bytes += len(blob)

        binding = {
            "database_id": DATABASE_ID,
            "source": "canonical",
            "schema_checksum": schema_checksum,
            "snapshot_id": f"snapshot:{schema_checksum}",
            "freshness": {
                "source": "canonical",
                "latest_message_timestamp": latest_ts,
                "schema_checksum": schema_checksum,
            },
        }
        receipt_id = "evidence:" + _sha256_hex({
            "query_checksum": checksum,
            "schema_checksum": schema_checksum,
            "row_count": len(projected),
            "latest_message_timestamp": latest_ts,
        })[:16]
        receipt = {
            "receipt_schema": EVIDENCE_SQLITE_RECEIPT_SCHEMA,
            "receipt_id": receipt_id,
            "identity": f"{DATABASE_ID}:canonical",
            "freshness": latest_ts,
            "query_checksum": checksum,
            "truncated": truncated,
            "status": "success",
            "row_count": len(projected),
            "bytes": total_bytes,
            "duration_ms": duration_ms,
            "database_id": DATABASE_ID,
            "source": "canonical",
            "query_id": QUERY_ID,
            "descriptor_version": DESCRIPTOR_VERSION,
        }
        return {
            "schema_version": EVIDENCE_SQLITE_SCHEMA,
            "operation": EVIDENCE_SQLITE_OPERATION,
            "ok": True,
            "status": "success",
            "query_id": QUERY_ID,
            "descriptor_version": DESCRIPTOR_VERSION,
            "database_id": DATABASE_ID,
            "row_count": len(projected),
            "limit": self.max_rows,
            "truncated": truncated,
            "bytes": total_bytes,
            "duration_ms": duration_ms,
            "statement_display": statement_display,
            "parameter_names": sorted(EVIDENCE_MESSAGES_PARAMETERS),
            "query_checksum": checksum,
            "rows": projected,
            "binding": binding,
            "receipt": receipt,
        }

    def _unavailable_envelope(self, statement_display: str, checksum: str) -> dict[str, Any]:
        return {
            "schema_version": EVIDENCE_SQLITE_SCHEMA,
            "operation": EVIDENCE_SQLITE_OPERATION,
            "ok": False,
            "status": "database_unavailable",
            "execution": "not_run",
            "query_id": QUERY_ID,
            "descriptor_version": DESCRIPTOR_VERSION,
            "database_id": DATABASE_ID,
            "row_count": 0,
            "limit": self.max_rows,
            "truncated": False,
            "bytes": 0,
            "duration_ms": 0,
            "statement_display": statement_display,
            "parameter_names": sorted(EVIDENCE_MESSAGES_PARAMETERS),
            "query_checksum": checksum,
            "rows": [],
            "binding": {
                "database_id": DATABASE_ID,
                "source": "canonical",
                "schema_checksum": "",
                "snapshot_id": f"snapshot:{''}",
                "freshness": {"source": "canonical", "latest_message_timestamp": ""},
            },
            "receipt": {
                "receipt_schema": EVIDENCE_SQLITE_RECEIPT_SCHEMA,
                "receipt_id": "evidence:" + checksum[:16],
                "identity": f"{DATABASE_ID}:canonical",
                "freshness": "",
                "query_checksum": checksum,
                "truncated": False,
                "status": "database_unavailable",
            },
        }

    def _schema_gate(self, con: sqlite3.Connection) -> str:
        all_tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if any(table not in all_tables for table in _REQUIRED_TABLES):
            raise EvidenceSqliteError("schema_gate_failed")
        for table, columns in _REQUIRED_COLUMNS.items():
            actual = {c[1] for c in con.execute(f"PRAGMA table_info({table})")}
            if not set(columns) <= actual:
                raise EvidenceSqliteError("schema_gate_failed")
        ddl = [
            sql or ""
            for _name, sql in con.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            if _name in _REQUIRED_TABLES
        ]
        return hashlib.sha256("\n;;;".join(ddl).encode("utf-8")).hexdigest()[:16]

    def _latest_message_timestamp(self, con: sqlite3.Connection) -> str:
        projection_filter, projection_params = canonical_projection_predicate(
            con, "canonical_session_id"
        )
        row = con.execute(
            "SELECT MAX(timestamp) FROM canonical_messages "
            f"WHERE {projection_filter}",
            projection_params,
        ).fetchone()
        return row[0] or ""


__all__ = [
    "DATABASE_ID", "DESCRIPTOR_VERSION", "EVIDENCE_MESSAGES_PARAMETERS",
    "EVIDENCE_SQLITE_OPERATION", "EVIDENCE_SQLITE_RECEIPT_SCHEMA",
    "EVIDENCE_SQLITE_SCHEMA", "LEASE_SKILL_ID", "MAX_BYTES", "MAX_ROWS",
    "PRIVACY_CEILING", "QUERY_ID", "TIMEOUT_MS", "EvidenceSqliteError",
    "EvidenceSqliteTool", "database_fingerprint", "derive_statement_display",
    "knowledge_research_checksum", "query_checksum",
]
