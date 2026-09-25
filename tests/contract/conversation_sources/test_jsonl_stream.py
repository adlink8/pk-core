"""JSONL 流解析原语契约（测试对象 = ``jsonl_stream.py``）。

共享低层原语，不属于某个家族适配器：默认 strict 必须整行炸出（fail-closed），
容错模式必须跳过坏行且留下可数的证据（行号列表），限额仍然 fail-closed。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_knowledge.adapters.conversation_sources.jsonl_stream import (
    JSONLLineError,
    JSONLLimitExceeded,
    iter_jsonl_lines,
    iter_jsonl_lines_counted,
)


def _write_jsonl(path: Path, rows: list) -> Path:
    lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_strict_default_still_raises_on_bad_line(tmp_path):
    """默认行为不变：一行坏 JSON 抛 JSONLLineError，整文件不产出。"""
    path = _write_jsonl(tmp_path / "stream.jsonl", [
        {"n": 1},
        "{not json",
        {"n": 2},
    ])
    with pytest.raises(JSONLLineError):
        list(iter_jsonl_lines(path))


def test_strict_false_skips_silently(tmp_path):
    """既有 ``strict=False`` 语义不变：跳过坏行、无计数义务。"""
    path = _write_jsonl(tmp_path / "stream.jsonl", [
        {"n": 1},
        "{not json",
        {"n": 2},
    ])
    assert list(iter_jsonl_lines(path, strict=False)) == [{"n": 1}, {"n": 2}]


def test_counted_mode_skips_and_counts_bad_lines(tmp_path):
    """计数容错模式：坏行被跳过，行号落进 ``errors``（1 基）。"""
    path = _write_jsonl(tmp_path / "stream.jsonl", [
        {"n": 1},
        "{not json",
        {"n": 2},
        "]]]",
    ])
    errors: list[int] = []
    records = list(iter_jsonl_lines_counted(path, errors=errors))
    assert records == [{"n": 1}, {"n": 2}]
    assert errors == [2, 4]


def test_counted_mode_reports_nothing_when_clean(tmp_path):
    path = _write_jsonl(tmp_path / "stream.jsonl", [{"n": 1}, {"n": 2}])
    errors: list[int] = []
    assert list(iter_jsonl_lines_counted(path, errors=errors)) == [
        {"n": 1}, {"n": 2},
    ]
    assert errors == []


def test_counted_mode_still_fails_closed_on_limits(tmp_path):
    """容错只针对坏行：``max_entries`` 限额照旧 fail-closed。"""
    path = _write_jsonl(tmp_path / "stream.jsonl", [
        {"n": 1}, {"n": 2}, {"n": 3},
    ])
    with pytest.raises(JSONLLimitExceeded):
        list(iter_jsonl_lines_counted(path, max_entries=2))

    big = tmp_path / "big.jsonl"
    big.write_text("{}\n" * 10, encoding="utf-8")
    with pytest.raises(JSONLLimitExceeded):
        list(iter_jsonl_lines_counted(big, max_bytes=8))


def test_counted_mode_reports_line_numbers_not_content(tmp_path):
    """计数只落行号，不落行内容：载荷文本不会泄漏进任何警告。"""
    canary = "sensitive-payload-token"
    path = _write_jsonl(tmp_path / "stream.jsonl", [
        json.dumps({"secret": canary})[:-1] + "broken",
    ])
    errors: list[int] = []
    assert list(iter_jsonl_lines_counted(path, errors=errors)) == []
    assert errors == [1]
