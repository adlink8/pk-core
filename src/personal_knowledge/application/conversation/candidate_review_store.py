"""Read-only views of persistent candidate review state and edits."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any
from personal_knowledge.application.conversation.candidate_review_contract import CandidateReviewError, effective_review_events, validate_edit_fields, _checksum

class CandidateReviewReadStore:
    def __init__(self, db_path, candidates):
        self.db_path = Path(db_path)
        self.candidates = candidates

    def _current_version(self, candidate_id: str) -> int:
        con = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT current_version FROM candidate_review_state WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            return int(row[0]) if row is not None else 1
        finally:
            con.close()

    def _lookup_idempotency(self, identity: str) -> dict[str, Any] | None:
        con = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT feedback_id, action, version, receipt_id, receipt_checksum, request_checksum "
                "FROM candidate_review_feedback WHERE idempotency_identity=?",
                (identity,),
            ).fetchone()
            if row is None:
                return None
            return {
                "feedback_id": str(row[0]),
                "action": str(row[1]),
                "version": int(row[2]),
                "receipt_id": str(row[3]),
                "receipt_checksum": str(row[4]),
                "request_checksum": row[5],
            }
        finally:
            con.close()

    def _feedback_exists(self, candidate_id: str, feedback_id: str) -> bool:
        con = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT 1 FROM candidate_review_feedback WHERE candidate_id=? AND feedback_id=?",
                (candidate_id, feedback_id),
            ).fetchone()
            return row is not None
        finally:
            con.close()

    def feedback_history(self, candidate_id: str) -> tuple[dict[str, Any], ...]:
        """Append-only immutable reversible calibration history in review order.

        Only the reversible review gestures (``ignore``/``undo``) surface here:
        they are the entries an ``undo`` can reference and the calibration
        feedback the loop records (D-25). Confirmed accept/edit receipts are
        still bound to metadata-only ledger rows so an exact replay deduplicates
        with the same feedback id/receipt, but a confirmed accept/edit is not a
        reversible gesture and never appears in this reversible-history view.
        """
        con = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT feedback_id, candidate_id, action, version, receipt_id, "
                "receipt_checksum, referenced_feedback_id, disposition, recorded_at "
                "FROM candidate_review_feedback WHERE candidate_id=? "
                "AND action IN ('ignore', 'undo') ORDER BY rowid",
                (candidate_id,),
            ).fetchall()
            return tuple({
                "feedback_id": str(row[0]),
                "candidate_id": str(row[1]),
                "action": str(row[2]),
                "version": int(row[3]),
                "receipt_id": str(row[4]),
                "receipt_checksum": str(row[5]),
                "referenced_feedback_id": row[6],
                "disposition": row[7],
                "recorded_at": str(row[8]),
            } for row in rows)
        finally:
            con.close()

    def _snapshot(self):
        if not self.db_path.is_file():
            return [], {}, {}
        con = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN")
            rows = [dict(row) for row in con.execute("SELECT * FROM candidate_review_feedback ORDER BY rowid")]
            edits = {str(row[0]): (row[1], row[2]) for row in con.execute("SELECT feedback_id,fields_json,fields_checksum FROM candidate_review_edits")}
            versions = {str(row[0]): int(row[1]) for row in con.execute("SELECT candidate_id,current_version FROM candidate_review_state")}
            return rows, edits, versions
        finally:
            con.close()

    def resolved_candidates(self) -> dict[str, dict[str, Any]]:
        rows, edits, _ = self._snapshot()
        return self._apply_edits(rows, edits)

    def _apply_edits(self, rows, edits):
        result = {key: dict(value) for key, value in self.candidates.items()}
        for cid, candidate in result.items():
            for row in effective_review_events(cid, rows):
                fid = str(row.get("feedback_id") or "")
                if row.get("action") != "edit" or fid not in edits:
                    continue
                try:
                    fields_json, fields_checksum = edits[fid]
                    fields = json.loads(fields_json)
                    validate_edit_fields(fields)
                    if _checksum(fields) == fields_checksum:
                        candidate.update(fields)
                except (CandidateReviewError, TypeError, ValueError):
                    continue
        return result

    def list_candidates(self, *, scope: str, limit: int = 20, cursor: str = "") -> dict[str, Any]:
        """Reviewable bounded view; never invent acceptance for missing feedback."""
        feedback, edits, versions = self._snapshot()
        candidates = self._apply_edits(feedback, edits)
        ids = sorted(key for key, value in candidates.items() if str(value.get("scope") or "") == scope and key > cursor)
        selected = ids[:limit]
        items = []
        for cid in selected:
            candidate = candidates[cid]
            active = effective_review_events(cid, feedback)
            latest = active[-1] if active else {}
            status = "accepted" if latest.get("action") in {"accept", "edit"} else "ignored" if latest.get("action") == "ignore" else "pending"
            history = [row for row in feedback if row["candidate_id"] == cid]
            items.append({
                "candidate_id": cid, "candidate_checksum": candidate.get("candidate_checksum"),
                "subject": candidate.get("subject"), "scope": scope,
                "conclusion": candidate.get("conclusion") or "",
                "confidence": candidate.get("confidence"), "uncertainty": candidate.get("uncertainty"),
                "support_refs": list(candidate.get("support_refs") or []),
                "conflict_refs": list(candidate.get("conflict_refs") or []),
                "high_impact": bool(candidate.get("high_impact")),
                "latest_feedback_id": history[-1]["feedback_id"] if history else None,
                "current_version": versions.get(cid, 1), "review_status": status,
                "content_status": "available" if candidate.get("conclusion") else "extraction_required",
                "provenance_class": "inference",
            })
        return {"candidates": items, "scope": scope, "next_cursor": selected[-1] if len(ids) > limit else None}
