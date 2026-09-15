"""P1: build_vector_store / search_vectors 纯逻辑 smoke。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "integration" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import personal_knowledge.retrieval.build_vector_store as bvs  # noqa: E402
import personal_knowledge.retrieval.search_vectors as sv  # noqa: E402


def test_filter_vectorizable_prefers_content_rich() -> None:
    rows = [
        {
            "event_id": "e1",
            "title": "主题",
            "content": "short",
            "content_rich": "这是足够长的 content_rich 文本用于向量化",
        },
        {
            "event_id": "e2",
            "title": "",
            "content": "x",
            "content_rich": "",
        },
        {
            "event_id": "e3",
            "title": "Google",
            "content": "足够长的原始 content 回退路径文本内容",
            "content_rich": None,
        },
    ]
    out, skipped = bvs.filter_vectorizable(rows)
    ids = {r["event_id"] for r in out}
    assert "e1" in ids
    assert "e3" in ids
    assert "e2" not in ids
    assert skipped >= 1
    e1 = next(r for r in out if r["event_id"] == "e1")
    assert "_text" in e1
    assert "主题" in e1["_text"]
    assert "content_rich" in e1["_text"] or "向量化" in e1["_text"]


def test_progress_roundtrip(tmp_path: Path, monkeypatch) -> None:
    progress = tmp_path / "progress.json"
    monkeypatch.setattr(bvs, "PROGRESS_FILE", progress)
    assert bvs.load_progress() == set()
    bvs.save_progress({"a", "b", "c"})
    assert bvs.load_progress() == {"a", "b", "c"}
    # corrupt file
    progress.write_text("{not-json", encoding="utf-8")
    assert bvs.load_progress() == set()


def test_normalize_similarity_bounds() -> None:
    assert sv._normalize_similarity(0.0) == 1.0
    assert 0.0 <= sv._normalize_similarity(2.0) <= 1.0
    assert sv._normalize_similarity(100.0) == 0.0


def test_search_empty_query_returns_empty() -> None:
    assert sv.search("") == []
    assert sv.search("   ") == []
