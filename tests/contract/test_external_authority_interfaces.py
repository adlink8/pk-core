from __future__ import annotations

import json
from pathlib import Path

from personal_knowledge.external_context.cli import main
from personal_knowledge.external_context.migrate import migrate


FORBIDDEN = {"body", "content", "raw", "raw_text", "full_text", "html", "secret", "token"}


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key).lower() for key in value} | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


def test_list_get_and_schema_status_share_metadata_only_contract(tmp_path: Path, capsys) -> None:
    db = tmp_path / "external.sqlite"
    migrate(db, write=True)
    before = db.read_bytes()
    for argv, operation in (
        (["--db", str(db), "list", "--json"], "sources.list"),
        (["--db", str(db), "get", "ext.python_releases", "--json"], "sources.get"),
        (["--db", str(db), "schema-status", "--json"], "schema.status"),
    ):
        assert main(argv) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema_version"] == "external_context_interface_v1"
        assert payload["operation"] == operation
        assert payload["privacy"] == {
            "metadata_only": True, "private_bodies": 0, "copyrighted_bodies": 0,
        }
        assert not (_keys(payload) & FORBIDDEN)
    assert db.read_bytes() == before


def test_get_missing_source_has_stable_error(tmp_path: Path, capsys) -> None:
    assert main(["--db", str(tmp_path / "missing.sqlite"), "get", "ext.missing", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "source_not_found"
    assert payload["privacy"]["private_bodies"] == 0


def test_schema_status_is_read_only_when_unapplied(tmp_path: Path, capsys) -> None:
    db = tmp_path / "never-created.sqlite"
    assert main(["--db", str(db), "schema-status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["schema_state"] == "unapplied"
    assert not db.exists()
