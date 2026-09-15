"""知识层分发契约：CLI backend / REST / MCP 共用 get_knowledge_status。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "integration" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import SCHEMA_SQL  # noqa: E402
import personal_knowledge.retrieval.unified_search as us  # noqa: E402
import personal_knowledge.retrieval._constants as _constants  # noqa: E402
import personal_knowledge.services.api_server as api_server  # noqa: E402
import personal_knowledge.services.mcp_server as mcp  # noqa: E402



def _assert_ssot_fields(status: dict) -> None:
    """Phase 15: ssot / fallback_policy always present on get_knowledge_status."""
    assert "ssot" in status
    assert status["ssot"]["dialogue"] == "agentsview_canonical"
    assert status["ssot"]["knowledge"] == "canonical_knowledge_units"
    assert status["ssot"]["non_dialogue_raw"] == "personal_events"
    # Wave2 default is layered (env PERSONAL_DATA_FALLBACK_POLICY may override)
    assert status["fallback_policy"] in ("legacy", "layered")
    assert status["fallback_policy"] == us._resolve_fallback_policy(None)

def _setup_ku_db(db: Path, collection: str = "ku_active_test") -> None:
    con = sqlite3.connect(str(db))
    con.executescript(SCHEMA_SQL)
    con.execute(
        "INSERT INTO knowledge_build_runs VALUES "
        "('run1','extraction','2026-01-01',NULL,'h','v1','v1','m',NULL,NULL,NULL,NULL,'current',NULL,NULL)"
    )
    con.execute(
        "INSERT INTO knowledge_index_versions VALUES "
        "('v1','run1',?,'run1',42,'active','2026-01-01','2026-01-02','abc123checksum')"
        ,
        (collection,),
    )
    con.execute(
        "INSERT INTO canonical_knowledge_units "
        "(canonical_unit_id, subject, unit_type, question, answer, confidence, "
        "lifecycle, status, version, run_id, created_at) VALUES "
        "('cu1','s','preference','q','a',0.9,'current','current',1,'run1','2026-01-01')"
    )
    con.commit()
    con.close()


def test_get_knowledge_status_no_pointer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import personal_knowledge.core.project_paths as paths

    monkeypatch.setattr(paths, "DB_DIR", tmp_path)
    monkeypatch.setattr(_constants, "DB_DIR", tmp_path)
    monkeypatch.setattr(_constants, "UNIFIED_DB", tmp_path / "missing.sqlite")
    status = us.get_knowledge_status(probe_chroma=False)
    assert status["available"] is False
    assert status["active_collection"] is None
    assert "fallback" in status["route_policy"]
    assert status["route_policy"].startswith(("wiki-first", "knowledge-first"))
    _assert_ssot_fields(status)
    assert "cli" in status["semantic_routes"]
    assert "rest" in status["semantic_routes"]
    assert "mcp" in status["semantic_routes"]


def test_get_knowledge_status_reads_pointer_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import personal_knowledge.core.project_paths as paths

    db = tmp_path / "ps.sqlite"
    _setup_ku_db(db, "ku_active_test")
    (tmp_path / "knowledge_index_active.txt").write_text("ku_active_test", encoding="utf-8")
    monkeypatch.setattr(paths, "DB_DIR", tmp_path)
    monkeypatch.setattr(_constants, "DB_DIR", tmp_path)
    monkeypatch.setattr(_constants, "UNIFIED_DB", db)

    status = us.get_knowledge_status(probe_chroma=False)
    assert status["available"] is True
    assert status["active_collection"] == "ku_active_test"
    assert status["db_unit_count"] == 42
    assert status["unit_count"] == 42
    assert status["version"]["status"] == "active"
    assert status["canonical_current_count"] == 1
    assert status["chroma_available"] is False
    _assert_ssot_fields(status)


def test_stats_embeds_knowledge_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import personal_knowledge.core.project_paths as paths

    db = tmp_path / "ps.sqlite"
    # minimal unified_events for stats()
    con = sqlite3.connect(str(db))
    con.executescript(SCHEMA_SQL)
    con.execute(
        "CREATE TABLE IF NOT EXISTS unified_events ("
        "event_id TEXT PRIMARY KEY, source TEXT, month TEXT)"
    )
    con.execute("INSERT INTO unified_events VALUES ('e1','Agent','2026-01')")
    con.execute(
        "INSERT INTO knowledge_build_runs VALUES "
        "('run1','extraction','2026-01-01',NULL,'h','v1','v1','m',NULL,NULL,NULL,NULL,'current',NULL,NULL)"
    )
    con.execute(
        "INSERT INTO knowledge_index_versions VALUES "
        "('v1','run1','ku_x','run1',3,'active','2026-01-01','2026-01-01',NULL)"
    )
    con.commit()
    con.close()
    (tmp_path / "knowledge_index_active.txt").write_text("ku_x", encoding="utf-8")
    monkeypatch.setattr(paths, "DB_DIR", tmp_path)
    monkeypatch.setattr(_constants, "DB_DIR", tmp_path)
    monkeypatch.setattr(_constants, "UNIFIED_DB", db)

    real_status = us.get_knowledge_status

    def _status(*, probe_chroma: bool = True):
        return real_status(probe_chroma=False)

    monkeypatch.setattr(us, "get_knowledge_status", _status)
    st = us.stats()
    assert st["total_events"] == 1
    assert "knowledge" in st
    assert st["knowledge"]["active_collection"] == "ku_x"


def test_mcp_tools_include_knowledge_status() -> None:
    names = {t.name for t in mcp.TOOLS}
    assert "knowledge_status" in names
    assert "search_semantic" in names
    tool = next(t for t in mcp.TOOLS if t.name == "search_semantic")
    assert "knowledge-first" in (tool.description or "").lower() or "knowledge" in (
        tool.description or ""
    ).lower()


def test_format_knowledge_status_text() -> None:
    text = mcp._format_knowledge_status(
        {
            "available": True,
            "active_collection": "ku_test",
            "unit_count": 10,
            "db_unit_count": 10,
            "canonical_current_count": 8,
            "route_policy": "knowledge-first + raw fallback",
            "fallback_policy": "legacy",
            "ssot": {
                "dialogue": "agentsview_canonical",
                "knowledge": "canonical_knowledge_units",
                "non_dialogue_raw": "personal_events",
            },
            "chroma_available": False,
            "semantic_routes": {"cli": "semantic", "rest": "POST /search/semantic", "mcp": "search_semantic"},
        }
    )
    assert "ku_test" in text
    assert "knowledge-first" in text
    assert "POST /search/semantic" in text
    assert "legacy" in text
    assert "agentsview_canonical" in text


def test_api_knowledge_and_health_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import personal_knowledge.core.project_paths as paths
    import socket

    db = tmp_path / "ps.sqlite"
    _setup_ku_db(db, "ku_api")
    (tmp_path / "knowledge_index_active.txt").write_text("ku_api", encoding="utf-8")
    monkeypatch.setattr(paths, "DB_DIR", tmp_path)
    monkeypatch.setattr(_constants, "DB_DIR", tmp_path)
    monkeypatch.setattr(_constants, "UNIFIED_DB", db)
    monkeypatch.setattr(api_server, "backend", us)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = api_server.ThreadingHTTPServer(("127.0.0.1", port), api_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for key in ("NO_PROXY", "no_proxy"):
            os.environ[key] = "127.0.0.1,localhost"

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/knowledge?no_chroma=1", timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body["ok"] is True
        assert body["data"]["active_collection"] == "ku_api"
        assert body["data"]["db_unit_count"] == 42
        _assert_ssot_fields(body["data"])

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        assert health["ok"] is True
        assert health["data"]["knowledge"]["active_collection"] == "ku_api"
    finally:
        server.shutdown()
