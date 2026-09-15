"""pk-ku product CLI surface — thin wrapper, policy on flags not code edits."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.application.ku import build_parser, main


def test_parser_subcommands_exist():
    p = build_parser()
    # required=True subparsers: parse known commands without crashing
    for cmd in (
        "inspect", "prepare", "extract", "status", "extract-gate",
        "canonical", "publish", "vector", "canary", "promote", "watermark",
        "reconcile", "history", "promote-units", "doctor",
        "workflow",
    ):
        # --help exits SystemExit 0
        with pytest.raises(SystemExit) as ei:
            p.parse_args([cmd, "--help"])
        assert ei.value.code == 0


def test_workflow_prints_and_exits_0(capsys):
    code = main(["workflow"])
    assert code == 0
    out = capsys.readouterr().out
    assert "pk-ku inspect" in out
    assert "Forbidden" in out or "forbidden" in out.lower()
    assert "full inventory" in out.lower() or "build_knowledge_inventory" in out


def test_view_consume_idle_does_not_create_database(tmp_path, capsys):
    import json

    db = tmp_path / "absent.sqlite"
    assert main(["view-consume", "--conversation-db", str(db)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "idle"
    assert not db.exists()


def test_prepare_requires_model():
    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["prepare"])  # missing --model


def test_extract_defaults_to_gemini_35_flash_lite():
    args = build_parser().parse_args(["extract", "--run", "ir_test"])
    assert args.model == "gemini-3.5-flash-lite"


def test_inspect_uses_committed_watermark_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db = tmp_path / "personal_system.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE knowledge_source_watermark "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    con.execute(
        "INSERT INTO knowledge_source_watermark VALUES ('committed', 'wm-safe', 'now')"
    )
    con.commit()
    con.close()

    captured: list[str] = []

    def _capture(argv: list[str] | None = None) -> int:
        captured.extend(argv or [])
        return 0

    monkeypatch.setattr(
        "personal_knowledge.application.knowledge.refresh_knowledge_units.main",
        _capture,
    )
    assert main(["inspect", "--db", str(db)]) == 0
    assert captured[:3] == ["--inspect", "--source-checksum", "wm-safe"]


def test_extract_rejects_non_incremental_run_id(capsys):
    code = main(["extract", "--run", "6f3da1eec10c4fee6fb1509c83cfb85b", "--max-items", "1"])
    assert code == 2
    err = capsys.readouterr().err
    assert "ir_" in err or "incremental" in err.lower()


def test_promote_without_args_exits_2(capsys):
    code = main(["promote"])
    assert code == 2


def test_promote_without_eval_refuses_by_default(capsys):
    """P0 fail-closed: promote with collection but no eval artifacts → non-zero.

    Does not touch active pointer — refuse happens before promote().
    """
    code = main(["promote", "--collection", "knowledge_units_test_no_eval_gate"])
    captured = capsys.readouterr()
    assert code != 0
    combined = (captured.err + captured.out).lower()
    assert "refused" in combined or "eval" in combined
    assert "promoted:" not in combined


def test_promote_parser_defaults_require_eval():
    p = build_parser()
    args = p.parse_args(["promote", "--collection", "c1"])
    assert args.require_eval_pass is True
    assert args.allow_without_eval is False


def test_promote_parser_allow_without_eval():
    p = build_parser()
    args = p.parse_args(["promote", "--collection", "c1", "--allow-without-eval"])
    assert args.allow_without_eval is True
    args2 = p.parse_args(["promote", "--collection", "c1", "--no-require-eval-pass"])
    assert args2.require_eval_pass is False


def test_prod_cli_start_soft_banned_without_env(capsys, monkeypatch):
    """CLI --start refuses unless PK_KU_ALLOW_FULL_INVENTORY_START=1 (API start_run OK)."""
    monkeypatch.delenv("PK_KU_ALLOW_FULL_INVENTORY_START", raising=False)
    from personal_knowledge.application.knowledge.build_knowledge_units_prod import (
        main as prod_main,
    )

    code = prod_main(
        ["--start", "--inventory", "inv_test_soft_ban", "--limit", "1"]
    )
    captured = capsys.readouterr()
    assert code == 2
    err = captured.err.lower()
    assert "soft-banned" in err or "pk-ku prepare" in err or "incremental" in err


def test_watermark_show_json(capsys):
    code = main(["watermark"])
    assert code == 0
    out = capsys.readouterr().out
    assert "committed" in out
    assert "current_source_checksum" in out


def test_watermark_advance_requires_source(capsys):
    code = main(["watermark", "--advance"])
    assert code == 2
    err = capsys.readouterr().err
    assert "--from-canonical" in err or "--checksum" in err


def test_watermark_advance_dry_run_from_canonical(capsys):
    code = main(["watermark", "--advance", "--from-canonical"])
    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out or '"write": false' in out.lower() or '"write": false' in out


_RUN_ITEMS_SCHEMA = (
    "CREATE TABLE knowledge_run_items ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, "
    "inventory_id TEXT NOT NULL, position INTEGER NOT NULL, "
    "evidence_ref TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
    "attempt_count INTEGER NOT NULL DEFAULT 0, lease_started_at TEXT, "
    "last_error_class TEXT, cache_key TEXT, response_hash TEXT, "
    "unit_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT, "
    "UNIQUE(run_id, position))"
)


def _add_run_item(
    db: Path, run_id: str, position: int, evidence_ref: str,
    status: str, error_class: str | None = None,
) -> None:
    con = sqlite3.connect(db)
    con.executescript(_RUN_ITEMS_SCHEMA.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))
    con.execute(
        "INSERT INTO knowledge_run_items "
        "(run_id, inventory_id, position, evidence_ref, status, last_error_class) "
        "VALUES (?,?,?,?,?,?)",
        (run_id, "inv_test", position, evidence_ref, status, error_class),
    )
    con.commit()
    con.close()


def _watermark_value(db: Path) -> str:
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT value FROM knowledge_source_watermark WHERE key='committed'"
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    con.close()
    return row[0] if row else ""


def test_watermark_advance_blocked_by_pending(tmp_path: Path, capsys):
    db = tmp_path / "ku.db"
    _add_run_item(db, "ir_a", 1, "sess:1", "pending")
    code = main(["watermark", "--advance", "--checksum", "cs1", "--db", str(db), "--write"])
    assert code == 2
    err = capsys.readouterr().err
    assert "ir_a" in err and "unfinished" in err
    assert _watermark_value(db) == ""


def test_watermark_advance_terminal_failed_requires_acknowledge(tmp_path: Path, capsys):
    db = tmp_path / "ku.db"
    _add_run_item(db, "ir_a", 1, "sess:1", "terminal_failed", "llm_timeout")
    code = main(["watermark", "--advance", "--checksum", "cs1", "--db", str(db), "--write"])
    assert code == 2
    err = capsys.readouterr().err
    assert "terminal_failed" in err and "--acknowledge-failures" in err
    assert _watermark_value(db) == ""

    code = main([
        "watermark", "--advance", "--checksum", "cs1", "--db", str(db),
        "--write", "--acknowledge-failures",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert '"dead_refs_recorded": 1' in out
    assert _watermark_value(db) == "cs1"
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT evidence_ref, run_id, error_class FROM knowledge_dead_refs"
    ).fetchall()
    con.close()
    assert rows == [("sess:1", "ir_a", "llm_timeout")]


def test_watermark_advance_not_blocked_by_acknowledged_dead_ref(tmp_path: Path, capsys):
    db = tmp_path / "ku.db"
    _add_run_item(db, "ir_a", 1, "sess:1", "terminal_failed", "llm_timeout")
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE knowledge_dead_refs (evidence_ref TEXT, run_id TEXT, "
        "error_class TEXT, acknowledged_at TEXT, PRIMARY KEY (evidence_ref, run_id))"
    )
    con.execute(
        "INSERT INTO knowledge_dead_refs VALUES ('sess:1', 'ir_a', 'llm_timeout', 'now')"
    )
    con.commit()
    con.close()
    code = main(["watermark", "--advance", "--checksum", "cs1", "--db", str(db), "--write"])
    assert code == 0
    capsys.readouterr()
    assert _watermark_value(db) == "cs1"


def test_watermark_advance_dry_run_reports_preconditions(tmp_path: Path, capsys):
    db = tmp_path / "ku.db"
    _add_run_item(db, "ir_a", 1, "sess:1", "pending")
    code = main(["watermark", "--advance", "--checksum", "cs1", "--db", str(db)])
    assert code == 0
    out = capsys.readouterr().out
    assert '"preconditions"' in out and '"ok": false' in out
    assert _watermark_value(db) == ""


def test_watermark_advance_ok_when_all_done(tmp_path: Path, capsys):
    db = tmp_path / "ku.db"
    _add_run_item(db, "ir_a", 1, "sess:1", "succeeded")
    _add_run_item(db, "ir_a", 2, "sess:2", "abstained")
    code = main(["watermark", "--advance", "--checksum", "cs1", "--db", str(db), "--write"])
    assert code == 0
    out = capsys.readouterr().out
    assert '"ok": true' in out and '"changed": true' in out
    assert _watermark_value(db) == "cs1"


def test_reconcile_write_requires_i_know(capsys):
    code = main(["reconcile", "--write"])
    assert code == 2
    err = capsys.readouterr().err
    assert "i-know" in err.lower()


def test_reconcile_parser_defaults():
    p = build_parser()
    args = p.parse_args(["reconcile", "--max-subjects", "5"])
    assert args.command == "reconcile"
    assert args.write is False
    assert args.max_subjects == 5


def test_history_requires_subject():
    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["history"])  # missing --subject


def test_history_parser_defaults():
    p = build_parser()
    args = p.parse_args(["history", "--subject", "Shell", "--limit", "5"])
    assert args.command == "history"
    assert args.subject == "Shell"
    assert args.limit == 5
    assert args.include_all_lifecycle is False
    assert args.json is False


def test_doctor_parser_defaults():
    p = build_parser()
    args = p.parse_args(["doctor"])
    assert args.command == "doctor"
    assert args.json is False
    assert args.skip_ports is False
    assert args.no_facade is False


def test_workflow_mentions_doctor(capsys):
    code = main(["workflow"])
    assert code == 0
    out = capsys.readouterr().out.lower()
    assert "doctor" in out
