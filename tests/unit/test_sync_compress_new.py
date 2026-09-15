"""pk-sync --compress-new is opt-in and never runs on dry-run."""
from __future__ import annotations

from types import SimpleNamespace

import personal_knowledge.application.sync as sync


def test_parser_exposes_compress_new_flag() -> None:
    parser = sync.build_parser()
    args = parser.parse_args(["conversations", "--write", "--compress-new"])
    assert args.compress_new is True
    dry = parser.parse_args(["conversations"])
    assert dry.compress_new is False


def test_dry_run_does_not_compress(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(
        "personal_knowledge.application.run_pipeline.run_agentsview_stage",
        lambda write: True,
    )
    monkeypatch.setattr(sync, "_run_compress_new", lambda: called.append("ran") or 0)
    rc = sync._cmd_conversations(
        write=False, args=SimpleNamespace(compress_new=True, v2_dry_run=False,
                                         v2_shadow=False, v2_activate=False,
                                         v2_native=False, v2_native_dry_run=False)
    )
    assert rc == 0
    assert called == []


def test_write_compress_new_invokes(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(
        "personal_knowledge.application.run_pipeline.run_agentsview_stage",
        lambda write: True,
    )
    monkeypatch.setattr(sync, "_record_conversation_versions", lambda: [])
    monkeypatch.setattr(
        sync,
        "publish_conversation_delta_committed",
        lambda **kwargs: {"published": False, "reason": "test"},
    )
    monkeypatch.setattr(sync, "_run_compress_new", lambda: called.append("ran") or 0)
    rc = sync._cmd_conversations(
        write=True, args=SimpleNamespace(compress_new=True, v2_dry_run=False,
                                         v2_shadow=False, v2_activate=False,
                                         v2_native=False, v2_native_dry_run=False)
    )
    assert rc == 0
    assert called == ["ran"]
