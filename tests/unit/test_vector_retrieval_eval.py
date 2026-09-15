"""Wave 10.2: 向量检索评测辅助逻辑单测。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNIFIED_SCRIPTS = ROOT / "integration" / "scripts"
if str(UNIFIED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(UNIFIED_SCRIPTS))

import personal_knowledge.retrieval.evaluate_vector_retrieval as mod  # noqa: E402


class TestVectorRetrievalEval(unittest.TestCase):
    def test_infer_collection_turn_result(self):
        result = {"session_id": "s1", "turn_id": "t1", "event_type": "conversation_turn"}
        self.assertEqual(mod.infer_collection(result), "conversation_turns")

    def test_infer_collection_personal_event(self):
        result = {"event_id": "e1", "source": "Google", "event_type": "search"}
        self.assertEqual(mod.infer_collection(result), "personal_events")

    def test_match_result_by_session(self):
        sample = {
            "expected_session_ids": ["sess-1"],
            "expected_source": "Agent",
            "preferred_collection": "conversation_turns",
        }
        result = {
            "session_id": "sess-1",
            "source": "Agent",
            "collection": "conversation_turns",
        }
        marks = mod.match_result(sample, result)
        self.assertTrue(marks["exact_match"])
        self.assertTrue(marks["session_match"])
        self.assertTrue(marks["source_match"])
        self.assertTrue(marks["collection_match"])

    def test_match_result_by_source_only(self):
        sample = {"expected_source": "Google", "preferred_collection": "personal_events"}
        result = {"source": "Google", "collection": "personal_events"}
        marks = mod.match_result(sample, result)
        self.assertFalse(marks["exact_match"])
        self.assertTrue(marks["source_match"])
        self.assertTrue(marks["collection_match"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
