"""Read-side access to consolidated wiki pages (Phase 4).

Wraps ``wiki_projection_pages`` reads with checksum validation and fail-safe
behaviour.  The read path (topic_projection) prefers these pages; every failure
mode (missing store, missing page, checksum drift) degrades to read-time
projection compute rather than raising.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from personal_knowledge.wiki.derived_store import ProjectionPage, latest_page, list_pages

PAGE_BODY_SCHEMA = "wiki_page_body_v1"


def subject_topic_id(subject: str) -> str:
    """Stable opaque topic id for a consolidated subject page.

    Mirrors ``opaque_topic_id`` for the canonical ``subject:{name}`` key shape
    so the read path and the consolidator derive identical ids.  Normalization
    (trim + lowercase) matches the consolidator's bucket key.
    """
    normalized = (subject or "").strip().lower()
    return "topic_" + hashlib.sha256(("subject:" + normalized).encode("utf-8")).hexdigest()[:32]


def subject_topic_key(subject: str) -> str:
    return "subject:" + (subject or "").strip().lower()


def page_checksum(page_body: str) -> str:
    """Recompute the canonical checksum of a stored page body (for validation)."""
    return hashlib.sha256(page_body.encode("utf-8")).hexdigest()


def parse_page_body(page: ProjectionPage) -> dict[str, Any] | None:
    """Parse and structurally validate a stored page body. Returns None on drift."""
    try:
        body = json.loads(page.page_body)
    except (TypeError, ValueError):
        return None
    if not isinstance(body, Mapping) or body.get("schema") != PAGE_BODY_SCHEMA:
        return None
    if page.page_checksum != page_checksum(page.page_body):
        return None
    return dict(body)


class WikiPageReader:
    """Fail-safe page store reader used by the topic projection service."""

    def __init__(self, store_path: Path | str) -> None:
        self.store_path = Path(store_path)

    def latest(self, topic_id: str) -> ProjectionPage | None:
        try:
            page = latest_page(self.store_path, topic_id)
        except Exception:  # noqa: BLE001 — store failures are read-time fallbacks
            return None
        if page is None:
            return None
        return page

    def latest_body(self, topic_id: str) -> tuple[ProjectionPage, dict[str, Any]] | None:
        page = self.latest(topic_id)
        if page is None:
            return None
        body = parse_page_body(page)
        if body is None:
            return None
        return page, body

    def directory(self, *, limit: int = 500) -> list[ProjectionPage]:
        try:
            return list_pages(self.store_path, limit=limit)
        except Exception:  # noqa: BLE001 — store failures degrade to an empty directory
            return []


__all__ = [
    "PAGE_BODY_SCHEMA", "WikiPageReader", "page_checksum", "parse_page_body",
    "subject_topic_id", "subject_topic_key",
]
