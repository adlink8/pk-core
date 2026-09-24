"""应用层 shadow 生成入口（``shadow_conversation_generation``）的族级覆盖。

Integration 层：真实临时目录 + 真实 artifact store + 真实 sqlite 生成库，
经应用层入口一次跑完 discovery → capture → adapt → generation 写入。

从 ``tests/contract/conversation_sources/test_grok.py`` 搬来的用例：它调用的是
**应用层**入口（``personal_knowledge.application.conversation.v2_sync``），不是
适配器契约，所以它的家在 integration，不在 contract。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from personal_knowledge.application.conversation.v2_sync import (
    shadow_conversation_generation,
)
from tests.contract.conversation_sources.support import grok as grok_fixtures


def test_shadow_import_keeps_one_session_for_the_directory(tmp_path: Path) -> None:
    """导入按目录把 summary 和 chat_history 交给同一次 adapt。"""
    source = tmp_path / "source"
    grok_fixtures.make_session_directory(source / "sess-a")
    db = tmp_path / "shadow.sqlite"
    report = shadow_conversation_generation(
        source_root=source,
        db=db,
        artifact_store=tmp_path / "store",
        report_path=tmp_path / "report.json",
    )
    entry = report["generations"]["grok"]
    assert entry["status"] in ("full", "partial"), entry.get("reason")

    con = sqlite3.connect(db)
    try:
        sessions = con.execute(
            "SELECT native_session_id FROM ce_sessions WHERE family = 'grok'"
        ).fetchall()
        contents = {
            row[0]
            for row in con.execute(
                "SELECT content FROM ce_events WHERE content IS NOT NULL"
            )
        }
        reasons = " ".join(
            row[0]
            for row in con.execute(
                "SELECT reason FROM ce_field_dispositions WHERE reason IS NOT NULL"
            )
        )
    finally:
        con.close()

    assert sessions == [(grok_fixtures.SESSION_ID,)]
    assert grok_fixtures.SESSION_USER_TEXT in contents
    assert grok_fixtures.SESSION_ASSISTANT_TEXT in contents
    assert "phase_changed" in reasons
    assert "tool_started" in reasons


def test_shadow_report_carries_discovery_ledger_and_covers_it_in_digest(
    tmp_path: Path,
) -> None:
    """报告新增 discovery 段，且该字段必须进入 report_digest。

    台账要「写进文件」，所以它得从发现层一路透到产品报告 JSON，并由摘要覆盖；
    否则数据变了摘要不变，摘要就在说谎。
    """
    from personal_knowledge.adapters.conversation_sources.discovery import (
        DiscoveryLedger,
        UnclaimedFile,
    )
    from personal_knowledge.application.conversation import v2_sync

    source = tmp_path / "source"
    grok_fixtures.make_session_directory(source / "sess-a")

    ledger = DiscoveryLedger(
        scanned_roots=2,
        candidates=7,
        claimed=4,
        unclaimed=[
            UnclaimedFile("grok", "a/b.jsonl", "not_this_family"),
            UnclaimedFile("grok", "c/d.jsonl", "not_this_family"),
            UnclaimedFile("grok", "e/f.jsonl", "probe_error"),
        ],
    )
    report = shadow_conversation_generation(
        source_root=source,
        db=tmp_path / "shadow-a.sqlite",
        artifact_store=tmp_path / "store-a",
        report_path=tmp_path / "report-a.json",
        discovery_ledger=ledger,
    )

    assert report["discovery"] == {
        "scanned_roots": 2,
        "candidates": 7,
        "claimed": 4,
        "unclaimed": 3,
        "unclaimed_by_reason": {"not_this_family": 2, "probe_error": 1},
        "reconcile_passed": True,
    }

    other = DiscoveryLedger(
        scanned_roots=3,
        candidates=5,
        claimed=4,
        unclaimed=[UnclaimedFile("grok", "g/h.jsonl", "not_this_family")],
    )
    report_other = shadow_conversation_generation(
        source_root=source,
        db=tmp_path / "shadow-b.sqlite",
        artifact_store=tmp_path / "store-b",
        report_path=tmp_path / "report-b.json",
        discovery_ledger=other,
    )
    assert report_other["discovery"] != report["discovery"]
    assert report["report_digest"] != report_other["report_digest"]

    # 鉴别力（与 created_at 无关）：把 discovery 从报告里去掉后重算摘要，必须
    # 与报告自身摘要不同 —— 证明 discovery 真的进了摘要，而不是时间戳在变。
    without_discovery = dict(report_other)
    del without_discovery["discovery"]
    assert (
        v2_sync._report_digest(without_discovery)
        != report_other["report_digest"]
    )


# ------------------------------- native dry-run report (ledger-aware)
#
# ``--v2-native-dry-run`` 是最便宜、只读、不抓取的路径；它也必须暴露发现层
# 台账，且只暴露计数 + 原因直方图（真机未认领 6 万多条，逐条路径不能进 JSON）。


def test_native_dry_run_report_carries_discovery_counts_and_reason_histogram(
) -> None:
    from personal_knowledge.adapters.conversation_sources.discovery import (
        DiscoveryLedger,
        UnclaimedFile,
    )
    from personal_knowledge.application.conversation.v2_sync import (
        native_dry_run_report,
    )

    grok_a = Path("/tmp/found/grok-a.jsonl")
    grok_b = Path("/tmp/found/grok-b.jsonl")
    gemini = Path("/tmp/found/gemini.json")
    found = {"grok": [grok_b, grok_a], "codex": [], "gemini": [gemini]}
    ledger = DiscoveryLedger(
        scanned_roots=3,
        candidates=9,
        claimed=5,
        unclaimed=[
            UnclaimedFile("grok", "/tmp/unclaimed/1", "not_this_family"),
            UnclaimedFile("grok", "/tmp/unclaimed/2", "not_this_family"),
            UnclaimedFile("codex", "/tmp/unclaimed/3", "probe_error"),
            UnclaimedFile("codex", "/tmp/unclaimed/4", "vanished"),
        ],
    )

    report = native_dry_run_report(found, ledger)

    assert report["mode"] == "native-dry-run"
    assert report["detected"] == {
        "gemini": [str(gemini)],
        "grok": [str(grok_a), str(grok_b)],
    }
    assert report["no_source"] == ["codex"]
    assert report["discovery"] == {
        "scanned_roots": 3,
        "candidates": 9,
        "claimed": 5,
        "unclaimed": 4,
        "unclaimed_by_reason": {
            "not_this_family": 2,
            "probe_error": 1,
            "vanished": 1,
        },
        "reconcile_passed": True,
    }
    # 硬规则：未认领的逐条路径绝不进报告。
    assert "/tmp/unclaimed" not in json.dumps(report, ensure_ascii=False)


def test_native_dry_run_report_without_ledger_keeps_legacy_shape() -> None:
    from personal_knowledge.adapters.conversation_sources.discovery import (
        DiscoveryLedger,
        UnclaimedFile,
    )
    from personal_knowledge.application.conversation.v2_sync import (
        native_dry_run_report,
    )

    grok = Path("/tmp/found/grok.jsonl")
    found = {"grok": [grok], "codex": []}
    ledger = DiscoveryLedger(
        scanned_roots=1,
        candidates=2,
        claimed=1,
        unclaimed=[UnclaimedFile("grok", "/tmp/unclaimed/x", "not_this_family")],
    )

    plain = native_dry_run_report(found)
    explicit_none = native_dry_run_report(found, None)
    with_ledger = native_dry_run_report(found, ledger)

    assert plain == explicit_none
    assert "discovery" not in plain
    assert set(plain) == {"mode", "detected", "no_source"}
    assert {
        key: plain[key] for key in ("mode", "detected", "no_source")
    } == {
        key: with_ledger[key] for key in ("mode", "detected", "no_source")
    }
    assert set(with_ledger) == {"mode", "detected", "no_source", "discovery"}

    # reconcile_passed 是算出来的，不是常量：未闭合就无法通过。
    leaky = DiscoveryLedger(
        scanned_roots=1,
        candidates=5,
        claimed=1,
        unclaimed=[UnclaimedFile("grok", "/tmp/unclaimed/y", "vanished")],
    )
    leaked = native_dry_run_report(found, leaky)["discovery"]
    assert leaked["reconcile_passed"] is False
    assert leaked["unclaimed_by_reason"] == {"vanished": 1}
