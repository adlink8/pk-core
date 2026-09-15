"""Phase 08 Wave 5 decomplexity plan structure tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
# Plan-only fixture (Phase 20 moved live analysis under archive quarantine).
_FIXTURES = ROOT / "tests" / "fixtures" / "memory_decomplexity"
PLAN_JSON = _FIXTURES / "memory_decomplexity_plan.json"
PLAN_MD = _FIXTURES / "memory_decomplexity_plan.md"


class TestMemoryDecomplexityPlan(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.plan = json.loads(PLAN_JSON.read_text(encoding="utf-8"))

    def test_plan_is_plan_only_and_protects_active_surfaces(self) -> None:
        guardrail = self.plan["no_action_guardrail"]
        self.assertFalse(guardrail["deletes_executed"])
        self.assertFalse(guardrail["renames_executed"])
        self.assertFalse(guardrail["entrypoints_disabled"])
        self.assertFalse(guardrail["databases_modified"])

        protected = {item["surface"] for item in self.plan["protected_surfaces"]}
        self.assertIn("integration/scripts/run_pipeline.py", protected)
        self.assertIn("integration/scripts/unified_search.py", protected)
        self.assertIn("memory_items/memory_links/memory_relations", protected)

    def test_counts_match_classification_rows(self) -> None:
        for section_name in ("scripts", "artifacts"):
            rows = self.plan[f"{section_name[:-1]}_classification"]
            counts = {key: 0 for key in ("keep", "keep_but_rename", "deprecated", "archive_only", "remove_candidate")}
            for row in rows:
                counts[row["category"]] += 1
            expected = self.plan["counts"][section_name]
            for key, value in counts.items():
                self.assertEqual(value, expected[key], f"{section_name}:{key}")
            self.assertEqual(sum(counts.values()), expected["total"])

    def test_deprecated_archive_and_remove_candidates_have_required_fields(self) -> None:
        required = {
            "current_owner",
            "current_readers",
            "replacement_path",
            "why_safe_or_not_safe",
            "risk",
            "required_pre_delete_checks",
            "reversible_path",
        }
        for candidate in self.plan["candidate_details"]:
            self.assertLessEqual(required, set(candidate), candidate["name"])
            self.assertTrue(candidate["current_owner"], candidate["name"])
            self.assertTrue(candidate["replacement_path"], candidate["name"])
            self.assertTrue(candidate["why_safe_or_not_safe"], candidate["name"])
            self.assertTrue(candidate["required_pre_delete_checks"], candidate["name"])
            self.assertIn(candidate["category"], {"deprecated", "archive_only", "remove_candidate"})

    def test_active_reader_objects_are_not_direct_remove_candidates(self) -> None:
        for candidate in self.plan["candidate_details"]:
            if candidate["current_readers"]:
                self.assertNotEqual(candidate["category"], "remove_candidate", candidate["name"])

    def test_plan_states_mechanism_consolidation_not_result_winner(self) -> None:
        md = PLAN_MD.read_text(encoding="utf-8")
        self.assertIn("不是因为某条旧记忆结果输给某条新图谱结果", md)
        self.assertIn("Any object with an active reader is never marked as direct remove", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
