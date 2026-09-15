"""Phase 20-01: ordinary file/directory stage-copy cutover sandbox."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from personal_knowledge.governance import apply_data_migration as m  # noqa: E402


def test_files_directory_cutover(tmp_path: Path) -> None:
    src = tmp_path / "imports" / "batch"
    src.mkdir(parents=True)
    (src / "a.txt").write_text("hello", encoding="utf-8")
    (src / "b.txt").write_text("world", encoding="utf-8")
    op = {
        "id": "dir-1",
        "type": "directory",
        "source": "imports/batch",
        "target": "data/imports/batch",
        "inverse": {"source": "data/imports/batch", "target": "imports/batch"},
    }
    man = m.build_sandbox_manifest([op])
    path = tmp_path / "m.json"
    path.write_text(__import__("json").dumps(man), encoding="utf-8")
    result = m.run(tmp_path, path, dry_run=False, apply=True, journal_path=tmp_path / "j.jsonl")
    assert result["status"] == "applied"
    tgt = tmp_path / "data" / "imports" / "batch"
    assert (tgt / "a.txt").read_text(encoding="utf-8") == "hello"
    assert not src.exists()


def test_files_failure_after_stage_no_cutover(tmp_path: Path) -> None:
    src = tmp_path / "logs" / "a.log"
    src.parent.mkdir(parents=True)
    src.write_text("logline", encoding="utf-8")
    op = {
        "id": "file-fail",
        "type": "files",
        "source": "logs/a.log",
        "target": "var/logs/a.log",
        "inverse": {"source": "var/logs/a.log", "target": "logs/a.log"},
    }
    man = m.build_sandbox_manifest([op])
    path = tmp_path / "m.json"
    path.write_text(__import__("json").dumps(man), encoding="utf-8")
    with pytest.raises(m.DataMigrationError):
        m.run(
            tmp_path,
            path,
            dry_run=False,
            apply=True,
            journal_path=tmp_path / "j.jsonl",
            fail_after="staged",
        )
    assert src.exists()
    assert src.read_text(encoding="utf-8") == "logline"


def test_unapproved_manifest_rejected_on_apply(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "scope": "phase20-data-cohort",
        "cohort": "x",
        "approved": False,
        "operations": [],
    }
    payload["manifest_sha256"] = m._manifest_checksum(payload)
    path = tmp_path / "m.json"
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    # dry-run allowed; apply requires approved=true
    dry = m.run(tmp_path, path, dry_run=True, apply=False)
    assert dry["status"] == "dry-run-pass"
    with pytest.raises(m.DataMigrationError, match="not approved"):
        m.run(tmp_path, path, dry_run=False, apply=True)
