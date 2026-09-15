"""Apps SDK backend data contract tests for memory widgets."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
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
import personal_knowledge.retrieval.unified_search as us  # noqa: E402


def _http_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_http(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError(f"service not ready: {url}")


class ApiServerProcess:
    def __init__(self) -> None:
        self.port = _find_free_port()
        self.proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> "ApiServerProcess":
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "personal_knowledge.services.api_server",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=os.environ.copy(),
        )
        _wait_for_http(f"http://127.0.0.1:{self.port}/health")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()


class AppsSdkDataContractTests(unittest.TestCase):
    maxDiff = None

    def test_memory_graph_contract_shape_and_bounds(self) -> None:
        data = us.get_memory_graph_contract(include_llm=True, limit=200)

        self.assertTrue(data["ok"])
        self.assertEqual(set(["ok", "scope", "counts", "nodes", "edges", "truncated"]).issubset(data), True)
        self.assertTrue(data["scope"]["include_llm"])
        self.assertLessEqual(len(data["nodes"]), 200)
        self.assertLessEqual(len(data["edges"]), 200)
        self.assertEqual(data["counts"]["returned_nodes"], len(data["nodes"]))
        self.assertEqual(data["counts"]["returned_edges"], len(data["edges"]))
        self.assertGreater(data["counts"]["rule_edges"], 0)
        self.assertTrue(all(node.get("id") and node.get("subject") for node in data["nodes"]))
        self.assertTrue(all(edge.get("edge_source") in {"rule", "llm_judgment"} for edge in data["edges"]))

        llm_edges = [edge for edge in data["edges"] if edge["edge_source"] == "llm_judgment"]
        for edge in llm_edges:
            self.assertIn(edge.get("gate_status"), {"review", "accepted"})
            self.assertIsInstance(edge.get("confidence"), float)
            self.assertTrue(edge.get("candidate_id"))
            self.assertIn("reason", edge)

    def test_subject_scoped_memory_graph_contract(self) -> None:
        data = us.get_memory_graph_contract(subject="Codex", hops=1, include_llm=False, limit=20)

        self.assertTrue(data["ok"])
        self.assertTrue(data["scope"]["found"])
        self.assertEqual(data["scope"]["subject"], "Codex")
        self.assertEqual(data["scope"]["hops"], 1)
        self.assertLessEqual(len(data["nodes"]), 20)
        self.assertTrue(any("Codex" in node["subject"] for node in data["nodes"]))
        self.assertTrue(all(edge["edge_source"] == "rule" for edge in data["edges"]))

    def test_subject_scoped_memory_graph_allows_zero_hops(self) -> None:
        data = us.get_memory_graph_contract(subject="Codex", hops=0, include_llm=True, limit=20)

        self.assertTrue(data["ok"])
        self.assertTrue(data["scope"]["found"])
        self.assertEqual(data["scope"]["hops"], 0)
        self.assertEqual(data["counts"]["total_nodes"], 1)
        self.assertEqual(len(data["nodes"]), 1)
        self.assertEqual(data["edges"], [])

    def test_relation_review_contract_shape_and_status_filter(self) -> None:
        data = us.get_memory_relation_review_contract(status="review", limit=10)

        self.assertTrue(data["ok"])
        self.assertLessEqual(data["count"], 10)
        self.assertEqual(data["count"], len(data["items"]))
        self.assertTrue(all(item["gate_status"] == "review" for item in data["items"]))
        for item in data["items"]:
            self.assertTrue(item["candidate_id"])
            self.assertTrue(item["source_memory_id"])
            self.assertTrue(item["target_memory_id"])
            self.assertIn("source_memory", item)
            self.assertIn("target_memory", item)
            self.assertIn("candidate_reason", item)
            self.assertIsInstance(item["evidence_refs"], list)
            self.assertIsInstance(item["risk_flags"], list)

    def test_rest_apps_sdk_contracts_are_top_level_json(self) -> None:
        with ApiServerProcess() as api:
            graph_url = (
                f"http://127.0.0.1:{api.port}/memory/graph?"
                + urllib.parse.urlencode({"include_llm": 1, "limit": 200})
            )
            review_url = (
                f"http://127.0.0.1:{api.port}/memory/relation-review?"
                + urllib.parse.urlencode({"status": "review", "limit": 10})
            )
            graph = _http_json(graph_url)
            review = _http_json(review_url)

        self.assertTrue(graph["ok"])
        self.assertNotIn("data", graph)
        self.assertIn("nodes", graph)
        self.assertIn("edges", graph)
        self.assertTrue(review["ok"])
        self.assertNotIn("data", review)
        self.assertIn("items", review)
        self.assertTrue(all(item["gate_status"] == "review" for item in review["items"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
