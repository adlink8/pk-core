"""Typed, privacy-aware evidence resolver for product serving."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB, ANALYSIS_DIR, GOOGLE_DB, UNIFIED_DB

DEFAULT_TURNS_ARTIFACT = ANALYSIS_DIR / "ai_context" / "conversation_summaries.json"


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')}


def _table(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


class EvidenceResolver:
    def __init__(self, *, unified_db: Path = UNIFIED_DB, conversation_db: Path = AGENT_CONVERSATIONS_DB, google_db: Path = GOOGLE_DB, turns_artifact: Path = DEFAULT_TURNS_ARTIFACT):
        self.unified_db = unified_db
        self.conversation_db = conversation_db
        self.google_db = google_db
        self.turns_artifact = turns_artifact
        self._turn_index_cache: dict[str, dict[str, Any]] | None = None

    @staticmethod
    def _result(ref: str, artifact_type: str, status: str, **kwargs: Any) -> dict[str, Any]:
        return {"ref": ref, "artifact_type": artifact_type, "status": status, **kwargs}

    def resolve(self, ref: str, *, artifact_type: str | None = None, include_content: bool = False, source_version: str | None = None) -> dict[str, Any]:
        if artifact_type is None:
            if ref.startswith("cm|") or ref.startswith("v2|cm|"):
                artifact_type = "canonical_message"
            elif ref.startswith("g|"):
                artifact_type = "google_signal"
            elif ref.startswith("ct|") or ref.startswith("turn|"):
                artifact_type = "turn"
            else:
                artifact_type = "knowledge_unit"
        handlers = {
            "canonical_message": self._message,
            "google_signal": self._google,
            "turn": self._turn,
            "knowledge_unit": self._knowledge,
        }
        handler = handlers.get(artifact_type)
        if handler is None:
            return self._result(ref, artifact_type, "unknown_type", source_version=source_version)
        result = handler(ref, include_content)
        result["source_version"] = source_version
        return result

    def _message(self, ref: str, include: bool) -> dict[str, Any]:
        con = _ro(self.conversation_db)
        if con is None or not _table(con, "canonical_messages"):
            if con: con.close()
            return self._result(ref, "canonical_message", "missing")
        cols = _columns(con, "canonical_messages")
        wanted = ["canonical_message_id"] + [x for x in ("canonical_session_id", "role", "content", "timestamp", "source", "evidence_scope", "is_system") if x in cols]
        row = con.execute(f"SELECT {','.join(wanted)} FROM canonical_messages WHERE canonical_message_id=?", (ref,)).fetchone()
        eligible = True
        if row and "canonical_session_id" in row.keys() and _table(con, "canonical_sessions"):
            scols = _columns(con, "canonical_sessions")
            if "evidence_eligible" in scols:
                s = con.execute("SELECT evidence_eligible FROM canonical_sessions WHERE canonical_session_id=?", (row["canonical_session_id"],)).fetchone()
                eligible = bool(s and s[0])
        if row and "evidence_scope" in row.keys():
            # Phase 41-04：scope 放宽为 user+assistant 双轨放行（assistant 轨
            # KU 的证据链下钻不再被 veto）。红线不动：session 级
            # evidence_eligible=0 与 is_system=1 的 veto 保持原样；
            # 'system'/'sidechain'/'subagent' 等其他 scope 依然 ineligible。
            eligible = eligible and str(row["evidence_scope"] or "user") in ("user", "assistant")
        if row and "is_system" in row.keys():
            eligible = eligible and not bool(row["is_system"])
        data = dict(row) if row else None
        con.close()
        if not data:
            return self._result(ref, "canonical_message", "missing")
        content = data.pop("content", None)
        if not eligible:
            return self._result(ref, "canonical_message", "ineligible", eligible=False, metadata=data)
        return self._result(ref, "canonical_message", "ok", eligible=True, metadata=data, content=content if include else None)

    def _knowledge(self, ref: str, include: bool) -> dict[str, Any]:
        con = _ro(self.unified_db)
        if con is None:
            return self._result(ref, "knowledge_unit", "missing")
        table = "canonical_knowledge_units" if _table(con, "canonical_knowledge_units") else "knowledge_units"
        key = "canonical_unit_id" if table.startswith("canonical") else "unit_id"
        row = con.execute(f"SELECT * FROM {table} WHERE {key}=?", (ref,)).fetchone()
        refs: list[str] = []
        if (
            row
            and table.startswith("canonical")
            and _table(con, "canonical_unit_members")
            and _table(con, "knowledge_units")
        ):
            refs = [
                str(x[0])
                for x in con.execute(
                    "SELECT u.source_message_ref FROM knowledge_units u "
                    "JOIN canonical_unit_members m ON m.member_unit_id=u.unit_id "
                    "WHERE m.canonical_unit_id=? AND COALESCE(u.source_message_ref,'')<>'' "
                    "ORDER BY m.id",
                    (ref,),
                ).fetchall()
            ]
        elif row and _table(con, "knowledge_unit_evidence"):
            refs = [
                str(x[0])
                for x in con.execute(
                    "SELECT evidence_ref FROM knowledge_unit_evidence WHERE unit_id=?",
                    (ref,),
                ).fetchall()
            ]
        data = dict(row) if row else None
        con.close()
        if not data:
            return self._result(ref, "knowledge_unit", "missing")
        content = data.pop("answer", None)
        data.pop("evidence_quote", None)
        if refs and not data.get("source_message_ref"):
            data["source_message_ref"] = refs[0]
        return self._result(ref, "knowledge_unit", "ok", eligible=True, evidence_refs=refs, metadata=data, content=content if include else None)

    def _turn(self, ref: str, include: bool) -> dict[str, Any]:
        con = _ro(self.unified_db)
        data = None
        if con is not None and _table(con, "conversation_turns_summary"):
            cols = _columns(con, "conversation_turns_summary")
            key = "turn_id" if "turn_id" in cols else "id"
            row = con.execute(f"SELECT * FROM conversation_turns_summary WHERE {key}=?", (ref,)).fetchone()
            data = dict(row) if row else None
        if con is not None:
            con.close()
        if data is None:
            data = self._turn_index().get(ref)
        if not data:
            return self._result(ref, "turn", "missing")
        data = dict(data)
        content = data.pop("narrative", data.pop("content", None))
        return self._result(ref, "turn", "ok", eligible=True, metadata=data, content=content if include else None)

    def _turn_index(self) -> dict[str, dict[str, Any]]:
        if self._turn_index_cache is not None:
            return self._turn_index_cache
        index: dict[str, dict[str, Any]] = {}
        if self.turns_artifact.exists():
            payload = json.loads(self.turns_artifact.read_text(encoding="utf-8"))
            sessions = payload if isinstance(payload, list) else payload.get("sessions", [])
            for session in sessions:
                session_id = str(session.get("session_id") or "")
                if not session_id:
                    continue
                for turn in session.get("turn_summaries") or []:
                    turn_id = str(turn.get("turn_id") or "")
                    if not turn_id:
                        continue
                    index[f"{session_id}#{turn_id}"] = {
                        **dict(turn),
                        "session_id": session_id,
                    }
        self._turn_index_cache = index
        return index

    def _google(self, ref: str, include: bool) -> dict[str, Any]:
        con = _ro(self.google_db)
        if con is None:
            return self._result(ref, "google_signal", "missing")
        row = None
        table = ""
        for candidate, key in (("google_light_assertions", "assertion_id"), ("normalized_events", "event_id")):
            if _table(con, candidate) and key in _columns(con, candidate):
                row = con.execute(f"SELECT * FROM {candidate} WHERE {key}=?", (ref,)).fetchone()
                if row:
                    table = candidate
                    break
        data = dict(row) if row else None
        con.close()
        if not data:
            return self._result(ref, "google_signal", "missing")
        privacy = str(data.get("privacy_tier") or data.get("privacy_class") or "R4")
        eligible = privacy not in {"secret", "blocked"}
        content = data.pop("claim", data.pop("content", data.pop("title", None)))
        if not eligible:
            return self._result(ref, "google_signal", "ineligible", eligible=False, metadata={"table": table, "privacy": privacy})
        return self._result(ref, "google_signal", "ok", eligible=True, metadata={"table": table, **data}, content=content if include else None)
