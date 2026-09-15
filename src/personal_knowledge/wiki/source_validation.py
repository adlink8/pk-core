"""Read-only validation of derived claims against their current KU authority."""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Any, Mapping


def claim_evidence_refs(claim: Mapping[str, Any]) -> list[str]:
    refs = claim.get("evidence_refs")
    return list(dict.fromkeys(
        value for ref in (refs if isinstance(refs, list) else [])
        if isinstance(value := (ref.get("ref") if isinstance(ref, dict) else ref), str) and value.strip()
    ))


def _confidence(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = {"high": .9, "medium": .7, "low": .4}.get(str(value).lower())
    return number if number is not None and math.isfinite(number) and 0 <= number <= 1 else None


def current_claims(claims: list, authority: Path) -> list[dict[str, Any]]:
    """Keep only claims whose content and cited evidence remain current.

    This verifies the returned assertions, not topic completeness. No cached
    freshness label can override a changed, withdrawn or unavailable source.
    """
    if len(claims) > 512 or not Path(authority).is_file():
        return []
    candidates = [claim for claim in claims if isinstance(claim, dict)
                  and claim.get("claim_type") == "knowledge_unit" and claim.get("unit_id")]
    if not candidates:
        return []
    ids = list(dict.fromkeys(str(claim["unit_id"]) for claim in candidates))
    placeholders = ",".join("?" for _ in ids)
    try:
        con = sqlite3.connect(Path(authority).resolve().as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN")
            sources = {str(row["unit_id"]): dict(row) for row in con.execute(
                f"SELECT unit_id,unit_type,subject,question,answer,confidence,lifecycle,status,version "
                f"FROM knowledge_units WHERE unit_id IN ({placeholders})", ids)}
            evidence: dict[str, set[str]] = {}
            for row in con.execute(
                f"SELECT unit_id,evidence_ref FROM knowledge_unit_evidence WHERE unit_id IN ({placeholders})", ids):
                evidence.setdefault(str(row["unit_id"]), set()).add(str(row["evidence_ref"]))
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return []
    verified = []
    for claim in candidates:
        uid = str(claim["unit_id"])
        source = sources.get(uid)
        if not source or source["lifecycle"] != "current" or source["status"] != "current":
            continue
        if any(str(claim.get(key) or "") != str(source.get(key) or "")
               for key in ("answer", "question", "unit_type")):
            continue
        if "subject" in claim and str(claim["subject"] or "") != str(source["subject"] or ""):
            continue
        if "version" in claim and str(claim["version"]) != str(source["version"]):
            continue
        confidence = _confidence(claim.get("confidence"))
        if confidence is None or confidence != _confidence(source["confidence"]):
            continue
        refs = claim_evidence_refs(claim)
        if not refs or not set(refs).issubset(evidence.get(uid, set())):
            continue
        verified.append(claim)
    return verified
