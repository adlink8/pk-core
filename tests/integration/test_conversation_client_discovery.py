"""Phase 62 discovery seam: client-directory discovery + incremental staging.

Red test first (engineering contract): the client discovery layer must
(a) map each registered family to machine-local candidate roots,
(b) probe files with the owning family detector (reusing registry.detect_family,
    never a second parser), and (c) stage only new/changed files into a
    shadow-compatible source root, deduped by content hash.

Integration layer: real temporary client roots on disk plus the real discovery
module. Family identity always comes from the family-level seam ``registry``
(``known_families`` / ``resolve_family``), never from a family module.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.adapters.conversation_sources import discovery, registry
from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact
from personal_knowledge.adapters.conversation_sources.discovery import (
    FAMILY_CLIENT_ROOTS,
    discover_client_sources,
    probe_source_kind,
    stage_client_sources,
)


def _write_codex_jsonl(root: Path, name: str, first_role: str = "user") -> Path:
    path = root / "sessions" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "session_meta", "session_id": "s1", "timestamp": 1})
        + "\n"
        + json.dumps(
            {"type": "response_item", "session_id": "s1",
             "payload": {"role": first_role, "content": [{"type": "text", "text": "hi"}]}}
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_cursor_sqlite(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE threads (id TEXT, title TEXT, created_at TEXT)")
        con.execute("CREATE TABLE messages (id TEXT, session_id TEXT, role TEXT, content TEXT, created_at TEXT)")
        con.execute("INSERT INTO threads VALUES ('t1','demo','2026-01-01T00:00:00Z')")
        con.commit()
    finally:
        con.close()
    return path


def test_family_client_roots_are_explicit() -> None:
    """Every registered family that has native directories carries candidates."""
    for family in registry.known_families():
        # chatgpt is manual-import (no native root) and must be declared as such.
        assert family in FAMILY_CLIENT_ROOTS, f"{family} missing in FAMILY_CLIENT_ROOTS"
        # 别名不另立契约：它解析出的属主家同样必须在 roots 表里。
        assert registry.resolve_family(family) in FAMILY_CLIENT_ROOTS


def test_probe_source_kind_detects_sqlite_and_file(tmp_path: Path) -> None:
    db = _write_cursor_sqlite(tmp_path / "store.sqlite")
    txt = tmp_path / "note.txt"
    txt.write_text("hello", encoding="utf-8")
    assert probe_source_kind(db) == "sqlite"
    assert probe_source_kind(txt) == "file"


def test_discover_codex_and_cursor_from_client_roots(tmp_path: Path) -> None:
    codex_file = _write_codex_jsonl(tmp_path / "home" / ".codex", "s1.jsonl")
    cursor_db = _write_cursor_sqlite(tmp_path / "home" / ".cursor" / "project.db")

    roots = {
        "codex": (tmp_path / "home" / ".codex",),
        "cursor": (tmp_path / "home" / ".cursor",),
    }
    found = discover_client_sources(roots=roots)
    # 发现结果只包含 registry 已登记的家族，别名不另起一族。
    assert set(found) <= set(registry.known_families())
    assert "codex" in found
    assert any(p == codex_file for p in found["codex"])
    assert "cursor" in found
    assert any(p == cursor_db for p in found["cursor"])


def test_discovery_ignores_foreign_files(tmp_path: Path) -> None:
    (tmp_path / "home" / ".codex").mkdir(parents=True)
    foreign = tmp_path / "home" / ".codex" / "config.toml"
    foreign.write_text("model = 'x'", encoding="utf-8")
    found = discover_client_sources(
        roots={"codex": (tmp_path / "home" / ".codex",)}
    )
    assert found.get("codex", []) == []


def test_stage_incremental_deduplicates_by_hash(tmp_path: Path) -> None:
    src = _write_codex_jsonl(tmp_path / "home" / ".codex", "s1.jsonl")
    stage = tmp_path / "stage"
    roots = {"codex": (tmp_path / "home" / ".codex",)}

    first = stage_client_sources(roots=roots, stage_root=stage,
                                 byte_limit=10_000, count_limit=100)
    assert first["staged"] == 1
    assert first["skipped"] == 0
    assert (stage / "codex" / "sessions" / "s1.jsonl").exists()

    second = stage_client_sources(roots=roots, stage_root=stage,
                                  byte_limit=10_000, count_limit=100)
    assert second["staged"] == 0
    assert second["skipped"] == 1

    # mutate -> staged again
    src.write_text(src.read_text(encoding="utf-8") + json.dumps({"x": 1}) + "\n", encoding="utf-8")
    third = stage_client_sources(roots=roots, stage_root=stage,
                                 byte_limit=10_000, count_limit=100)
    assert third["staged"] == 1


def test_stage_sqlite_uses_wal_safe_snapshot(tmp_path: Path) -> None:
    """SQLite family sources stage via online-backup snapshot, not loose copy."""
    from personal_knowledge.adapters.conversation_sources.discovery import (
        SQLITE_ALLOWLISTS,
        snapshot_sqlite_to_file,
    )

    db = tmp_path / "home" / ".zcode" / "cli" / "db" / "db.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE session (id TEXT, parent_id TEXT, title TEXT, time_created INTEGER, time_updated INTEGER, time_compacting TEXT, trace_id TEXT, directory TEXT, path TEXT)")
        con.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT, sequence INTEGER)")
        con.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT, sequence INTEGER)")
        con.execute("CREATE TABLE credentials (token TEXT)")  # forbidden adjacency
        con.execute("INSERT INTO session VALUES ('s1', NULL, 'demo', 1, 1, NULL, 't1', NULL, NULL)")
        con.execute("INSERT INTO credentials VALUES ('sk-secret')")
        con.commit()
    finally:
        con.close()

    tables, columns = SQLITE_ALLOWLISTS["zcode"]
    target = tmp_path / "stage" / "zcode" / "db.sqlite"
    digest = snapshot_sqlite_to_file(db, target, allowed_tables=tables, allowed_columns=columns)

    # snapshot is a valid sqlite store, credential table excluded
    check = sqlite3.connect(target)
    try:
        tables_now = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "credentials" not in tables_now, "forbidden table leaked into snapshot"
        assert {"session", "message", "part"} <= tables_now
        assert check.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 1
    finally:
        check.close()
    assert isinstance(digest, str) and len(digest) == 64
    # capture intermediates must not land in the stage tree: the tree is
    # re-scanned on the next run, so a leaked temp would be re-adapted as a
    # second copy of the same trajectory and collide on event ids.
    assert [p.name for p in target.parent.iterdir() if p.name.startswith(".")] == []


def _write_store_with_forbidden_table(path: Path) -> Path:
    """Real source store: one allowlisted table (literal rows) + one outside it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE messages (id TEXT, role TEXT, body TEXT)")
        con.execute("CREATE TABLE secrets (token TEXT)")
        con.execute("INSERT INTO messages VALUES ('m1','user','alpha')")
        con.execute("INSERT INTO messages VALUES ('m2','assistant','beta')")
        con.execute("INSERT INTO messages VALUES ('m3','user','gamma')")
        con.execute("INSERT INTO secrets VALUES ('credential-value')")
        con.commit()
    finally:
        con.close()
    return path


def test_snapshot_keeps_whole_store_under_a_tiny_byte_limit(tmp_path: Path) -> None:
    """byte_limit must not discard the whole snapshot (keep-everything intent).

    A live store above the entry threshold used to stage zero rows, so the
    entire family disappeared. The capture must keep the filtered snapshot
    regardless of size; the allowlist alone decides what it may contain.
    """
    from personal_knowledge.adapters.conversation_sources.discovery import (
        snapshot_sqlite_to_file,
    )

    source = _write_store_with_forbidden_table(tmp_path / "live" / "store.sqlite")
    target = tmp_path / "stage" / "family" / "store.sqlite"

    snapshot_sqlite_to_file(
        source,
        target,
        allowed_tables=("messages",),
        allowed_columns={"messages": ("id", "role", "body")},
        byte_limit=1,
    )

    assert target.is_file()
    check = sqlite3.connect(target)
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        names = {
            r[0]
            for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "messages" in names
        assert "secrets" not in names
        rows = check.execute(
            "SELECT id, role, body FROM messages ORDER BY id"
        ).fetchall()
        assert rows == [
            ("m1", "user", "alpha"),
            ("m2", "assistant", "beta"),
            ("m3", "user", "gamma"),
        ]
    finally:
        check.close()


def _write_zcode_shaped_store(path: Path) -> Path:
    """Minimal real zcode store: allowlisted tables + a credential-adjacent one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE session (id TEXT, parent_id TEXT, title TEXT, time_created INTEGER, time_updated INTEGER, time_compacting TEXT, trace_id TEXT, directory TEXT, path TEXT)")
        con.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT, sequence INTEGER)")
        con.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT, sequence INTEGER)")
        con.execute("CREATE TABLE credentials (token TEXT)")
        con.execute("INSERT INTO session VALUES ('s1', NULL, 'demo', 1, 1, NULL, 't1', NULL, NULL)")
        con.execute("INSERT INTO message VALUES ('m1','s1',1,1,'{\"role\":\"user\"}',0)")
        con.execute("INSERT INTO part VALUES ('p1','m1','s1',1,1,'{\"type\":\"text\",\"text\":\"alpha\"}',0)")
        con.execute("INSERT INTO credentials VALUES ('credential-value')")
        con.commit()
    finally:
        con.close()
    return path


def test_snapshot_digest_matches_published_file_and_manifest_name(tmp_path: Path) -> None:
    """Digest == sha256 of the published snapshot, keyed by its mirror name.

    The seam hashes the bytes it writes; the test hashes the file that landed
    on disk, so the two digests come from independent paths. The caller
    convention (``stage_client_sources``) records the hash in
    ``<stage>/<family>/.hashes.json`` under the mirror-relative name.
    """
    from personal_knowledge.adapters.conversation_sources.discovery import (
        SQLITE_ALLOWLISTS,
        snapshot_sqlite_to_file,
    )

    source_root = tmp_path / "home" / ".zcode" / "cli" / "db"
    source = _write_zcode_shaped_store(source_root / "db.sqlite")
    tables, columns = SQLITE_ALLOWLISTS["zcode"]

    target = tmp_path / "stage" / "zcode" / "db.sqlite"
    digest = snapshot_sqlite_to_file(
        source, target, allowed_tables=tables, allowed_columns=columns
    )
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()

    caller_stage = tmp_path / "stage-caller"
    report = stage_client_sources(roots={"zcode": (source_root,)}, stage_root=caller_stage)
    assert report["families"]["zcode"]["staged"] == 1
    manifest = json.loads(
        (caller_stage / "zcode" / ".hashes.json").read_text(encoding="utf-8")
    )
    assert list(manifest) == ["db.sqlite"]
    assert manifest["db.sqlite"] == digest
    assert manifest["db.sqlite"] == hashlib.sha256(
        (caller_stage / "zcode" / "db.sqlite").read_bytes()
    ).hexdigest()


def test_discovery_skips_files_that_vanish_mid_scan(tmp_path: Path) -> None:
    """A SQLite -shm/-wal sidecar can disappear between listing and stat.

    Regression 2026-09-23: discover_client_sources() raised FileNotFoundError
    on ``~/.gemini/antigravity/conversation_summaries.db-shm`` during a real
    native dry-run. A file that no longer exists has nothing to ingest, so the
    probe must skip it instead of crashing the whole scan.
    """
    ghost = tmp_path / "conversation_summaries.db-shm"
    ghost.write_text("")  # listed by _walk, then removed before _artifact_for
    ghost.unlink()

    # 竞争窗口无法用公开入口确定性重现（公开入口只在 walk 列过的现存文件上
    # 走这条路），所以这里保留对 ``_artifact_for`` 的最小私有探针：它是唯一
    # 能把「已消失的文件 → None」钉死的入口。
    assert discovery._artifact_for(ghost) is None

    # and a scan over a root containing only vanished files yields nothing
    # rather than raising
    roots = {"codex": (tmp_path,)}
    assert discover_client_sources(roots) == {"codex": []}


# ------------------------------------------- Fix 3: codex 的 discovery 根目录
#
# audit 2026-09-23 Fix 3：``~/.codex`` 下同时存在两棵会话目录树 —— live 的
# ``sessions/``（按日期分层）与轮转归档的 ``archived_sessions/``（扁平）。
# 两棵都要被发现，且 slot 身份（相对各根的路径）不得把不同会话映到同一目标。


def _codex_rollout(name: str) -> str:
    return json.dumps(
        {
            "timestamp": "2026-03-29T10:23:20.293Z",
            "ordinal": 0,
            "type": "session_meta",
            "payload": {
                "session_id": name,
                "id": name,
                "timestamp": "2026-03-29T10:22:48.025Z",
                "cwd": "C:\\Users\\li",
                "originator": "codex_cli_rs",
            },
        },
        ensure_ascii=False,
    )


class TestCodexDiscoveryArchivedRoot:
    def test_default_roots_include_archived_sessions(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".codex" / "sessions").mkdir(parents=True)
        (home / ".codex" / "archived_sessions").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        # ``FAMILY_CLIENT_ROOTS`` 是 import 期算出来的常量，只看真实 home，
        # 无法观察 monkeypatch 后的 home；这里必须重算默认根。
        roots = {str(r) for r in discovery._default_roots()["codex"]}
        assert str((home / ".codex" / "sessions").resolve()) in roots
        assert str((home / ".codex" / "archived_sessions").resolve()) in roots

    def test_both_trees_discovered_without_duplicates(self, tmp_path):
        # live 与 archived 是不同文件：两棵目录树都要被发现，且同一文件不捕两次。
        sessions_root = tmp_path / "sessions"
        archived_root = tmp_path / "archived_sessions"
        (sessions_root / "2026" / "03").mkdir(parents=True)
        archived_root.mkdir(parents=True)
        live = sessions_root / "2026" / "03" / "rollout-2026-03-29T18-22-48-live.jsonl"
        old = archived_root / "rollout-2026-01-31T21-51-26-old.jsonl"
        live.write_text(_codex_rollout("live") + "\n", encoding="utf-8")
        old.write_text(_codex_rollout("old") + "\n", encoding="utf-8")
        found = discover_client_sources(
            roots={"codex": (sessions_root, archived_root)}
        )
        assert set(found) <= set(registry.known_families())
        paths = found["codex"]
        assert len(paths) == 2
        assert len(set(paths)) == 2
        assert live in paths and old in paths

    def test_stage_slot_identity_not_shared_across_trees(self, tmp_path):
        # slot 身份取相对各根的路径：live 的日期子目录前缀与 archived 的扁平
        # 路径不会把不同会话映到同一 stage 目标。
        sessions_root = tmp_path / "sessions"
        archived_root = tmp_path / "archived_sessions"
        (sessions_root / "2026" / "03").mkdir(parents=True)
        archived_root.mkdir(parents=True)
        live = sessions_root / "2026" / "03" / "rollout-2026-03-29T18-22-48-x.jsonl"
        old = archived_root / "rollout-2026-03-29T18-22-48-x.jsonl"
        live.write_text(_codex_rollout("x") + "\n", encoding="utf-8")
        old.write_text(_codex_rollout("x") + "\n", encoding="utf-8")
        rels = {
            src.relative_to(root).as_posix()
            for root, src in ((sessions_root, live), (archived_root, old))
        }
        assert rels == {
            "2026/03/rollout-2026-03-29T18-22-48-x.jsonl",
            "rollout-2026-03-29T18-22-48-x.jsonl",
        }


@pytest.mark.skipif(
    not (Path.home() / ".codex" / "archived_sessions").is_dir(),
    reason="本机没有 ~/.codex/archived_sessions 真实目录",
)
def test_real_codex_archived_root_registered():
    # 真实环境佐证：archived_sessions 与 sessions 都是已注册的 codex 根。
    roots = {r.as_posix() for r in FAMILY_CLIENT_ROOTS.get("codex", ())}
    assert any(r.endswith(".codex/sessions") for r in roots)
    assert any(r.endswith(".codex/archived_sessions") for r in roots)


# ----------------------------------------- chatgpt 的发现根（AgentsView 兼容通道）
#
# 本机没有 ChatGPT 原生导出目录，所以 chatgpt 一直是空根 —— 适配器、允许清单、
# 探测器都在，但没有任何源喂给它，整族 0 条。唯一的本地锚点是 AgentView 的
# 只读库 ``~/.agentsview/sessions.db``（``chatgpt.detect`` 认的就是
# ``sessions.db`` / agentsview 形态的 sqlite）。发现层必须把它注册成 chatgpt
# 的根，否则这条兼容观测通道永远不会被走。


class TestChatgptDiscoveryRoot:
    def test_default_roots_include_agentsview_store(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".agentsview").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        # ``FAMILY_CLIENT_ROOTS`` 是 import 期算出来的常量，只看真实 home；
        # 这里必须重算默认根才能观察 monkeypatch 后的 home。
        roots = {str(r) for r in discovery._default_roots()["chatgpt"]}
        assert str((home / ".agentsview").resolve()) in roots

    def test_real_agentsview_store_is_claimed_as_chatgpt(self, tmp_path):
        """注册成根之后，真实 AgentsView 库必须被 chatgpt 探测器认领。"""
        home = tmp_path / "home"
        store = home / ".agentsview" / "sessions.db"
        store.parent.mkdir(parents=True)
        con = sqlite3.connect(store)
        try:
            con.execute(
                "CREATE TABLE sessions (id TEXT, agent TEXT, started_at TEXT, "
                "ended_at TEXT, deleted_at TEXT, file_path TEXT)"
            )
            con.execute(
                "CREATE TABLE messages (id TEXT, session_id TEXT, ordinal INTEGER, "
                "role TEXT, content TEXT, timestamp TEXT, is_system INTEGER, "
                "is_sidechain INTEGER)"
            )
            con.execute(
                "INSERT INTO sessions VALUES ('c1','chatgpt','2026-01-01T00:00:00Z',"
                "NULL,NULL,NULL)"
            )
            con.execute(
                "INSERT INTO messages VALUES "
                "('m1','c1',1,'user','hi','2026-01-01T00:00:01Z',0,0)"
            )
            con.commit()
        finally:
            con.close()

        found = discover_client_sources(
            roots={"chatgpt": (store.parent,)},
        )
        assert found["chatgpt"] == [store]


@pytest.mark.skipif(
    not (Path.home() / ".agentsview" / "sessions.db").is_file(),
    reason="本机没有 ~/.agentsview/sessions.db 真实 AgentsView 库",
)
def test_real_agentsview_root_registered():
    # 真实环境佐证：AgentsView 库目录是已注册的 chatgpt 根。
    roots = {r.as_posix() for r in FAMILY_CLIENT_ROOTS.get("chatgpt", ())}
    assert any(r.endswith(".agentsview") for r in roots)


# ------------------------------------------------- unclaimed-file discovery ledger
#
# 台账只增加可见性：认领结果必须与不传 ledger 时逐比特一致。


def test_ledger_counts_claimed_file_without_changing_result(tmp_path: Path) -> None:
    from personal_knowledge.adapters.conversation_sources.discovery import (
        DiscoveryLedger,
    )

    codex_file = _write_codex_jsonl(tmp_path / "home" / ".codex", "s1.jsonl")
    roots = {"codex": (tmp_path / "home" / ".codex",)}

    ledger = DiscoveryLedger()
    with_ledger = discover_client_sources(roots, ledger=ledger)

    assert codex_file in with_ledger["codex"]
    assert ledger.candidates == 1
    assert ledger.claimed == 1
    assert ledger.unclaimed == []

    without_ledger = discover_client_sources(roots)
    assert without_ledger == with_ledger


def test_ledger_records_file_rejected_by_family_detector(tmp_path: Path) -> None:
    from personal_knowledge.adapters.conversation_sources.discovery import (
        DiscoveryLedger,
    )

    codex_root = tmp_path / "home" / ".codex"
    codex_root.mkdir(parents=True)
    foreign = codex_root / "config.toml"
    foreign.write_text("model = 'x'", encoding="utf-8")
    roots = {"codex": (codex_root,)}

    ledger = DiscoveryLedger()
    found = discover_client_sources(roots, ledger=ledger)

    assert found.get("codex", []) == []
    assert len(ledger.unclaimed) == 1
    entry = ledger.unclaimed[0]
    assert entry.reason == "not_this_family"
    assert Path(entry.path) == foreign
    assert entry.family == "codex"
    assert ledger.candidates == 1
    assert ledger.claimed == 0


def test_stage_client_sources_passes_ledger_through(tmp_path: Path) -> None:
    """stage_client_sources 把 ledger 透传给 discovery，且返回值与不传时一致。"""
    from personal_knowledge.adapters.conversation_sources.discovery import (
        DiscoveryLedger,
    )

    codex_root = tmp_path / "home" / ".codex"
    claimed = _write_codex_jsonl(codex_root, "s1.jsonl")
    foreign = codex_root / "config.toml"
    foreign.write_text("model = 'x'", encoding="utf-8")
    roots = {"codex": (codex_root,)}

    ledger = DiscoveryLedger()
    with_ledger = stage_client_sources(
        stage_root=tmp_path / "stage-ledger",
        roots=roots, byte_limit=10_000, count_limit=100, ledger=ledger,
    )

    assert ledger.candidates == 2
    assert ledger.claimed == 1
    assert len(ledger.unclaimed) == 1
    assert Path(ledger.unclaimed[0].path) == foreign
    assert ledger.unclaimed[0].reason == "not_this_family"
    assert Path(claimed).exists()

    without_ledger = stage_client_sources(
        stage_root=tmp_path / "stage-plain",
        roots=roots, byte_limit=10_000, count_limit=100,
    )
    assert without_ledger == with_ledger


def test_ledger_records_probe_error_without_changing_result(tmp_path: Path) -> None:
    """探测阶段抛异常的文件必须留痕，而不是被静默吞掉。

    改动前这里是 ``except Exception: continue``：真实错误零痕迹。台账存在的
    意义就是让它留痕，所以这条分支是台账最该守住的一条。

    确定性触发器：传一个未登记的家族名。registry.detect_family 会先经过
    resolve_family 并抛 KeyError，从而走进 probe_error 分支——不是
    not_this_family（探测器没跑起来），也不是 vanished（文件真实存在）。
    异常被吞的同时返回值不受影响：可见性只增，行为不变。
    """
    from personal_knowledge.adapters.conversation_sources.discovery import (
        DiscoveryLedger,
    )

    family = "no_such_family_zzz"
    probe_file = tmp_path / "candidate.json"
    probe_file.write_text("{}", encoding="utf-8")
    roots = {family: (tmp_path,)}

    ledger = DiscoveryLedger()
    found = discover_client_sources(roots, ledger=ledger)

    assert found == {family: []}
    assert ledger.candidates == 1
    assert ledger.claimed == 0
    assert len(ledger.unclaimed) == 1
    entry = ledger.unclaimed[0]
    assert entry.reason == "probe_error"
    assert entry.family == family
    assert Path(entry.path) == probe_file

    without_ledger = discover_client_sources(roots)
    assert without_ledger == found


# ------------------------------- 同一文件只认领一次（嵌套 root / 别名共享 root）
#
# 真机实测（``pk-sync conversations --v2-native-dry-run``）：zcode 的
# ``~/.zcode/cli/db`` 嵌套在 ``~/.zcode/cli`` 里面，同一 db 文件被列两次。
# 两个 root 各有用途（一个直取 db、一个供递归），所以去重发生在「收集结果」
# 这一层，而不是删 root。


def test_nested_roots_in_one_family_claim_a_file_once(tmp_path: Path) -> None:
    """同一家族的两个嵌套 root 不得把同一文件算两次（返回值与台账都不重复）。"""
    from personal_knowledge.adapters.conversation_sources.discovery import (
        DiscoveryLedger,
    )

    root = tmp_path / "root"
    codex_file = _write_codex_jsonl(root / "nested", "s1.jsonl")
    # 后者嵌套前者：嵌套 root 直取到该文件，外层 root 递归再取一次。
    assert root / "nested" in codex_file.parents
    roots = {"codex": (root / "nested", root)}

    ledger = DiscoveryLedger()
    found = discover_client_sources(roots, ledger=ledger)

    assert found["codex"] == [codex_file]
    assert ledger.candidates == 1
    assert ledger.claimed == 1
    assert ledger.unclaimed == []


def test_alias_family_sharing_one_root_is_counted_once_in_ledger(
    tmp_path: Path,
) -> None:
    """别名家族与属主共享同一 root：台账不得把同一批文件算两遍。

    真机实测台账显示 ``copilot`` 与 ``vscode-copilot`` 各认领同一批 12 个
    文件（``resolve_family`` 把别名归一到属主）。返回值保持原样——别名键有
    外部消费者（``v2_sync.native_dry_run_report`` 按家族键出 ``detected`` /
    ``no_source``，``stage_client_sources`` 自己跳过别名键）——所以只修台账
    计数：按解析后的绝对路径全局去重。
    """
    from personal_knowledge.adapters.conversation_sources.discovery import (
        DiscoveryLedger,
    )

    root = tmp_path / "home" / ".copilot"
    (root / "session-state").mkdir(parents=True)
    trace = root / "session-state" / "events.jsonl"
    trace.write_text(
        json.dumps({"type": "session.start", "id": "s1"}) + "\n", encoding="utf-8"
    )
    assert registry.resolve_family("vscode-copilot") == "copilot"
    roots = {"copilot": (root,), "vscode-copilot": (root,)}

    ledger = DiscoveryLedger()
    found = discover_client_sources(roots, ledger=ledger)

    assert found["copilot"] == [trace]
    assert found["vscode-copilot"] == [trace]
    assert ledger.candidates == 1
    assert ledger.claimed == 1
    assert ledger.unclaimed == []
    assert ledger.candidates == ledger.claimed + len(ledger.unclaimed)


def test_ledger_decomposes_claimed_by_family_under_the_same_dedup_rule(
    tmp_path: Path,
) -> None:
    """认领数按属主家族分解：该认领的家族拿到正确的数，未认领桶里没有它。

    一个 generation 是按家族分组成批的，所以「发现层认领了 N 个文件」这句话
    必须能拆到家族上，否则台账跟任何一批 generation 都对不上账。

    场景里同时含两种「同一文件被列两次」：codex 的两个嵌套 root，以及
    copilot / vscode-copilot 共享同一 root。分解若按各家族 ``found`` 列表求长度
    （绕过全局去重），别名家族会把同一批文件各记一遍 —— copilot 会记 2 而不是
    1。本条钉住的是「每个文件在 ``claimed`` 里只算一次、在分解里各归各的属主」，
    不再断言分解总和恒等于 ``claimed``（跨家族重复时它本就不等，语义见
    ``DiscoveryLedger`` docstring）。
    """
    from personal_knowledge.adapters.conversation_sources.discovery import (
        DiscoveryLedger,
    )

    codex_root = tmp_path / "home" / ".codex"
    codex_file = _write_codex_jsonl(codex_root, "s1.jsonl")
    copilot_root = tmp_path / "home" / ".copilot"
    (copilot_root / "session-state").mkdir(parents=True)
    copilot_trace = copilot_root / "session-state" / "events.jsonl"
    copilot_trace.write_text(
        json.dumps({"type": "session.start", "id": "s1"}) + "\n", encoding="utf-8"
    )
    roots = {
        # 后者嵌套前者：同一 jsonl 被列两次。
        "codex": (codex_root / "sessions", codex_root),
        # 别名与属主共享 root：同一批文件被列两次。
        "copilot": (copilot_root,),
        "vscode-copilot": (copilot_root,),
    }

    ledger = DiscoveryLedger()
    found = discover_client_sources(roots, ledger=ledger)

    # 返回值保持原样：别名键另有消费者，不许因为改台账而消失。
    assert codex_file in found["codex"]
    assert found["copilot"] == [copilot_trace]
    assert found["vscode-copilot"] == [copilot_trace]

    # 独立字面量：两个属主家族各认领 1 个文件（嵌套 / 别名都不翻倍）。
    assert ledger.claimed == 2
    assert ledger.claimed_by_family == {"codex": 1, "copilot": 1}
    assert ledger.unclaimed == []
    assert ledger.candidates == ledger.claimed + len(ledger.unclaimed)


def test_ledger_claimed_by_family_matches_claimed_on_mixed_machine(
    tmp_path: Path,
) -> None:
    """认领 / 未认领混在一批时，分解给到正确的家族。

    未认领的文件不进分解（它们不是任何 generation 的输入），所以分母是
    ``claimed`` 而不是 ``candidates``。分解总和与 ``claimed`` 相等在本夹具
    下成立（每个文件只被一个家族接受），但那是巧合而不是契约：语义见
    ``DiscoveryLedger`` docstring（跨家族重复在分解侧各算一次）。
    """
    from personal_knowledge.adapters.conversation_sources.discovery import (
        DiscoveryLedger,
    )

    codex_root = tmp_path / "home" / ".codex"
    _write_codex_jsonl(codex_root, "s1.jsonl")
    (codex_root / "config.toml").write_text("model = 'x'", encoding="utf-8")
    roots = {"codex": (codex_root,)}

    ledger = DiscoveryLedger()
    discover_client_sources(roots, ledger=ledger)

    assert ledger.candidates == 2
    assert ledger.claimed == 1
    assert len(ledger.unclaimed) == 1
    assert ledger.claimed_by_family == {"codex": 1}
    assert ledger.candidates == ledger.claimed + len(ledger.unclaimed)


# --------------------- 跨家族嵌套 root：认领归属必须在走完所有家族后才定
#
# 真机实测（``pk-sync conversations --v2-native-dry-run``）：gemini 的 root
# ``~/.gemini`` 里嵌着 antigravity 的 root ``~/.gemini/antigravity``（根表里
# 唯一一对跨家族嵌套）。先走的 gemini 会走到 antigravity 的文件并拒收，后走的
# antigravity 会接受它们。归属若在「第一次见到」时就定死，这批文件会被记进
# gemini 的 ``not_this_family``，而真正认领它们的 antigravity 一个数都不加
# ——而算术仍然闭合（candidates == claimed + len(unclaimed)），只有按家族比
# 才暴露。


def _write_antigravity_live_store(path: Path) -> Path:
    """最小 antigravity live 库：带 ``trajectory_meta`` 表即被本族探测器接受。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE trajectory_meta (trajectory_id TEXT, cascade_id TEXT)"
        )
        con.execute("INSERT INTO trajectory_meta VALUES ('t1','c1')")
        con.commit()
    finally:
        con.close()
    return path


def test_nested_cross_family_root_is_claimed_by_the_accepting_family(
    tmp_path: Path,
) -> None:
    """嵌套在 gemini root 里的 antigravity 文件：算 antigravity 认领，不进 unclaimed。"""
    from personal_knowledge.adapters.conversation_sources.discovery import (
        DiscoveryLedger,
    )

    gemini_root = tmp_path / "home" / ".gemini"
    nested = gemini_root / "antigravity"
    nested.mkdir(parents=True)
    store = _write_antigravity_live_store(nested / "conversation_summaries.db")
    # 对照：gemini 自己的会话文件仍由 gemini 认领（防止把 gemini 整体判死）。
    session = gemini_root / "kept.json"
    session.write_text(
        json.dumps({"messages": []}), encoding="utf-8"
    )

    roots = {"gemini": (gemini_root,), "antigravity": (nested,)}

    ledger = DiscoveryLedger()
    found = discover_client_sources(roots, ledger=ledger)

    # 返回值（抓取快照）：每个家族都照旧列出它探测器接受的文件。
    assert found["antigravity"] == [store]
    assert session in found["gemini"]

    # 归属：两个不同文件各被一个家族接受。
    assert ledger.claimed == 2
    assert ledger.claimed_by_family == {"gemini": 1, "antigravity": 1}
    # 合法跳过的桶必须干净：真正被别的家族认领的文件不许混进 not_this_family。
    assert [Path(entry.path) for entry in ledger.unclaimed] == []
    assert ledger.candidates == ledger.claimed + len(ledger.unclaimed)

    # 台账只增可见性：不传 ledger 的结果逐比特一致。
    plain = discover_client_sources(roots)
    assert plain == found
