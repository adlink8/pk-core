"""Phase 20-01: SQLite migration protocol sandbox."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from personal_knowledge.governance import apply_data_migration as m  # noqa: E402


def _make_db(path: Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t(v) VALUES (?)", [(f"r{i}",) for i in range(rows)])
    con.commit()
    con.close()


def test_sqlite_apply_and_fingerprint(tmp_path: Path) -> None:
    src = tmp_path / "src" / "a.sqlite"
    tgt = tmp_path / "dst" / "a.sqlite"
    _make_db(src, 5)
    pre = m.sqlite_logical_fingerprint(src)
    assert pre["integrity"] == "ok"
    assert pre["counts"]["t"] == 5

    op = {
        "id": "sqlite-1",
        "type": "sqlite",
        "source": "src/a.sqlite",
        "target": "dst/a.sqlite",
        "inverse": {"source": "dst/a.sqlite", "target": "src/a.sqlite"},
    }
    manifest = m.build_sandbox_manifest([op])
    man_path = tmp_path / "m.json"
    man_path.write_text(__import__("json").dumps(manifest), encoding="utf-8")
    journal = tmp_path / "j.jsonl"
    result = m.run(tmp_path, man_path, dry_run=False, apply=True, journal_path=journal)
    assert result["status"] == "applied"
    assert tgt.exists()
    post = m.sqlite_logical_fingerprint(tgt)
    assert post["logical_checksum"] == pre["logical_checksum"]


def test_sqlite_mid_failure_rolls_back(tmp_path: Path) -> None:
    src = tmp_path / "src" / "a.sqlite"
    _make_db(src, 2)
    pre = m.sqlite_logical_fingerprint(src)
    op = {
        "id": "sqlite-fail",
        "type": "sqlite",
        "source": "src/a.sqlite",
        "target": "dst/a.sqlite",
        "inverse": {"source": "dst/a.sqlite", "target": "src/a.sqlite"},
    }
    manifest = m.build_sandbox_manifest([op])
    man_path = tmp_path / "m.json"
    man_path.write_text(__import__("json").dumps(manifest), encoding="utf-8")
    journal = tmp_path / "j.jsonl"
    with pytest.raises(m.DataMigrationError):
        m.run(
            tmp_path,
            man_path,
            dry_run=False,
            apply=True,
            journal_path=journal,
            fail_after="staged",
        )
    # source still intact
    assert src.exists()
    assert m.sqlite_logical_fingerprint(src)["logical_checksum"] == pre["logical_checksum"]
