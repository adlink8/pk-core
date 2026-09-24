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
        claimed_by_family={"grok": 4},
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
        "claimed_by_family": {"grok": 4},
        "reconcile_passed": True,
    }
    assert sum(report["discovery"]["claimed_by_family"].values()) == 4

    other = DiscoveryLedger(
        scanned_roots=3,
        candidates=5,
        claimed=4,
        unclaimed=[UnclaimedFile("grok", "g/h.jsonl", "not_this_family")],
        claimed_by_family={"grok": 4},
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

    # 单独改分解（claimed 仍为 4，总和不变）后重算摘要，必须与报告自身摘要不同
    # —— 证明新增的族级分解键也在摘要覆盖范围内。
    reshaped = json.loads(json.dumps(report))
    reshaped["discovery"]["claimed_by_family"] = {"grok": 3, "gemini": 1}
    assert v2_sync._report_digest(reshaped) != report["report_digest"]


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
        # 独立字面量：grok 3 + gemini 2 == claimed 5。
        claimed_by_family={"grok": 3, "gemini": 2},
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
        "claimed_by_family": {"gemini": 2, "grok": 3},
        "reconcile_passed": True,
    }
    # 新键只放 family 名 + 计数：别名 / 嵌套已在发现层去重，总和 == claimed。
    assert all(
        isinstance(count, int)
        for count in report["discovery"]["claimed_by_family"].values()
    )
    assert sum(report["discovery"]["claimed_by_family"].values()) == 5
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
        claimed_by_family={"grok": 1},
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
    # 分解块是台账的直通视图（只多 family 名 + 计数）。
    assert with_ledger["discovery"]["claimed_by_family"] == {"grok": 1}

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


# --------------------------- 只读测量块（accounting：只记不拦）
#
# 目的只有一个：先把真机数字拿到手（发现层认领数 vs 该 generation 实际引用到
# 的 artifact 数），再决定是否升格为门禁。所以它不进 report["gates"]，也不拦
# 任何一步；输出只有 family 名、计数与布尔，不含正文 / 路径 / 凭据。


def _seed_staging_db(db: Path) -> None:
    """建一个真实 v2 staging 库（复用生产建表 DDL）。"""
    from personal_knowledge.application.conversation.event_schema import (
        create_v2_schema,
    )

    create_v2_schema(db)


def _seed_sessions(
    db: Path, *, generation_id: str, family: str, artifact_ids: tuple[str, ...]
) -> None:
    """写一代 generation 的会话行：每个 artifact 一条会话。

    ``artifact_ids`` 允许重复（同一 artifact 被多条会话引用），用来区分
    ``COUNT(DISTINCT artifact_id)`` 与 ``COUNT(*)``。
    """
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO ce_event_generations "
            "(generation_id, status, created_at) "
            "VALUES (?, 'staged', '2026-01-01T00:00:00Z')",
            (generation_id,),
        )
        for artifact_id in sorted(set(artifact_ids)):
            con.execute(
                "INSERT INTO ce_source_artifacts "
                "(artifact_id, family, source_kind, content_hash, capture_method,"
                " relative_path, byte_size) "
                "VALUES (?, ?, 'file', 'fixture-hash', 'capture', 'fixture-rel', 1)",
                (artifact_id, family),
            )
        for index, artifact_id in enumerate(artifact_ids):
            con.execute(
                "INSERT INTO ce_sessions "
                "(generation_id, session_id, family, artifact_id, native_locator,"
                " contract_version, fidelity_json) "
                "VALUES (?, ?, ?, ?, 'fixture-locator', '1.0.0', '{}')",
                (generation_id, f"fixture-sess-{index}", family, artifact_id),
            )
        con.commit()
    finally:
        con.close()


def test_accounting_block_uses_distinct_session_artifact_count(
    tmp_path: Path,
) -> None:
    """右值是该 generation 的会话去重引用数，不是会话行数。

    场景：codex 的两条会话引用同一个 artifact。若右侧写成 ``COUNT(*)``，
    右值会变成 2，delta 变成 -1，本条即红。
    """
    from personal_knowledge.application.conversation import v2_sync

    db = tmp_path / "staging.sqlite"
    _seed_staging_db(db)
    _seed_sessions(
        db, generation_id="gen-codex", family="codex",
        artifact_ids=("fixture-art-1", "fixture-art-1"),
    )
    _seed_sessions(
        db, generation_id="gen-copilot", family="copilot",
        artifact_ids=("fixture-art-2",),
    )
    copilot_entry = {"family": "copilot", "generation_id": "gen-copilot"}
    generations = {
        "codex": {"family": "codex", "generation_id": "gen-codex"},
        "copilot": dict(copilot_entry),
        # 别名键与属主指向同一批行：分解只按属主家族记一次。
        "vscode-copilot": dict(copilot_entry),
        # 台账里没有认领数的家族（blocked / no_source）必须保留，左值记 0。
        "pi": {"family": "pi", "generation_id": None, "status": "blocked"},
    }

    accounting = v2_sync._accounting_by_family(
        db, generations, {"codex": 1, "copilot": 1}
    )

    # 只有 by_family 一块：左侧文件数 / 右侧去重引用数 / 差，没有聚合结论字段。
    assert accounting == {
        "by_family": {
            "codex": {
                "claimed": 1, "referenced_artifacts": 1, "delta": 0,
            },
            "copilot": {
                "claimed": 1, "referenced_artifacts": 1, "delta": 0,
            },
            "pi": {"claimed": 0, "referenced_artifacts": 0, "delta": 0},
        },
    }
    assert "closed" not in accounting
    # 只有 family 名与计数：路径与正文一律不进测量块。
    assert str(tmp_path) not in json.dumps(accounting, ensure_ascii=False)


def test_accounting_block_reports_delta_without_conclusion(tmp_path: Path) -> None:
    """少一个 artifact：delta = 左 - 右 = 1（差额本身，不是丢失结论）。"""
    from personal_knowledge.application.conversation import v2_sync

    db = tmp_path / "staging.sqlite"
    _seed_staging_db(db)
    _seed_sessions(
        db, generation_id="gen-codex", family="codex",
        artifact_ids=("fixture-art-1",),
    )
    generations = {"codex": {"family": "codex", "generation_id": "gen-codex"}}

    accounting = v2_sync._accounting_by_family(
        db, generations, {"codex": 2, "grok": 3}
    )

    assert accounting["by_family"]["codex"] == {
        "claimed": 2, "referenced_artifacts": 1, "delta": 1,
    }
    # 独立复算：右值只有 1 个不同 artifact，左值 2 → 差必为 1。
    assert accounting["by_family"]["codex"]["delta"] == 2 - 1
    # 台账有认领数、但报告里没有对应 generation 的家族照样保留（不许静默丢）。
    assert accounting["by_family"]["grok"] == {
        "claimed": 3, "referenced_artifacts": 0, "delta": 3,
    }
    assert "closed" not in accounting


def test_accounting_block_query_failure_is_not_a_zero(tmp_path: Path) -> None:
    """查不出来 != 0：库缺失 / 库损坏时右值与差都记 None，家族仍保留。"""
    from personal_knowledge.application.conversation import v2_sync

    generations = {
        "codex": {"family": "codex", "generation_id": "gen-codex"},
        # 无 generation 的家族：这才是真的 0，与查不出来必须区分。
        "blocked-owner": {"family": "blocked-owner", "generation_id": None},
    }

    missing = tmp_path / "absent.sqlite"  # 根本不存在
    corrupt = tmp_path / "corrupt.sqlite"  # 存在但不是 sqlite 库
    corrupt.write_bytes(b"definitely not a sqlite database")

    for db in (missing, corrupt):
        accounting = v2_sync._accounting_by_family(
            db, generations, {"codex": 2, "blocked-owner": 4}
        )["by_family"]
        assert accounting["codex"] == {
            "claimed": 2, "referenced_artifacts": None, "delta": None,
        }
        assert accounting["blocked-owner"] == {
            "claimed": 4, "referenced_artifacts": 0, "delta": 4,
        }
        assert set(accounting) == {"codex", "blocked-owner"}


def test_shadow_report_carries_read_only_accounting_block(tmp_path: Path) -> None:
    """shadow 报告带 accounting 段：有台账才产出，且既不进 gates 也进摘要。"""
    from personal_knowledge.adapters.conversation_sources.discovery import (
        DiscoveryLedger,
    )
    from personal_knowledge.application.conversation import v2_sync

    source = tmp_path / "source"
    grok_fixtures.make_session_directory(source / "sess-a")
    ledger = DiscoveryLedger(
        scanned_roots=1, candidates=1, claimed=1, claimed_by_family={"grok": 1},
    )
    report = shadow_conversation_generation(
        source_root=source,
        db=tmp_path / "shadow-accounting.sqlite",
        artifact_store=tmp_path / "store-accounting",
        report_path=tmp_path / "report-accounting.json",
        discovery_ledger=ledger,
    )

    assert report["accounting"]["by_family"]["grok"] == {
        "claimed": 1, "referenced_artifacts": 1, "delta": 0,
    }
    # 测量块只有 by_family，没有任何聚合结论字段（closed 之类一律不产出）。
    assert set(report["accounting"]) == {"by_family"}
    # 只记不拦：测量块不得进 gates，也不得改变 gates 的形状。
    assert "accounting" not in report["gates"]
    assert set(report["gates"]) == {
        "uncovered_sources", "detected_families_unblocked", "overall",
    }
    # 摘要覆盖测量块：单独改右值后重算摘要，必须与报告自身摘要不同。
    reshaped = json.loads(json.dumps(report))
    reshaped["accounting"]["by_family"]["grok"]["referenced_artifacts"] = 0
    assert v2_sync._report_digest(reshaped) != report["report_digest"]
