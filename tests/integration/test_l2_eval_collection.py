"""Phase 17 L2-only evaluation collection safety contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_knowledge.evaluation import build_l2_eval_collection as builder


class _FakeCollection:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})

    def count(self):
        return len(self.rows)

    def get(self, ids=None, limit=None, offset=0, include=None, timeout=60):
        keys = list(ids) if ids is not None else list(self.rows)[offset : offset + limit]
        keys = [key for key in keys if key in self.rows]
        result = {"ids": keys}
        if "embeddings" in (include or []):
            result["embeddings"] = [self.rows[key][0] for key in keys]
        if "documents" in (include or []):
            result["documents"] = [self.rows[key][1] for key in keys]
        if "metadatas" in (include or []):
            result["metadatas"] = [self.rows[key][2] for key in keys]
        return result

    def upsert(self, ids, embeddings, documents, metadatas, timeout=300):
        for idx, unit_id in enumerate(ids):
            self.rows[unit_id] = (embeddings[idx], documents[idx], metadatas[idx])


class _FakeClient:
    def __init__(self, source_rows):
        self.collections = {"active": _FakeCollection(source_rows)}

    def list_collections(self):
        return [{"name": name} for name in self.collections]

    def get_or_create_collection(self, name, metadata=None):
        return self.collections.setdefault(name, _FakeCollection())


def _rows(ids):
    return {unit_id: ([1.0, 0.0], f"doc-{unit_id}", {"unit": unit_id}) for unit_id in ids}


def test_l2_builder_is_dry_by_default_and_idempotent(monkeypatch, tmp_path: Path) -> None:
    ids = {"cu1", "cu2"}
    client = _FakeClient(_rows(ids))
    monkeypatch.setattr(builder, "_read_active", lambda: "active")
    monkeypatch.setattr(builder, "load_l2_unit_ids", lambda *_: ids)

    dry = builder.build_l2_eval_collection(client=client, report_dir=tmp_path)
    assert dry.written == 0
    assert dry.target_collection not in client.collections

    first = builder.build_l2_eval_collection(
        client=client, write=True, report_dir=tmp_path
    )
    assert first.gate_passed
    assert first.written == 2
    assert set(client.collections[first.target_collection].rows) == ids

    second = builder.build_l2_eval_collection(
        client=client, write=True, report_dir=tmp_path
    )
    assert second.gate_passed
    assert second.written == 0


def test_l2_builder_fails_closed_on_orphan(monkeypatch, tmp_path: Path) -> None:
    ids = {"cu1"}
    client = _FakeClient(_rows(ids))
    monkeypatch.setattr(builder, "_read_active", lambda: "active")
    monkeypatch.setattr(builder, "load_l2_unit_ids", lambda *_: ids)
    target = builder.target_name("active", ids)
    client.collections[target] = _FakeCollection(_rows({"unexpected"}))

    with pytest.raises(RuntimeError, match="unexpected IDs"):
        builder.build_l2_eval_collection(
            client=client, write=True, report_dir=tmp_path
        )
