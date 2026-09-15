"""记忆治理元数据公共函数。

把 Phase 05 需要统一的 evidence/confidence/last_seen/source_hash/merge_key
收口到一个文件,避免四个 build_*memory 脚本各写一套。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from personal_knowledge.core.common import sha256_text


def unique_evidence_ids(evidence_ids: Iterable[str], limit: int = 50) -> list[str]:
    """去重保序,并截断到 limit。"""
    out: list[str] = []
    seen: set[str] = set()
    for value in evidence_ids:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
        if len(out) >= limit:
            break
    return out


def load_last_seen(con: sqlite3.Connection, evidence_ids: list[str]) -> str:
    """根据证据事件求最近一次出现时间。"""
    if not evidence_ids:
        return ""
    placeholders = ",".join("?" * len(evidence_ids))
    row = con.execute(
        f"SELECT MAX(event_time) FROM unified_events WHERE event_id IN ({placeholders})",
        evidence_ids,
    ).fetchone()
    return str(row[0] or "")


def build_governance_metadata(
    *,
    source: str,
    evidence_ids: list[str],
    confidence: float,
    merge_key: str,
    last_seen: str,
    extra: dict | None = None,
) -> dict:
    """组装标准化 governance metadata。"""
    payload = dict(extra or {})
    payload["evidence_ids"] = unique_evidence_ids(evidence_ids, limit=50)
    payload["confidence"] = round(float(confidence), 4)
    payload["last_seen"] = last_seen or str(payload.get("last_seen") or "")
    payload["merge_key"] = merge_key
    payload["source"] = source
    payload["source_hash"] = sha256_text(
        json.dumps(
            {
                "source": payload["source"],
                "merge_key": payload["merge_key"],
                "evidence_ids": payload["evidence_ids"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return payload
