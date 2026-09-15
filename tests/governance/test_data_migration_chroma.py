"""Phase 20-01: Chroma active pointer atomic switch sandbox."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from personal_knowledge.governance import apply_data_migration as m  # noqa: E402


def test_chroma_pointer_switch_and_identity(tmp_path: Path) -> None:
    pointer = tmp_path / "var" / "db" / "knowledge_index_active.txt"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("knowledge_units_old\n", encoding="utf-8")
    op = {
        "id": "chroma-ptr",
        "type": "chroma_pointer",
        "source": "var/db/knowledge_index_active.txt",
        "target": "var/db/knowledge_index_active.txt",  # same path; value changes
        "target_value": "knowledge_units_new\n",
        "backup": "var/db/knowledge_index_active.txt.bak-phase20",
        "inverse": {
            "source": "var/db/knowledge_index_active.txt",
            "target": "var/db/knowledge_index_active.txt",
        },
    }
    man = m.build_sandbox_manifest([op])
    path = tmp_path / "m.json"
    path.write_text(__import__("json").dumps(man), encoding="utf-8")
    result = m.run(tmp_path, path, dry_run=False, apply=True, journal_path=tmp_path / "j.jsonl")
    assert result["status"] == "applied"
    assert pointer.read_text(encoding="utf-8").strip() == "knowledge_units_new"
