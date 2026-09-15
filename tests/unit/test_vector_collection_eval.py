"""Wave 10.1: 向量 collection 健康收口逻辑单测。"""

from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNIFIED_SCRIPTS = ROOT / "integration" / "scripts"
if str(UNIFIED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(UNIFIED_SCRIPTS))

import personal_knowledge.retrieval.evaluate_vector_collections as mod  # noqa: E402


def make_base_report() -> dict:
    return {
        "summary": {"available": True, "session_count": 164, "turn_count": 2046},
        "sqlite": {"conversation_turns_summary": 2046},
        "expected": {
            "conversation_turns": {"expected_count": 2046},
            "personal_events": {"expected_count": 7723},
        },
        "chroma": {
            "available": True,
            "heartbeat_ns": 123,
            "collections": {
                "personal_events": {"exists": True, "count_match": True},
                "conversation_turns": {"exists": True, "count_match": True},
            },
            "errors": [],
        },
        "actions": [],
    }


class TestVectorCollectionEval(unittest.TestCase):
    def test_finalize_report_marks_healthy_when_all_counts_match(self):
        report = mod.finalize_report(deepcopy(make_base_report()))
        self.assertEqual(report["status"], "healthy")
        self.assertFalse(report["blocked"])
        self.assertEqual(report["issues"], [])
        self.assertIn("live 检查通过", report["actions"][0])

    def test_finalize_report_marks_blocked_when_chroma_unavailable(self):
        report = make_base_report()
        report["chroma"]["available"] = False
        report["chroma"]["errors"] = ["ChromaError: connection refused"]
        finalized = mod.finalize_report(deepcopy(report))
        self.assertEqual(finalized["status"], "blocked")
        self.assertTrue(finalized["blocked"])
        self.assertIn("chroma_unavailable", finalized["issues"])
        self.assertTrue(any("blocked" in action for action in finalized["actions"]))
        self.assertFalse(any("无需回灌" in action for action in finalized["actions"]))

    def test_finalize_report_marks_unhealthy_for_count_mismatch(self):
        report = make_base_report()
        report["chroma"]["collections"]["conversation_turns"]["count_match"] = False
        finalized = mod.finalize_report(deepcopy(report))
        self.assertEqual(finalized["status"], "unhealthy")
        self.assertIn("count_mismatch:conversation_turns", finalized["issues"])
        self.assertTrue(any("personal_knowledge.application.conversation.build_conversation_vector_store --write" in action for action in finalized["actions"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
