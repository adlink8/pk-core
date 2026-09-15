"""Private abstention dev-set builder contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from personal_knowledge.evaluation.build_abstention_dev import build_rows
from personal_knowledge.evaluation.calibrate_abstention import select_threshold
from personal_knowledge.evaluation.knowledge_eval_metrics import RankedHit, score_case


def test_build_rows_joins_labels_without_publishing_queries(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    database = tmp_path / "knowledge.sqlite"
    report.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "query_hash": "abc123",
                        "label": "helpful",
                        "returned_ids": ["cu1"],
                    },
                    {
                        "query_hash": "skip",
                        "label": "missing",
                        "returned_ids": ["cu2"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    query = "private positive"
    query_hash = hashlib.sha256(query.encode()).hexdigest()[:32]
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["results"][0]["query_hash"] = query_hash
    report.write_text(json.dumps(payload), encoding="utf-8")
    con = sqlite3.connect(database)
    con.execute("CREATE TABLE canonical_knowledge_units(question TEXT,status TEXT)")
    con.execute("INSERT INTO canonical_knowledge_units VALUES (?, 'current')", (query,))
    con.commit()
    con.close()
    rows = build_rows(report, database)
    assert len(rows) == 2
    positive = next(row for row in rows if not row["expected_abstain"])
    negative = next(row for row in rows if row["expected_abstain"])
    assert positive["gold_unit_ids"] == ["cu1"]
    assert "DEV-NO-EVIDENCE-" in negative["query"]
    assert negative["gold_provenance"] == "derived_absent_nonce_v1"


def test_select_threshold_uses_development_scores_only() -> None:
    scores = []
    for index, value in enumerate([0.9, 0.85, 0.8, 0.75, 0.7]):
        scores.append(
            score_case(
                f"p{index}", "l1", [RankedHit(id="x", score=value)]
            )
        )
    for index, value in enumerate([0.6, 0.55, 0.5, 0.45, 0.4]):
        scores.append(
            score_case(
                f"n{index}",
                "l1",
                [RankedHit(id="x", score=value)],
                expected_abstain=True,
            )
        )
    result = select_threshold(scores)
    assert result.passed
    assert result.negative_fp_rate <= 0.10
    assert result.positive_retention >= 0.80
