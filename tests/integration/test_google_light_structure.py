"""Phase 16: Google normalized_events + light assertions + lifecycle."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "integration" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.application.build_google_normalized_events import build as build_norm  # noqa: E402
from personal_knowledge.application.build_google_light_assertions import (  # noqa: E402
    build as build_assert,
    _is_restricted,
)
from personal_knowledge.application.google_structure_lifecycle import activity_event_id  # noqa: E402
from personal_knowledge.retrieval.unified_search import (  # noqa: E402
    list_google_light_assertions,
    get_google_light_assertion,
)


def _seed_google_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE activities (
            id INTEGER PRIMARY KEY,
            service TEXT,
            event_at TEXT,
            month TEXT,
            action TEXT,
            category TEXT,
            title_or_query TEXT,
            channel_or_source TEXT,
            domain TEXT,
            url TEXT,
            raw_excerpt TEXT,
            source_dataset TEXT
        );
        """
    )
    rows = [
        (1, "Search", "2026-01-01", "2026-01", "search", "AI / 编程 / 工具", "python fastapi", "", "example.com", "", "python fastapi", "t"),
        (2, "Search", "2026-01-02", "2026-01", "search", "AI / 编程 / 工具", "sqlite fts", "", "example.com", "", "sqlite fts", "t"),
        (3, "Search", "2026-01-03", "2026-01", "search", "AI / 编程 / 工具", "chroma vector", "", "docs.com", "", "chroma", "t"),
        (4, "Search", "2026-01-04", "2026-01", "search", "AI / 编程 / 工具", "rag pipeline", "", "docs.com", "", "rag", "t"),
        (5, "Search", "2026-01-05", "2026-01", "search", "AI / 编程 / 工具", "embedding model", "", "docs.com", "", "embed", "t"),
        (6, "YouTube", "2026-01-06", "2026-01", "watch", "娱乐 / 体育 / 生活内容", "video a", "ChannelX", "youtube.com", "", "vid", "t"),
        (7, "YouTube", "2026-01-07", "2026-01", "watch", "娱乐 / 体育 / 生活内容", "video b", "ChannelX", "youtube.com", "", "vid", "t"),
        (8, "YouTube", "2026-01-08", "2026-01", "watch", "娱乐 / 体育 / 生活内容", "video c", "ChannelX", "youtube.com", "", "vid", "t"),
        (9, "Maps", "2026-01-09", "2026-01", "view", "地图 / 地点 / 本地生活", "some place", "", "", "", "place", "t"),
        (10, "Search", "2026-01-10", "2026-01", "search", "支付 / 金融 / 卡", "bank stuff", "", "bank.com", "", "pay", "t"),
        # Location intent via Search/Gemini (not Maps service) must still be restricted.
        (11, "Search", "2026-01-11", "2026-01", "search", "地图 / 地点 / 本地生活", "附近咖啡", "", "maps.google.com", "", "loc", "t"),
        (12, "Gemini Apps", "2026-01-12", "2026-01", "chat", "地图 / 地点 / 本地生活", "怎么去机场", "", "", "", "nav", "t"),
    ]
    con.executemany(
        "INSERT INTO activities VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()


def test_is_restricted() -> None:
    assert _is_restricted("Maps", "anything")
    assert _is_restricted("Search", "支付 / 金融 / 卡")
    # Policy: location intent restricted by category/content, not Maps service alone.
    assert _is_restricted("Search", "地图 / 地点 / 本地生活")
    assert _is_restricted("Gemini Apps", "地图 / 地点 / 本地生活")
    assert _is_restricted("Search", "路线导航怎么走")
    assert not _is_restricted("Search", "AI / 编程 / 工具")


def test_normalized_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "g.sqlite"
    _seed_google_db(db)
    s1 = build_norm(db, write=True)
    assert s1.written == 12
    assert s1.after_count == 12
    s2 = build_norm(db, write=True)
    assert s2.after_count == 12
    con = sqlite3.connect(str(db))
    eid = con.execute("SELECT event_id FROM normalized_events LIMIT 1").fetchone()[0]
    assert str(eid).startswith("g|")
    con.close()


def test_light_assertions_privacy(tmp_path: Path) -> None:
    db = tmp_path / "g.sqlite"
    _seed_google_db(db)
    build_norm(db, write=True)
    stats, assertions = build_assert(db, write=True)
    assert stats.assertions >= 1
    # Maps / payment / location-category not as interest subjects
    subjects = {a["subject"] for a in assertions}
    assert "Maps" not in subjects
    assert not any("支付" in s for s in subjects)
    assert not any("地图" in s or "地点" in s for s in subjects)
    # programming topic should appear
    types = {a["assertion_type"] for a in assertions}
    assert "interest_topic" in types or "frequent_service" in types
    con = sqlite3.connect(str(db))
    n = con.execute("SELECT COUNT(*) FROM google_light_assertions").fetchone()[0]
    assert n == len(assertions)
    con.close()


def test_dry_run_no_write(tmp_path: Path) -> None:
    db = tmp_path / "g.sqlite"
    _seed_google_db(db)
    s = build_norm(db, write=False)
    assert s.written == 12
    con = sqlite3.connect(str(db))
    # table may exist after ensure but empty if dry-run without write path creating empty - build dry-run still runs ENSURE
    n = con.execute("SELECT COUNT(*) FROM normalized_events").fetchone()[0]
    assert n == 0
    con.close()


def test_normalized_deletes_orphans_and_stable_event_id(tmp_path: Path) -> None:
    db = tmp_path / "g.sqlite"
    _seed_google_db(db)
    build_norm(db, write=True)
    con = sqlite3.connect(str(db))
    eid = con.execute(
        "SELECT event_id FROM normalized_events WHERE source_file_id='1'"
    ).fetchone()[0]
    assert eid == activity_event_id(1)
    # Delete an activity and rebuild
    con.execute("DELETE FROM activities WHERE id=12")
    con.commit()
    con.close()
    s = build_norm(db, write=True)
    assert s.deleted_orphans >= 1
    con = sqlite3.connect(str(db))
    n = con.execute("SELECT COUNT(*) FROM normalized_events").fetchone()[0]
    assert n == 11
    # Title edit keeps same event_id
    con.execute(
        "UPDATE activities SET title_or_query='python fastapi changed' WHERE id=1"
    )
    con.commit()
    con.close()
    build_norm(db, write=True)
    con = sqlite3.connect(str(db))
    eid2 = con.execute(
        "SELECT event_id FROM normalized_events WHERE source_file_id='1'"
    ).fetchone()[0]
    assert eid2 == eid
    assert con.execute("SELECT COUNT(*) FROM google_structure_runs").fetchone()[0] >= 1
    con.close()


def test_assertions_stage_promote_and_consumer(tmp_path: Path) -> None:
    db = tmp_path / "g.sqlite"
    _seed_google_db(db)
    build_norm(db, write=True)
    stats, assertions = build_assert(db, write=True)
    assert stats.gate_passed is True
    assert stats.promoted is True
    assert stats.run_id
    con = sqlite3.connect(str(db))
    cur = con.execute(
        "SELECT COUNT(*) FROM google_light_assertions WHERE status='current'"
    ).fetchone()[0]
    assert cur == len(assertions)
    assert cur == stats.assertions
    con.close()

    pack = list_google_light_assertions(db_path=db, limit=20)
    assert pack["not_knowledge_unit"] is True
    assert pack["total"] == stats.assertions
    assert pack["items"]
    assert pack["items"][0]["kind"] == "google_light_assertion"
    aid = pack["items"][0]["assertion_id"]
    one = get_google_light_assertion(aid, db_path=db)
    assert one is not None
    assert one["assertion_id"] == aid
    assert one["not_knowledge_unit"] is True
