"""conversation_fts 测试。

用 tmp_path 造小型权威库（schema 对齐真实 canonical_messages /
canonical_sessions 的列布局，内容以中文为主 + 英文），断言：
建索引、中英文查询命中、snippet 生成、增量刷新只收新行、FTS 运算符
查询不炸、索引构建前后权威库 md5 不变。
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.retrieval import conversation_fts as cf
from personal_knowledge.retrieval.conversation_fts import ConversationFtsError

# 真实权威库 canonical_messages / canonical_sessions 的列布局（实测 2026-09）
AUTHORITY_SCHEMA = """
CREATE TABLE canonical_messages (
    canonical_message_id TEXT PRIMARY KEY,
    canonical_session_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_message_ref TEXT,
    ordinal INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    content_length INTEGER,
    timestamp TEXT,
    model TEXT,
    is_system INTEGER NOT NULL DEFAULT 0,
    is_sidechain INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT,
    evidence_scope TEXT NOT NULL DEFAULT 'user'
);
CREATE TABLE canonical_sessions (
    canonical_session_id TEXT PRIMARY KEY,
    primary_source TEXT NOT NULL,
    agent TEXT,
    started_at TEXT,
    ended_at TEXT,
    message_count INTEGER,
    user_message_count INTEGER,
    file_hash TEXT,
    parent_canonical_id TEXT,
    relationship_type TEXT,
    cwd TEXT,
    git_branch TEXT,
    model TEXT,
    evidence_eligible INTEGER NOT NULL DEFAULT 1,
    evidence_scope TEXT NOT NULL DEFAULT 'user',
    merged INTEGER NOT NULL DEFAULT 0,
    lifecycle TEXT NOT NULL DEFAULT 'active',
    superseded_by_canonical_id TEXT
);
-- P1-14：FTS 失效台账（event_schema.ce_fts_invalidate 的实测布局）
CREATE TABLE ce_fts_invalidate (
    canonical_message_id TEXT PRIMARY KEY,
    changed_at TEXT NOT NULL
);
"""

# (session_id, ordinal, role, content, timestamp)；末行为空 content
MESSAGES = [
    ("sess-alpha", 0, "user", "帮我看一下数据分析的流程怎么设计，重点是报表部分", "2026-09-01T01:00:00Z"),
    ("sess-alpha", 1, "assistant", "数据分析通常分三步：采集、清洗、建模。报表用 pivot table 就行", "2026-09-01T01:01:00Z"),
    ("sess-alpha", 2, "user", "代理设置在哪修改？我想配一个 http proxy", "2026-09-01T01:02:00Z"),
    ("sess-beta", 0, "user", "语义压缩 semantic compression 的配置项有哪些", "2026-09-02T02:00:00Z"),
    ("sess-beta", 1, "assistant", "The waterfall plan needs real-time metrics AND a rollback trigger", "2026-09-02T02:01:00Z"),
    ("sess-gamma", 0, "user", "   ", "2026-09-03T03:00:00Z"),
]

SESSIONS = [
    ("sess-alpha", "agentsview", "claude-code", "2026-09-01T01:00:00Z", "2026-09-01T01:02:30Z", 3, 2, "D:/proj", "main", "opus"),
    ("sess-beta", "legacy", "codex", "2026-09-02T02:00:00Z", "2026-09-02T02:05:00Z", 2, 1, "D:/other", "dev", "gpt"),
    ("sess-gamma", "agentsview", "claude-code", "2026-09-03T03:00:00Z", None, 1, 1, None, None, None),
]


@pytest.fixture()
def authority_db(tmp_path: Path) -> Path:
    db = tmp_path / "authority.sqlite"
    con = sqlite3.connect(db)
    con.executescript(AUTHORITY_SCHEMA)
    con.executemany(
        "INSERT INTO canonical_messages "
        "(canonical_message_id, canonical_session_id, source, ordinal, role, content, content_length, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (f"cm-{i}", s, "agentsview", o, r, c, len(c or ""), t)
            for i, (s, o, r, c, t) in enumerate(MESSAGES, 1)
        ],
    )
    con.executemany(
        "INSERT INTO canonical_sessions "
        "(canonical_session_id, primary_source, agent, started_at, ended_at, "
        "message_count, user_message_count, cwd, git_branch, model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        SESSIONS,
    )
    con.commit()
    con.close()
    return db


@pytest.fixture()
def index_db(tmp_path: Path) -> Path:
    return tmp_path / "var" / "db" / "conversation_fts.sqlite"


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- 构建与元数据


def test_build_indexes_messages_and_meta(authority_db: Path, index_db: Path) -> None:
    result = cf.build(index_db=index_db, authority_db=authority_db)
    # 6 行中 1 行空 content 被跳过；watermark = 权威库 max rowid
    assert result["new_rows"] == 5
    assert result["skipped_empty"] == 1
    assert result["indexed_messages"] == 5
    assert result["watermark_rowid"] == 6
    assert index_db.exists()

    info = cf.stats(index_db=index_db)
    assert info["indexed_messages"] == 5
    assert info["distinct_sessions"] == 2  # sess-gamma 无内容不进索引
    assert info["sessions_meta_rows"] == 3  # 会话元数据全量同步
    assert info["meta"]["watermark_rowid"] == "6"
    assert info["meta"]["schema_version"] == cf.SCHEMA_VERSION
    assert info["meta"]["tokenizer"] == cf.TOKENIZER


def test_search_without_index_raises(tmp_path: Path) -> None:
    with pytest.raises(cf.IndexNotReady):
        cf.search_content("数据分析", index_db=tmp_path / "absent.sqlite")


def test_missing_required_column_raises(tmp_path: Path, index_db: Path) -> None:
    broken = tmp_path / "broken.sqlite"
    con = sqlite3.connect(broken)
    con.executescript(
        "CREATE TABLE canonical_messages (canonical_message_id TEXT PRIMARY KEY, "
        "canonical_session_id TEXT, ordinal INTEGER, role TEXT);"
    )
    con.commit()
    con.close()
    with pytest.raises(ConversationFtsError):
        cf.build(index_db=index_db, authority_db=broken)


# ---------------------------------------------------------------- 中英文命中


def test_chinese_query_hits(authority_db: Path, index_db: Path) -> None:
    cf.build(index_db=index_db, authority_db=authority_db)

    # 4 字词：嵌入长句的子串也必须命中
    hits = cf.search_content("数据分析", index_db=index_db)
    assert len(hits) == 2
    assert {h["session_id"] for h in hits} == {"sess-alpha"}
    assert sorted(h["ordinal"] for h in hits) == [0, 1]
    assert all("[[数据分析]]" in h["snippet"] for h in hits)

    # 2 字词（trigram 会直接落空的那类查询）
    hits = cf.search_content("设置", index_db=index_db)
    assert len(hits) == 1
    assert hits[0]["session_id"] == "sess-alpha"
    assert hits[0]["ordinal"] == 2
    assert "[[设置]]" in hits[0]["snippet"]

    # 4 字词，另一会话
    hits = cf.search_content("语义压缩", index_db=index_db)
    assert len(hits) == 1
    assert hits[0]["session_id"] == "sess-beta"

    # 多词 AND
    hits = cf.search_content("报表 pivot", index_db=index_db)
    assert len(hits) == 1
    assert hits[0]["ordinal"] == 1


def test_english_query_hits(authority_db: Path, index_db: Path) -> None:
    cf.build(index_db=index_db, authority_db=authority_db)

    hits = cf.search_content("waterfall", index_db=index_db)
    assert len(hits) == 1
    assert hits[0]["session_id"] == "sess-beta"
    assert hits[0]["ordinal"] == 1
    assert "[[waterfall]]" in hits[0]["snippet"]

    # 连字符词：引号内由 tokenizer 切成 real/time 相邻短语
    hits = cf.search_content("real-time", index_db=index_db)
    assert len(hits) == 1
    assert hits[0]["ordinal"] == 1

    # 大小写不敏感（unicode61 默认折叠）
    hits = cf.search_content("Proxy", index_db=index_db)
    assert len(hits) == 1
    assert hits[0]["ordinal"] == 2


def test_search_sessions_aggregates_and_meta(authority_db: Path, index_db: Path) -> None:
    cf.build(index_db=index_db, authority_db=authority_db)
    sessions = cf.search_sessions("数据分析", index_db=index_db)
    assert len(sessions) == 1
    top = sessions[0]
    assert top["session_id"] == "sess-alpha"
    assert top["hits"] == 2
    assert top["agent"] == "claude-code"
    assert top["started_at"] == "2026-09-01T01:00:00Z"
    assert top["message_count"] == 3
    assert "[[数据分析]]" in top["snippet"]

    # 空 content 会话不进结果
    sessions = cf.search_sessions("设置", index_db=index_db)
    assert [s["session_id"] for s in sessions] == ["sess-alpha"]


# ---------------------------------------------------------------- snippet


def test_snippet_highlight_and_truncation(authority_db: Path, index_db: Path) -> None:
    cf.build(index_db=index_db, authority_db=authority_db)
    hits = cf.search_content("建模", index_db=index_db)
    assert len(hits) == 1
    snippet = hits[0]["snippet"]
    assert "[[建模]]" in snippet
    assert len(snippet) <= cf.SNIPPET_MAX_LEN + 16  # 高亮标记只占几个字符

    # 长文本：以命中为中心截断，带省略号
    long_text = "无关的前言内容。" * 50 + "这里是关键的数据分析段落。" + "无关的后续内容。" * 50
    snippet = cf.build_snippet(long_text, ["数据分析"])
    assert "[[数据分析]]" in snippet
    assert cf.SNIPPET_ELLIPSIS in snippet
    assert len(snippet) <= cf.SNIPPET_MAX_LEN + 16

    # 无命中：开头截断
    snippet = cf.build_snippet("完全没有匹配的内容", ["数据分析"])
    assert snippet.startswith("完全没有匹配")

    # 空内容
    assert cf.build_snippet(None, ["x"]) == ""
    assert cf.build_snippet("   ", ["x"]) == ""


# ---------------------------------------------------------------- 增量与重建


def test_incremental_build_only_new_rows(authority_db: Path, index_db: Path) -> None:
    first = cf.build(index_db=index_db, authority_db=authority_db)
    assert first["new_rows"] == 5

    con = sqlite3.connect(authority_db)
    con.execute(
        "INSERT INTO canonical_messages "
        "(canonical_message_id, canonical_session_id, source, ordinal, role, content, content_length, timestamp) "
        "VALUES ('cm-new', 'sess-alpha', 'agentsview', 3, 'user', '新增一条关于数据仓库的讨论', 12, '2026-09-04T04:00:00Z')"
    )
    con.commit()
    con.close()

    second = cf.build(index_db=index_db, authority_db=authority_db)
    assert second["new_rows"] == 1  # 只收新行
    assert second["skipped_empty"] == 0
    assert second["indexed_messages"] == 6
    assert second["watermark_rowid"] == 7
    assert second["full"] is False

    hits = cf.search_content("数据仓库", index_db=index_db)
    assert len(hits) == 1
    assert hits[0]["ordinal"] == 3
    # 旧内容仍在
    assert len(cf.search_content("语义压缩", index_db=index_db)) == 1


def test_full_rebuild_no_duplicates(authority_db: Path, index_db: Path) -> None:
    cf.build(index_db=index_db, authority_db=authority_db)
    con = sqlite3.connect(authority_db)
    con.execute(
        "INSERT INTO canonical_messages "
        "(canonical_message_id, canonical_session_id, source, ordinal, role, content, content_length, timestamp) "
        "VALUES ('cm-new2', 'sess-beta', 'agentsview', 1, 'user', '重建后还应只剩一条 waterfall 讨论', 16, '2026-09-05T05:00:00Z')"
    )
    con.commit()
    con.close()

    result = cf.build(index_db=index_db, authority_db=authority_db, full=True)
    assert result["full"] is True
    assert result["new_rows"] == 6  # 5 + 1 新增，空行跳过
    assert result["indexed_messages"] == 6
    info = cf.stats(index_db=index_db)
    assert info["indexed_messages"] == 6  # 无重复累积
    assert info["meta"]["full_built_at"]


# ---------------------------------------------------------------- P1-14 失效台账

# 投影写侧的同 id in-place UPDATE 不动 rowid，而增量游标只读 rowid > watermark：
# 没有失效台账时，改写后的正文永远进不了索引。台账行由投影写侧写入
# （compatibility_projection._upsert_rows），这里直接手工写入模拟该契约。


def test_content_rewrite_is_reindexed_without_rowid_change(
    authority_db: Path, index_db: Path
) -> None:
    cf.build(index_db=index_db, authority_db=authority_db)
    assert len(cf.search_content("pivot", index_db=index_db)) == 1

    con = sqlite3.connect(authority_db)
    # rowid 2 = sess-alpha 的 assistant 消息；in-place UPDATE 保持 rowid 不变
    con.execute(
        "UPDATE canonical_messages SET content = ? "
        "WHERE canonical_message_id = 'cm-2'",
        ("改写后的回答：全新关键词斑马与量子 pencil",),
    )
    con.execute(
        "INSERT INTO ce_fts_invalidate (canonical_message_id, changed_at) "
        "VALUES ('cm-2', '2026-09-26T00:00:00Z')"
    )
    con.commit()
    con.close()

    result = cf.build(index_db=index_db, authority_db=authority_db)
    assert result["full"] is False
    assert result["invalidations_refreshed"] == 1
    assert result["invalidations_pending"] == 1
    # 新正文可查（其 rowid == watermark，游标本来到不了它）
    hits = cf.search_content("斑马", index_db=index_db)
    assert len(hits) == 1
    assert hits[0]["ordinal"] == 1
    # 旧 token 不再命中；其他行不受影响
    assert len(cf.search_content("pivot", index_db=index_db)) == 0
    assert len(cf.search_content("waterfall", index_db=index_db)) == 1


def test_invalidate_ledger_replay_is_idempotent(
    authority_db: Path, index_db: Path
) -> None:
    """台账只增不清（权威库只读契约）：重放消费必须零成本跳过且无重复累积。"""
    cf.build(index_db=index_db, authority_db=authority_db)

    con = sqlite3.connect(authority_db)
    con.execute(
        "UPDATE canonical_messages SET content = ? "
        "WHERE canonical_message_id = 'cm-2'",
        ("第二次改写：唯一关键词猞猁",),
    )
    con.execute(
        "INSERT OR IGNORE INTO ce_fts_invalidate (canonical_message_id, changed_at) "
        "VALUES ('cm-2', '2026-09-26T00:00:00Z')"
    )
    con.commit()
    con.close()

    first = cf.build(index_db=index_db, authority_db=authority_db)
    assert first["invalidations_refreshed"] == 1
    second = cf.build(index_db=index_db, authority_db=authority_db)
    assert second["invalidations_refreshed"] == 0
    assert second["invalidations_pending"] == 1  # 台账仍在，校验幂等
    assert len(cf.search_content("猞猁", index_db=index_db)) == 1


def test_clear_sentinel_forces_full_rebuild_once(
    authority_db: Path, index_db: Path
) -> None:
    """clear 后重插行的 rowid 跌破 watermark：哨兵强制一次全量重建。

    全链路：build → 投影写入并 build → clear_compatibility_projection（删行 +
    写 *full-rebuild* 哨兵）→ 同 id 重插新正文（rowid 回收）→ build 必须
    full 重建且查得到新正文；再次 build 恢复增量，不重复重建。
    """
    from personal_knowledge.application.conversation.compatibility_projection import (
        CompatibilityProjectionReport,
        ProjectionFingerprint,
        clear_compatibility_projection,
        upsert_compatibility_projection,
    )

    def _report(content: str) -> CompatibilityProjectionReport:
        return CompatibilityProjectionReport(
            generation_id="gen-1",
            sessions=(),
            messages=({
                "canonical_message_id": "cm|claude|S-native-1|uuid-u1",
                "canonical_session_id": "sess-alpha",
                "source": "legacy",
                "source_message_ref": "agent-x.jsonl#L1",
                "ordinal": 9,
                "role": "user",
                "content": content,
                "content_length": len(content),
                "timestamp": "2026-09-06T06:00:00Z",
                "model": None,
                "is_system": 0,
                "is_sidechain": 0,
                "content_hash": None,
                "evidence_scope": "user",
            },),
            tools=(),
            excluded=(),
            fingerprint=ProjectionFingerprint("gen-1", 0, 1, 0, "digest"),
        )

    cf.build(index_db=index_db, authority_db=authority_db)  # watermark = 6

    con = sqlite3.connect(authority_db)
    upsert_compatibility_projection(con, _report("clear 前被索引的正文 pivots"))
    con.commit()
    mid = cf.build(index_db=index_db, authority_db=authority_db)
    assert mid["new_rows"] == 1  # watermark 推到 7

    clear_compatibility_projection(con)  # 删除该行并写哨兵
    sentinel = con.execute(
        "SELECT canonical_message_id FROM ce_fts_invalidate").fetchall()
    upsert_compatibility_projection(con, _report("clear 后重插的斑马正文"))
    con.commit()
    con.close()
    assert sentinel == [(cf.FTS_REBUILD_MARKER,)]

    rebuilt = cf.build(index_db=index_db, authority_db=authority_db)
    assert rebuilt["full"] is True
    assert rebuilt["fts_rebuild_forced"] is True
    hits = cf.search_content("斑马", index_db=index_db)
    assert len(hits) == 1  # rowid 回收后的新正文必须可见
    assert len(cf.search_content("pivots", index_db=index_db)) == 0

    again = cf.build(index_db=index_db, authority_db=authority_db)
    assert again["full"] is False
    assert again["fts_rebuild_forced"] is False  # 哨兵已消费，不重复重建
    assert again["invalidations_refreshed"] == 0


# ---------------------------------------------------------------- 转义与边界


def test_prepare_fts_query_escaping() -> None:
    # CJK 分字 + 引号转义
    assert cf.prepare_fts_query("数据分析") == '"数 据 分 析"'
    assert cf.prepare_fts_query("Python数据") == '"Python 数 据"'
    # 内部引号翻倍，运算符退化为字面量
    assert cf.prepare_fts_query('data AND "x"') == '"data" "AND" """x"""'
    # 纯标点 token 被丢弃
    assert cf.prepare_fts_query("数据分析 - ... :") == '"数 据 分 析"'
    assert cf.prepare_fts_query("") == ""
    assert cf.prepare_fts_query("   ") == ""
    assert cf.prepare_fts_query("---") == ""


def test_fts_operator_queries_do_not_crash(authority_db: Path, index_db: Path) -> None:
    cf.build(index_db=index_db, authority_db=authority_db)
    for query in [
        "data AND",
        "代理 AND 设置",
        '"unclosed',
        "NEAR(数据分析)",
        "数据分析 - OR :",
        "数*据",
        "^设置$",
        "waterfall OR NOT proxy",
        "analys?s",
        "数据:分析",
    ]:
        hits = cf.search_content(query, index_db=index_db)
        assert isinstance(hits, list)
        sessions = cf.search_sessions(query, index_db=index_db)
        assert isinstance(sessions, list)


def test_empty_queries_return_empty(authority_db: Path, index_db: Path) -> None:
    cf.build(index_db=index_db, authority_db=authority_db)
    for query in ["", "   ", "---", "...", "-"]:
        assert cf.search_content(query, index_db=index_db) == []
        assert cf.search_sessions(query, index_db=index_db) == []


def test_limit_is_clamped(authority_db: Path, index_db: Path) -> None:
    cf.build(index_db=index_db, authority_db=authority_db)
    assert cf.search_content("数据分析", limit=0, index_db=index_db) != []
    assert len(cf.search_content("数据分析", limit=-5, index_db=index_db)) >= 1
    assert len(cf.search_content("数据分析", limit=10**6, index_db=index_db)) <= cf.MAX_LIMIT


# ---------------------------------------------------------------- 权威库只读


def test_authority_db_md5_unchanged(authority_db: Path, index_db: Path) -> None:
    before = _md5(authority_db)

    cf.build(index_db=index_db, authority_db=authority_db)
    after_build = _md5(authority_db)
    assert after_build == before

    cf.build(index_db=index_db, authority_db=authority_db)  # 增量（无新行）
    cf.build(index_db=index_db, authority_db=authority_db, full=True)  # 全量重建
    for query in ["数据分析", "waterfall", "data AND", "设置"]:
        cf.search_content(query, index_db=index_db)
        cf.search_sessions(query, index_db=index_db)
    cf.stats(index_db=index_db)
    assert _md5(authority_db) == before

    # 权威库目录下不产生 WAL/journal 之类的副产物文件
    sidecars = [
        p.name
        for p in authority_db.parent.iterdir()
        if p.is_file() and p.name != authority_db.name
    ]
    assert sidecars == []
