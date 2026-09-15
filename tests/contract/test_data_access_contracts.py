"""Data access contract tests for /data REST routes and backend pure functions."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path


for _key in ("NO_PROXY", "no_proxy"):
    existing = os.environ.get(_key)
    if existing:
        missing = [host for host in ("127.0.0.1", "localhost") if host not in existing]
        if missing:
            os.environ[_key] = existing + "," + ",".join(missing)
    else:
        os.environ[_key] = "127.0.0.1,localhost"


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "integration" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import personal_knowledge.services.api_server as api_server  # noqa: E402
import personal_knowledge.retrieval.unified_search as us  # noqa: E402
import personal_knowledge.retrieval._constants as _constants  # noqa: E402


def _http_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _build_test_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE unified_events (
            event_id TEXT PRIMARY KEY,
            source TEXT,
            source_table TEXT,
            source_id TEXT,
            event_type TEXT,
            service TEXT,
            event_time TEXT,
            month TEXT,
            title TEXT,
            content TEXT,
            category TEXT,
            url TEXT,
            domain TEXT,
            file_name TEXT,
            session_id TEXT,
            weight REAL
        );
        CREATE TABLE unified_events_rich (
            event_id TEXT PRIMARY KEY,
            content_rich TEXT,
            content_rich_source TEXT
        );
        CREATE TABLE event_categories_v2 (
            event_id TEXT PRIMARY KEY,
            category_v1 TEXT,
            category_v2 TEXT
        );
        CREATE TABLE memory_items (
            memory_id TEXT PRIMARY KEY,
            memory_type TEXT,
            memory_subtype TEXT,
            subject TEXT,
            description TEXT,
            confidence REAL,
            evidence_count INTEGER,
            metadata TEXT,
            created_at TEXT
        );
        CREATE TABLE memory_links (
            id INTEGER PRIMARY KEY,
            memory_id TEXT,
            target_type TEXT,
            target_id TEXT,
            relation TEXT
        );
        CREATE TABLE memory_relations (
            id INTEGER PRIMARY KEY,
            from_memory_id TEXT,
            to_memory_id TEXT,
            relation TEXT,
            strength REAL
        );
        CREATE TABLE memory_relation_judgments (
            candidate_id TEXT PRIMARY KEY,
            package_id TEXT,
            source_memory_id TEXT,
            target_memory_id TEXT,
            relation_type TEXT,
            confidence REAL,
            evidence_refs_json TEXT,
            source_refs_json TEXT,
            risk_flags_json TEXT,
            candidate_reason TEXT,
            gate_status TEXT,
            gate_reasons_json TEXT,
            model TEXT,
            prompt_version TEXT,
            llm_status TEXT,
            created_at TEXT
        );
        """
    )
    events = [
        ("e1", "GPT", "chat", "1", "message", "ChatGPT", "2026-01-01T10:00:00", "2026-01", "Alpha", "alpha content", "old", "", "", "", "s1", 1.0),
        ("e2", "Agent", "session", "2", "action", "Codex", "2026-01-02T10:00:00", "2026-01", "Beta", "beta content", "old", "", "", "", "s2", 1.0),
        ("e3", "Google", "activity", "3", "activity", "Gemini", "2026-02-01T10:00:00", "2026-02", "Gamma", "gamma content", "old", "", "", "", "s3", 1.0),
        ("e4", "GPT", "chat", "4", "message", "ChatGPT", "2026-02-10T10:00:00", "2026-02", "Delta", "delta content", "old", "", "", "", "s4", 1.0),
    ]
    con.executemany("INSERT INTO unified_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", events)
    con.executemany(
        "INSERT INTO unified_events_rich VALUES (?,?,?)",
        [
            ("e1", "alpha rich", "test"),
            ("e2", "beta rich", "test"),
            ("e3", "gamma rich", "test"),
            ("e4", "delta rich", "test"),
        ],
    )
    con.executemany(
        "INSERT INTO event_categories_v2 VALUES (?,?,?)",
        [
            ("e1", "", "编程 / 调试"),
            ("e2", "", "编程 / 工具"),
            ("e3", "", "生活 / 计划"),
            ("e4", "", "编程 / 调试"),
        ],
    )
    con.executemany(
        "INSERT INTO memory_items VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("m1", "tooling", "editor", "Codex", "Uses Codex", 0.9, 2, '{"source":"test"}', "2026-01-01T00:00:00"),
            ("m2", "preference", "language", "Chinese", "Prefers Chinese", 0.8, 1, "{}", "2026-01-02T00:00:00"),
        ],
    )
    con.execute("INSERT INTO memory_links VALUES (1,'m1','event','e2','evidenced_by')")
    con.execute("INSERT INTO memory_relations VALUES (1,'m1','m2','prefers',0.7)")
    con.execute(
        "INSERT INTO memory_relation_judgments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "cand-1",
            "pkg-1",
            "m1",
            "m2",
            "uses_tool",
            0.91,
            "[]",
            "[]",
            "[]",
            "Codex is used as a coding tool",
            "accepted",
            "[]",
            "gpt-5.4",
            "test",
            "ok",
            "2026-01-03T00:00:00",
        ),
    )
    con.commit()
    con.close()


class LocalApiServer:
    def __enter__(self) -> "LocalApiServer":
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            self.port = int(s.getsockname()[1])
        self.server = api_server.ThreadingHTTPServer(("127.0.0.1", self.port), api_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class DataAccessContractTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "contracts.sqlite"
        _build_test_db(cls.db_path)
        cls.original_db = _constants.UNIFIED_DB
        _constants.UNIFIED_DB = cls.db_path

    @classmethod
    def tearDownClass(cls) -> None:
        _constants.UNIFIED_DB = cls.original_db
        cls.tmp.cleanup()

    def test_list_events_pagination_and_default_fields(self) -> None:
        data = us.list_events_contract(limit=2, offset=1)

        self.assertTrue(data["ok"])
        self.assertEqual(data["total"], 4)
        self.assertEqual(data["count"], 2)
        self.assertEqual([item["event_id"] for item in data["items"]], ["e3", "e2"])
        self.assertNotIn("content", data["items"][0])
        self.assertNotIn("content_rich", data["items"][0])

    def test_event_filters_and_explicit_content_fields(self) -> None:
        by_time = us.list_events_contract(time_from="2026-01-01", time_to="2026-01-31T23:59:59", order="asc")
        by_source_service_category = us.list_events_contract(source="Agent", service="Codex", category="工具")
        with_content = us.list_events_contract(fields="event_id,content,content_rich", source="Agent")

        self.assertEqual([item["event_id"] for item in by_time["items"]], ["e1", "e2"])
        self.assertEqual([item["event_id"] for item in by_source_service_category["items"]], ["e2"])
        self.assertEqual(with_content["items"][0]["content"], "beta content")
        self.assertEqual(with_content["items"][0]["content_rich"], "beta rich")

    def test_aggregate_and_timeline_contracts(self) -> None:
        agg = us.aggregate_contract(group_by="source")
        memory_agg = us.aggregate_contract(group_by="memory_type")
        relation_agg = us.aggregate_contract(group_by="relation_type")
        timeline = us.timeline_contract(interval="month")

        self.assertEqual({row["source"]: row["count"] for row in agg["items"]}, {"GPT": 2, "Google": 1, "Agent": 1})
        self.assertEqual({row["memory_type"]: row["count"] for row in memory_agg["items"]}, {"tooling": 1, "preference": 1})
        self.assertEqual(relation_agg["items"][0]["relation_type"], "prefers")
        self.assertEqual(timeline["items"], [{"bucket": "2026-01", "count": 2}, {"bucket": "2026-02", "count": 2}])

    def test_memory_id_query_and_relation_list(self) -> None:
        memories = us.list_memories_contract(memory_type="tooling")
        memory = us.get_memory_by_id_contract("m1")
        relations = us.list_relations_contract(subject="Codex")
        llm_relations = us.list_relations_contract(status="accepted", relation="uses_tool")

        self.assertEqual(memories["count"], 1)
        self.assertEqual(memories["items"][0]["memory_id"], "m1")
        self.assertTrue(memory["found"])
        self.assertEqual(memory["item"]["subject"], "Codex")
        self.assertEqual(memory["evidence"][0]["target_id"], "e2")
        self.assertEqual(relations["count"], 1)
        self.assertEqual(relations["items"][0]["relation"], "prefers")
        self.assertEqual(llm_relations["count"], 1)
        self.assertEqual(llm_relations["items"][0]["status"], "accepted")
        self.assertEqual(llm_relations["items"][0]["edge_source"], "llm_judgment")

    def test_get_event_by_id_contract(self) -> None:
        data = us.get_event_by_id_contract("e2", fields="event_id,service,category_v2")

        self.assertTrue(data["found"])
        self.assertEqual(data["item"], {"event_id": "e2", "service": "Codex", "category_v2": "编程 / 工具"})

    def test_export_jsonl_and_csv_contracts(self) -> None:
        jsonl = us.export_events_contract(export_format="jsonl", fields="event_id,title", limit=2, order="asc")
        csv_data = us.export_events_contract(export_format="csv", fields="event_id,title", limit=2, order="asc")

        self.assertEqual(json.loads(jsonl["content"].splitlines()[0]), {"event_id": "e1", "title": "Alpha"})
        self.assertTrue(csv_data["content"].startswith("event_id,title\n"))
        self.assertIn("e1,Alpha\n", csv_data["content"])

    def test_data_quality_report_basic_shape(self) -> None:
        data = us.data_quality_report_contract()

        self.assertTrue(data["ok"])
        self.assertIn("tables", data)
        self.assertEqual(data["tables"]["unified_events"]["count"], 4)
        self.assertEqual(data["events"]["missing"]["event_id"], 0)
        self.assertEqual(data["memories"]["total"], 2)
        self.assertEqual(data["relations"]["dangling_relations"], 0)

    def test_rest_data_routes_are_top_level_contract_json(self) -> None:
        with LocalApiServer() as api:
            events_url = (
                f"http://127.0.0.1:{api.port}/data/events?"
                + urllib.parse.urlencode({
                    "limit": 1,
                    "fields": "event_id,source",
                    "start_time": "2026-01-01",
                    "end_time": "2026-01-31T23:59:59",
                })
            )
            event_url = f"http://127.0.0.1:{api.port}/data/event/e2?fields=event_id,service"
            aggregate_url = f"http://127.0.0.1:{api.port}/data/aggregate?group_by=service"
            relations_url = f"http://127.0.0.1:{api.port}/data/relations?status=accepted"
            quality_url = f"http://127.0.0.1:{api.port}/data/quality"

            events = _http_json(events_url)
            event = _http_json(event_url)
            aggregate = _http_json(aggregate_url)
            relations = _http_json(relations_url)
            quality = _http_json(quality_url)

        self.assertTrue(events["ok"])
        self.assertNotIn("data", events)
        self.assertEqual(events["count"], 1)
        self.assertTrue(event["found"])
        self.assertEqual(event["item"], {"event_id": "e2", "service": "Codex"})
        self.assertIn("items", aggregate)
        self.assertEqual(relations["items"][0]["edge_source"], "llm_judgment")
        self.assertEqual(relations["items"][0]["status"], "accepted")
        self.assertEqual(quality["tables"]["unified_events"]["count"], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
