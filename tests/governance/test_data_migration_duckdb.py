"""Phase 20-01: DuckDB file-level migration sandbox."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from personal_knowledge.governance import apply_data_migration as m  # noqa: E402


def test_duckdb_file_copy_cutover(tmp_path: Path) -> None:
    src = tmp_path / "src" / "g.duckdb"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"DUCKDB-FAKE-HEADER" + b"\x00" * 64)
    pre = m.duckdb_fingerprint(src)
    op = {
        "id": "duck-1",
        "type": "duckdb",
        "source": "src/g.duckdb",
        "target": "dst/g.duckdb",
        "inverse": {"source": "dst/g.duckdb", "target": "src/g.duckdb"},
    }
    man = m.build_sandbox_manifest([op])
    path = tmp_path / "m.json"
    path.write_text(__import__("json").dumps(man), encoding="utf-8")
    result = m.run(tmp_path, path, dry_run=False, apply=True, journal_path=tmp_path / "j.jsonl")
    assert result["status"] == "applied"
    tgt = tmp_path / "dst" / "g.duckdb"
    assert tgt.exists()
    assert m.duckdb_fingerprint(tgt)["sha256"] == pre["sha256"]
