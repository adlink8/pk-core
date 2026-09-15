"""Unit tests for canary critical-row triage (list-critical parsing)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_knowledge.evaluation.knowledge.evaluate_knowledge_canary import (
    CRITICAL_CANARY_LABELS,
    list_critical_canary_rows,
    main as canary_main,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "canary_report_critical_sample.json"
)


def test_fixture_exists():
    assert FIXTURE.is_file()


def test_list_critical_canary_rows_from_path():
    rows = list_critical_canary_rows(FIXTURE)
    assert len(rows) == 2
    assert {r["label"] for r in rows} == {"wrong", "stale"}
    assert all(r["label"] in CRITICAL_CANARY_LABELS for r in rows)

    by_label = {r["label"]: r for r in rows}
    wrong = by_label["wrong"]
    assert wrong["index"] == 1
    assert wrong["query_hash"] == "bbb222wrong000000000000000000000"
    assert wrong["returned_ids"] == ["u_wrong_1", "u_wrong_2", "u_wrong_3"]
    assert wrong["scores"] == [0.77, 0.65, 0.50]
    assert len(wrong["returned_ids"]) <= 3
    assert len(wrong["scores"]) <= 3

    stale = by_label["stale"]
    assert stale["index"] == 3
    assert stale["query_hash"] == "ddd444stale000000000000000000000"
    assert stale["returned_ids"] == ["u_stale_1", "u_stale_2"]
    assert stale["scores"] == [0.85, 0.60]


def test_list_critical_canary_rows_from_dict():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = list_critical_canary_rows(data)
    assert [r["index"] for r in rows] == [1, 3]


def test_list_critical_ignores_helpful_missing_empty():
    report = {
        "results": [
            {"query_hash": "h", "label": "helpful", "returned_ids": ["a"], "scores": [1.0]},
            {"query_hash": "m", "label": "missing", "returned_ids": ["b"], "scores": [0.1]},
            {"query_hash": "e", "label": "", "returned_ids": ["c"], "scores": [0.2]},
            {"query_hash": "w", "label": "wrong", "returned_ids": ["d1", "d2"], "scores": [0.9, 0.8]},
        ]
    }
    rows = list_critical_canary_rows(report)
    assert len(rows) == 1
    assert rows[0]["index"] == 3
    assert rows[0]["label"] == "wrong"


def test_list_critical_cli_exit_and_shape(capsys: pytest.CaptureFixture[str]):
    rc = canary_main(["--report", str(FIXTURE), "--list-critical"])
    assert rc == 1  # critical rows present → non-zero ops signal
    out = json.loads(capsys.readouterr().out)
    assert out["critical_count"] == 2
    assert set(out["critical_labels"]) == {"wrong", "stale"}
    assert len(out["rows"]) == 2
    for row in out["rows"]:
        assert set(row.keys()) >= {
            "index",
            "query_hash",
            "label",
            "returned_ids",
            "scores",
        }


def test_list_critical_cli_clean_exit(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    clean = {
        "results": [
            {
                "query_hash": "ok",
                "label": "helpful",
                "returned_ids": ["u1"],
                "scores": [0.9],
            }
        ]
    }
    path = tmp_path / "clean.json"
    path.write_text(json.dumps(clean), encoding="utf-8")
    rc = canary_main(["--report", str(path), "--list-critical"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["critical_count"] == 0
    assert out["rows"] == []
