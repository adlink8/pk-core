"""Plan 61-05: Python-canonical conversation/project-scope projection (HARNESS-01).

The canonical conversation repository is the sole authority for a safe
``conversation.thread.last|recent|select`` and the schema-bound
``conversation.project_scopes.list`` / ``conversation.project_scope.select``
reads. Project scopes are derived deterministically from canonical session
working directories (``canonical_sessions.cwd``): one scope per distinct
working directory, label = directory basename.

Every response is metadata-only and read-only. Selected-thread display text is
normalized to stable user/assistant messages and exists only inside the returned
view model: it is never retained on this service and never reaches telemetry
(which receives IDs/counts/checksums/status only). The live AgentsView database
is never referenced here and no canonical write is ever issued.
"""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping

from personal_knowledge.application.conversation.harness_freshness import DualFreshness  # noqa: F401
from personal_knowledge.core.canonical_visibility import (
    canonical_projection_predicate,
)
from personal_knowledge.core.conversation_repository import SOURCE_CANONICAL, ConversationRepository  # noqa: F401

MAX_LIMIT = 50
MESSAGE_FIELDS = ("message_id", "role", "display_text", "created_at", "source_ref", "evidence_ref")
THREAD_METADATA_FIELDS = ("conversation_id", "project_scope_id", "label", "last_activity_at", "message_count")
SCOPE_ROW_FIELDS = ("project_scope_id", "label", "thread_count", "last_activity_at", "freshness")


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii")


def _decode_cursor(value: str | None) -> int:
    if value is None:
        return 0
    try:
        return int(base64.urlsafe_b64decode(str(value).encode("ascii")).decode("ascii"))
    except Exception as exc:  # transport-safe: opaque cursor only
        raise ValueError("cursor_invalid") from exc


def _basename(path: str) -> str:
    name = Path(path).name
    return name or path


def _pagination(limit: int, has_more: bool, cursor: str | None) -> dict:
    return {"limit": limit, "has_more": has_more, "cursor": cursor}


def _normalize_limit(limit: Any, default: int = 20) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        return default
    if limit < 1:
        return 1
    if limit > MAX_LIMIT:
        return MAX_LIMIT
    return limit


def _overall(freshness: Mapping[str, Any]) -> str:
    return str(freshness.get("overall_status", "unknown"))


def _limitation_text(freshness: Mapping[str, Any]) -> str:
    statuses = [
        str((freshness.get("source_to_agentsview") or {}).get("status", "unknown")),
        str((freshness.get("agentsview_to_canonical") or {}).get("status", "unknown")),
    ]
    if "unknown" in statuses:
        return "freshness unknown: a freshness leg could not be verified"
    if "missing_watermark" in statuses:
        return "missing watermark: synchronization watermarks are not available"
    if "backlog_pending" in statuses:
        return "backlog pending: some evidence has not been committed to canonical"
    if "stale" in statuses:
        return "data is stale: source or canonical sync is older than the freshness horizon"
    return "data is current and bounded to the authorized scope"


class HarnessConversationService:
    """Read-only canonical navigation authority with safe explicit state envelopes.

    Requires an explicit ``canonical`` repository; legacy/live sources are
    rejected because canonical is the sole conversation history authority.
    """

    def __init__(
        self,
        *,
        repository: ConversationRepository,
        freshness_provider: Callable[[], DualFreshness],
        telemetry: Callable[[dict], None] | None = None,
    ) -> None:
        if getattr(repository, "source", None) != SOURCE_CANONICAL:
            raise ValueError("harness conversation service requires an explicit canonical repository (source='canonical')")
        self._repository = repository
        self._db = Path(repository.canonical_db)
        self._freshness_provider = freshness_provider
        self._telemetry = telemetry

    # ------------------------------------------------------------------
    # Internal helpers (all read-only, connection-per-call)
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self._db.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        return con

    def _freshness(self) -> dict:
        return self._freshness_provider().to_dict()

    def _emit_telemetry(self, record: dict) -> None:
        if self._telemetry is not None:
            self._telemetry(record)

    @staticmethod
    def _sessions(con: sqlite3.Connection) -> list[dict]:
        """Canonical sessions plus per-session last message activity (metadata only)."""
        projection_filter, projection_params = canonical_projection_predicate(
            con, "s.canonical_session_id"
        )
        rows = con.execute(
            "SELECT s.canonical_session_id AS session_id, s.cwd, s.message_count, "
            "MAX(CASE WHEN m.role IN ('user', 'assistant') THEN m.timestamp END) AS last_activity "
            "FROM canonical_sessions s "
            "LEFT JOIN canonical_messages m ON m.canonical_session_id = s.canonical_session_id "
            f"WHERE {projection_filter} "
            "GROUP BY s.canonical_session_id "
            "ORDER BY last_activity DESC",
            projection_params,
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _normalized_messages(con: sqlite3.Connection, session_id: str, *, limit: int, offset: int) -> tuple[list[dict], bool]:
        """Project only normalized user/assistant display messages with stable identity."""
        projection_filter, projection_params = canonical_projection_predicate(
            con, "canonical_session_id"
        )
        rows = con.execute(
            "SELECT canonical_message_id, role, content, timestamp, source_message_ref, content_hash "
            "FROM canonical_messages "
            "WHERE canonical_session_id = ? AND "
            f"{projection_filter} AND role IN ('user', 'assistant') "
            "ORDER BY ordinal ASC",
            (session_id, *projection_params),
        ).fetchall()
        total = len(rows)
        page = rows[offset : offset + limit]
        has_more = (offset + len(page)) < total
        messages: list[dict] = []
        for row in page:
            messages.append(
                {
                    "message_id": row["canonical_message_id"],
                    "role": row["role"],
                    "display_text": row["content"] or "",
                    "created_at": row["timestamp"] or "",
                    "source_ref": row["source_message_ref"] or "",
                    "evidence_ref": row["content_hash"] or "",
                }
            )
        return messages, has_more

    def _thread_view(self, session: dict, messages: list[dict], *, limit: int, has_more: bool, cursor: str | None) -> dict:
        freshness = self._freshness()
        if not messages:
            state = "empty"
        elif has_more:
            state = "partial"
        else:
            state = _overall(freshness)
        return {
            "ok": True,
            "data": {
                "conversation_id": session["session_id"],
                "project_scope_id": session.get("cwd"),
                "messages": messages,
                "pagination": _pagination(limit, has_more, cursor),
                "truncated": has_more,
                "freshness": freshness,
                "state": state,
                "limitation": _limitation_text(freshness),
            },
        }

    def _envelope_error(self, code: str) -> dict:
        return {"ok": False, "status": "error", "error": {"code": code}}

    # ------------------------------------------------------------------
    # conversation.thread.last / recent / select (canonical only)
    # ------------------------------------------------------------------

    def thread_last(self, *, limit: int = 20) -> dict:
        """Most recently active canonical conversation as a safe thread view."""
        limit = _normalize_limit(limit)
        try:
            with self._connect() as con:
                sessions = self._sessions(con)
                active = [item for item in sessions if item.get("last_activity")]
                if not active:
                    return self._empty_thread_view(limit, con)
                newest = active[0]
                messages, has_more = self._normalized_messages(
                    con, newest["session_id"], limit=limit, offset=0
                )
                return self._thread_view(newest, messages, limit=limit, has_more=has_more, cursor=None)
        except sqlite3.Error:
            return self._envelope_error("domain_unavailable")

    def _empty_thread_view(self, limit: int, con: sqlite3.Connection) -> dict:
        freshness = self._freshness()
        return {
            "ok": True,
            "data": {
                "conversation_id": None,
                "project_scope_id": None,
                "messages": [],
                "pagination": _pagination(limit, False, None),
                "truncated": False,
                "freshness": freshness,
                "state": "empty",
                "limitation": "empty: no canonical conversation exists yet",
            },
        }

    def thread_recent(self, *, limit: int = 20, cursor: str | None = None) -> dict:
        """Metadata-only recent canonical conversations sorted by last activity."""
        limit = _normalize_limit(limit)
        try:
            offset = _decode_cursor(cursor)
        except ValueError:
            return self._envelope_error("cursor_invalid")
        try:
            with self._connect() as con:
                sessions = self._sessions(con)
                active = [item for item in sessions if item.get("last_activity") and item.get("message_count")]
                page = active[offset : offset + limit]
                has_more = (offset + len(page)) < len(active)
                items = [
                    {
                        "conversation_id": item["session_id"],
                        "project_scope_id": item.get("cwd"),
                        "label": _basename(item.get("cwd") or ""),
                        "last_activity_at": item.get("last_activity"),
                        "message_count": item.get("message_count"),
                    }
                    for item in page
                ]
                return {
                    "ok": True,
                    "data": {
                        "conversations": items,
                        "pagination": _pagination(
                            limit,
                            has_more,
                            _encode_cursor(offset + len(page)) if has_more else None,
                        ),
                        "freshness": self._freshness(),
                    },
                }
        except sqlite3.Error:
            return self._envelope_error("domain_unavailable")

    def thread_select(self, *, conversation_id: str, limit: int = 20, after: str | None = None) -> dict:
        """Selected canonical thread normalized to user/assistant messages only."""
        limit = _normalize_limit(limit)
        try:
            offset = _decode_cursor(after)
        except ValueError:
            return self._envelope_error("cursor_invalid")
        try:
            with self._connect() as con:
                projection_filter, projection_params = (
                    canonical_projection_predicate(
                        con, "canonical_session_id"
                    )
                )
                row = con.execute(
                    "SELECT canonical_session_id AS session_id, cwd, message_count "
                    "FROM canonical_sessions WHERE canonical_session_id = ? AND "
                    f"{projection_filter}",
                    (conversation_id, *projection_params),
                ).fetchone()
                if row is None:
                    return self._envelope_error("conversation_unknown")
                session = dict(row)
                messages, has_more = self._normalized_messages(con, conversation_id, limit=limit, offset=offset)
                self._emit_telemetry(
                    {
                        "operation": "conversation.thread.select",
                        "conversation_id": conversation_id,
                        "message_count": len(messages),
                        "status": "ok",
                    }
                )
                return self._thread_view(
                    session,
                    messages,
                    limit=limit,
                    has_more=has_more,
                    cursor=_encode_cursor(offset + len(messages)) if has_more else None,
                )
        except sqlite3.Error:
            return self._envelope_error("domain_unavailable")

    # ------------------------------------------------------------------
    # conversation.project_scopes.list / conversation.project_scope.select
    # ------------------------------------------------------------------

    def project_scopes_list(self) -> dict:
        """Allowlisted scope rows only; freshness is two typed legs."""
        try:
            with self._connect() as con:
                sessions = self._sessions(con)
                grouped: dict[str, list[dict]] = {}
                for item in sessions:
                    cwd = item.get("cwd") or ""
                    grouped.setdefault(cwd, []).append(item)
                scopes: list[dict] = []
                for cwd in sorted(grouped):
                    group = grouped[cwd]
                    last_activity = max(
                        (item.get("last_activity") for item in group if item.get("last_activity")),
                        default=None,
                    )
                    scopes.append(
                        {
                            "project_scope_id": cwd,
                            "label": _basename(cwd),
                            "thread_count": len(group),
                            "last_activity_at": last_activity,
                            "freshness": self._freshness(),
                        }
                    )
                freshness = self._freshness()
                return {
                    "ok": True,
                    "data": {
                        "scopes": scopes,
                        "state": _overall(freshness),
                        "limitation": _limitation_text(freshness),
                    },
                }
        except sqlite3.Error:
            return self._envelope_error("domain_unavailable")

    def project_scope_select(self, *, project_scope_id: str, limit: int = 20, after: str | None = None) -> dict:
        """Read-only selected-scope projection with paginated recent-thread metadata."""
        limit = _normalize_limit(limit)
        try:
            offset = _decode_cursor(after)
        except ValueError:
            return self._envelope_error("cursor_invalid")
        try:
            with self._connect() as con:
                sessions = self._sessions(con)
                group = [item for item in sessions if (item.get("cwd") or "") == project_scope_id]
                if not group:
                    return self._envelope_error("unknown_scope")
                active = [item for item in group if item.get("last_activity") and item.get("message_count")]
                page = active[offset : offset + limit]
                has_more = (offset + len(page)) < len(active)
                threads = [
                    {
                        "conversation_id": item["session_id"],
                        "label": _basename(item.get("cwd") or ""),
                        "last_activity_at": item.get("last_activity"),
                        "message_count": item.get("message_count"),
                    }
                    for item in page
                ]
                freshness = self._freshness()
                self._emit_telemetry(
                    {
                        "operation": "conversation.project_scope.select",
                        "project_scope_id": project_scope_id,
                        "thread_count": len(threads),
                        "status": "ok",
                    }
                )
                return {
                    "ok": True,
                    "data": {
                        "project_scope_id": project_scope_id,
                        "label": _basename(project_scope_id),
                        "threads": threads,
                        "pagination": _pagination(
                            limit,
                            has_more,
                            _encode_cursor(offset + len(page)) if has_more else None,
                        ),
                        "freshness": freshness,
                        "state": _overall(freshness),
                        "limitation": _limitation_text(freshness),
                    },
                }
        except sqlite3.Error:
            return self._envelope_error("domain_unavailable")


# Compatibility exports; projection has a separate lifecycle responsibility.
from personal_knowledge.services.harness_model_projection import (
    HarnessModelProjectionError, HarnessModelProjectionProvider,
)


__all__ = [
    "MESSAGE_FIELDS",
    "SCOPE_ROW_FIELDS",
    "THREAD_METADATA_FIELDS",
    "HarnessConversationService",
    "HarnessModelProjectionError",
    "HarnessModelProjectionProvider",
    "MAX_LIMIT",
]
