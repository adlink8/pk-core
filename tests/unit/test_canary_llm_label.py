"""Unit tests for canary LLM label parsing (no network)."""

from __future__ import annotations

from personal_knowledge.evaluation.knowledge.evaluate_knowledge_canary import (
    _parse_label_json,
)


def test_parse_label_json_object():
    assert _parse_label_json('{"label":"helpful","reason":"ok"}') == "helpful"
    assert _parse_label_json('```json\n{"label": "missing"}\n```') == "missing"


def test_parse_label_json_invalid():
    assert _parse_label_json("") == ""
    assert _parse_label_json('{"label":"maybe"}') == ""
