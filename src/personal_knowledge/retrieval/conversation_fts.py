"""会话检索层：SQLite FTS5 全文索引（权威库只读，索引独立落库）。

对标 AgentsView 的 FTS5 双层检索中的 FTS 层（本项目自建，不依赖 AgentsView）：
权威库 data/canonical/agent/structured/db/agent_conversations.sqlite 的
canonical_messages 以只读方式读出，写入独立索引库
var/db/conversation_fts.sqlite；权威库全程 mode=ro + query_only，
索引构建与检索都不产生任何写路径（测试以 md5 前后校验证明）。

分词器选型（实测证据，sqlite 3.49.1，中文会话语料）：
  1. porter unicode61（AgentsView 的用法）：对中文基本无效。实测 unicode61
     把连续字母数字串当作单个 token，中文长句因此整体成词，"数据分析"查不中
     嵌在长句里的同一子串（必须整串相等才命中），2-4 字中文词检索实际不可用；
     porter 词干还原对中文同样无意义。
  2. trigram：子串匹配可用，但 MATCH 串不足 3 字符时零命中（实测 2 字/1 字
     查询全部落空），而本层要求 2-4 字中文词必须命中，直接出局。
  3. 采用 unicode61 + 索引期 CJK 分字：写入 FTS 前在相邻 CJK 字符之间、以及
     CJK 与字母数字边界插入空格，使每个汉字成为独立 token；查询侧对同一串
     做同样分字后按 FTS5 短语匹配——等价于"任意长度连续子串"检索，1 字以上
     均可命中；英文/标识符仍走 unicode61 原生分词。不引入第三方分词依赖
     （与 semantic_cards "避免引入分词依赖"的房屋风格一致）。
  代价：FTS5 内置 snippet() 依赖索引文本与原文逐 token 对齐，分字后必然错位
  （实测命中行返回 '…' 或整段无高亮），故 snippet 在 Python 侧基于原文生成。

查询转义：用户查询按空白切分，每段包双引号（内部 " 翻倍）后作为 FTS5 字符串，
FTS 运算符（AND/OR/NOT/NEAR/^/$/*/-/: 等）全部退化为字面量，语法错误与
注入面归零；多段之间为隐式 AND。

增量刷新：以权威库 canonical_messages 的 rowid 为游标，索引库 index_meta 表
记录 watermark_rowid；build() 只读 rowid > watermark 的新行，full=True 时
整体重建。canonical_messages 是只增不改的权威库（行不删不改），rowid 单调
递增，游标因此成立；若未来权威库发生重写/回退，需 full=True 重建。

P1-14 失效台账：该前提有一个写侧例外——兼容投影会按 id 对 canonical_messages
做 in-place UPDATE（P1-3 富者胜合并），rowid 不变，游标永远看不到新正文。
投影写侧因此把"内容实际变化"的消息 id 记入权威库 ce_fts_invalidate
（event_schema 定义，含 changed_at）；build() 增量路径每次先只读消费该台账：
逐 id 取权威库当前内容与索引行比对，不一致则重写该 rowid 的索引行（删除旧
token + 重插）。台账行不被 FTS 侧清空（权威库只读契约，md5 前后校验），
每次构建对其做幂等校验，内容一致即跳过；台账在两次 clear 之间缓慢累积，
代价是每次增量构建 |台账| 次主键查询。ce_fts_invalidate 中的
``*full-rebuild*`` 哨兵行（clear_compatibility_projection 写入）表示整个
存储已被清空重建——重插行的 rowid 回收跌破 watermark，增量无从弥补，build()
检测到未消费的哨兵即强制 full=True 整体重建，完成后把哨兵的 changed_at 记入
索引库 index_meta（fts_rebuild_done_at）防止重复重建。

canonical_messages 的实际列名以 PRAGMA table_info 实测为准，缺失必需列直接
报错，不做任何假设（timestamp/source 缺失时降级为空值）。

CLI:
  python -m personal_knowledge.retrieval.conversation_fts build [--full]
  python -m personal_knowledge.retrieval.conversation_fts search --query "数据分析" [--mode content|sessions] [--limit 20]
  python -m personal_knowledge.retrieval.conversation_fts stats
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# 允许 `python -m personal_knowledge.retrieval.conversation_fts` 与直接跑脚本两种方式
_SRC_DIR = Path(__file__).resolve().parents[2]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from personal_knowledge.core.project_paths import (  # noqa: E402
    AGENT_CONVERSATIONS_DB,
    VAR_DB,
)
from personal_knowledge.application.conversation.event_schema import (  # noqa: E402
    CE_FTS_INVALIDATE_TABLE,
    FTS_REBUILD_MARKER,
)

SCHEMA_VERSION = "1"
TOKENIZER = "unicode61 + cjk-separate"

MESSAGES_TABLE = "messages"
FTS_TABLE = "messages_fts"
SESSIONS_META_TABLE = "sessions_meta"
INDEX_META_TABLE = "index_meta"

# P1-14b: index_meta 里记录"已消费的全量重建哨兵 changed_at"，防止同一次
# clear 触发的哨兵让每次构建都全量重建。
REBUILD_DONE_META_KEY = "fts_rebuild_done_at"

DEFAULT_INDEX_DB = VAR_DB / "conversation_fts.sqlite"
DEFAULT_AUTHORITY_DB = AGENT_CONVERSATIONS_DB

DEFAULT_LIMIT = 20
MAX_LIMIT = 200
BATCH_ROWS = 5000
MAX_QUERY_TERMS = 12
SESSION_OVERFETCH = 3

HIGHLIGHT_OPEN = "[["
HIGHLIGHT_CLOSE = "]]"
SNIPPET_MAX_LEN = 160
SNIPPET_WINDOW = 48
SNIPPET_ELLIPSIS = "…"

# 建索引前逐列核对的必需列；缺失即失败，不猜列名
_REQUIRED_MESSAGE_COLUMNS = ("canonical_session_id", "ordinal", "role", "content")
# 有则用、无则置空的可选列
_OPTIONAL_MESSAGE_COLUMNS = ("timestamp", "source")

# CJK 主区 + 扩展 A + 兼容表 + 日文假名（会话语料以中文为主，假名防御性纳入）
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {MESSAGES_TABLE} (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    role TEXT NOT NULL,
    timestamp TEXT,
    source TEXT,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session
    ON {MESSAGES_TABLE} (session_id, ordinal);
CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(
    content,
    content = '{MESSAGES_TABLE}',
    content_rowid = 'id',
    tokenize = 'unicode61'
);
CREATE TABLE IF NOT EXISTS {SESSIONS_META_TABLE} (
    canonical_session_id TEXT PRIMARY KEY,
    primary_source TEXT,
    agent TEXT,
    started_at TEXT,
    ended_at TEXT,
    message_count INTEGER,
    user_message_count INTEGER,
    cwd TEXT,
    git_branch TEXT,
    model TEXT
);
CREATE TABLE IF NOT EXISTS {INDEX_META_TABLE} (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class ConversationFtsError(RuntimeError):
    """会话 FTS 检索层基础异常。"""


class IndexNotReady(ConversationFtsError):
    """索引库缺失或未构建。"""


# ---------------------------------------------------------------- 查询转义


def cjk_separate(text: str) -> str:
    """相邻 CJK 字符之间、CJK 与字母数字之间插入空格。

    unicode61 把连续字母数字串当作单个 token，中文长句因此不可子串检索；
    分字后每个汉字独立成 token，配合短语查询实现任意长度中文子串匹配。
    """
    if not text:
        return ""
    out: list[str] = []
    for i, ch in enumerate(text):
        if i:
            prev = text[i - 1]
            prev_cjk = bool(_CJK_RE.match(prev))
            ch_cjk = bool(_CJK_RE.match(ch))
            # CJK-CJK 与 CJK-字母数字边界需要断开；标点两侧不断（tokenizer 本就把
            # 标点当分隔符，多插空格只会稀释短语相邻性）
            if (prev_cjk and (ch_cjk or ch.isalnum())) or (ch_cjk and prev.isalnum()):
                out.append(" ")
        out.append(ch)
    return "".join(out)


def query_terms(query: str) -> list[str]:
    """原始查询按空白切分为字面量词表（snippet 用），丢弃纯标点 token。"""
    return [t for t in (query or "").split() if any(ch.isalnum() for ch in t)]


def prepare_fts_query(query: str) -> str:
    """转义为安全 FTS5 MATCH 串；无可索引内容时返回空串。

    每段先做 CJK 分字再整体包进双引号（内部 " 翻倍）：中文段成为等长短语
    （等价连续子串），运算符退化为字面量。多段之间隐式 AND。
    """
    parts = []
    for term in query_terms(query)[:MAX_QUERY_TERMS]:
        literal = cjk_separate(term).replace('"', '""')
        parts.append(f'"{literal}"')
    return " ".join(parts)


def build_snippet(
    content: str | None,
    terms: list[str],
    max_len: int = SNIPPET_MAX_LEN,
    window: int = SNIPPET_WINDOW,
) -> str:
    """基于原文生成高亮 snippet（FTS5 内置 snippet 与分字索引不对齐，见模块 docstring）。

    以首个命中位置为中心截取不超过 max_len 的窗口，窗口内全部命中以
    HIGHLIGHT_OPEN/CLOSE 包裹，越界处补省略号；无命中时退化为开头截断。
    """
    if not content:
        return ""
    text = re.sub(r"\s+", " ", str(content)).strip()
    if not text:
        return ""
    spans: list[tuple[int, int]] = []
    for term in terms:
        if not term:
            continue
        start = 0
        while True:
            i = text.find(term, start)
            if i < 0:
                break
            spans.append((i, i + len(term)))
            start = i + len(term)
    if not spans:
        head = text[:max_len]
        return head + SNIPPET_ELLIPSIS if len(text) > max_len else head
    first = min(s for s, _ in spans)
    lo = max(0, first - window)
    hi = min(len(text), lo + max_len)
    spans = sorted((max(s, lo), min(e, hi)) for s, e in spans if e > lo and s < hi)
    parts: list[str] = []
    cursor = lo
    for s, e in spans:
        if s < cursor:
            continue  # 重叠命中（如 "数" 与 "数据"）不重复高亮
        parts.append(text[cursor:s])
        parts.append(HIGHLIGHT_OPEN + text[s:e] + HIGHLIGHT_CLOSE)
        cursor = e
    parts.append(text[cursor:hi])
    snippet = "".join(parts)
    if lo > 0:
        snippet = SNIPPET_ELLIPSIS + snippet
    if hi < len(text):
        snippet = snippet + SNIPPET_ELLIPSIS
    return snippet


# ---------------------------------------------------------------- 连接与元数据


def _open_authority(authority_db: Path) -> sqlite3.Connection:
    """权威库只读连接：mode=ro + query_only 双保险，杜绝任何写路径。"""
    uri = f"file:{authority_db.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30.0)
    con.execute("PRAGMA query_only = 1")
    return con


def _open_index(index_db: Path) -> sqlite3.Connection:
    index_db.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(index_db), timeout=30.0)


def _open_index_readonly(index_db: Path) -> sqlite3.Connection:
    if not index_db.exists():
        raise IndexNotReady(f"索引库不存在: {index_db}（先运行 build）")
    uri = f"file:{index_db.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30.0)
    if not _table_exists(con, FTS_TABLE):
        con.close()
        raise IndexNotReady(f"索引库缺少 FTS 表 {FTS_TABLE}: {index_db}（先运行 build）")
    return con


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute(
        f"SELECT value FROM {INDEX_META_TABLE} WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else default


def _set_meta(con: sqlite3.Connection, key: str, value: object) -> None:
    con.execute(
        f"INSERT INTO {INDEX_META_TABLE} (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def _get_meta_int(con: sqlite3.Connection, key: str, default: int = 0) -> int:
    try:
        return int(_get_meta(con, key) or default)
    except (TypeError, ValueError):
        return default


def _schema_ready(con: sqlite3.Connection) -> bool:
    return all(
        _table_exists(con, t)
        for t in (MESSAGES_TABLE, FTS_TABLE, SESSIONS_META_TABLE, INDEX_META_TABLE)
    )


def _create_schema(con: sqlite3.Connection) -> None:
    con.executescript(_SCHEMA_SQL)


def _reset_index(con: sqlite3.Connection) -> None:
    """整体重建：外部内容表的依赖方向是 FTS -> messages，必须先删虚表。"""
    if _table_exists(con, FTS_TABLE):
        con.execute(f"DROP TABLE {FTS_TABLE}")
    for table in (MESSAGES_TABLE, SESSIONS_META_TABLE, INDEX_META_TABLE):
        if _table_exists(con, table):
            con.execute(f"DROP TABLE {table}")
    _create_schema(con)


def _clamp_limit(limit: int) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(n, MAX_LIMIT))


# ---------------------------------------------------------------- 失效台账（P1-14）


def _fts_rebuild_requested(auth: sqlite3.Connection) -> str | None:
    """全量重建哨兵的 changed_at；无哨兵（或权威库尚无台账表）返回 None。"""
    if not _table_exists(auth, CE_FTS_INVALIDATE_TABLE):
        return None
    row = auth.execute(
        f"SELECT changed_at FROM {CE_FTS_INVALIDATE_TABLE} "
        "WHERE canonical_message_id = ?",
        (FTS_REBUILD_MARKER,),
    ).fetchone()
    return row[0] if row else None


def _load_invalidate_entries(auth: sqlite3.Connection) -> list[str]:
    """台账中的待校验消息 id（不含哨兵行）；权威库无台账表视为空（旧库）。"""
    if not _table_exists(auth, CE_FTS_INVALIDATE_TABLE):
        return []
    return [
        row[0]
        for row in auth.execute(
            f"SELECT canonical_message_id FROM {CE_FTS_INVALIDATE_TABLE} "
            "WHERE canonical_message_id != ?",
            (FTS_REBUILD_MARKER,),
        )
    ]


def _refresh_indexed_message(
    auth: sqlite3.Connection,
    idx: sqlite3.Connection,
    select_cols: list[str],
    message_id: str,
) -> bool:
    """把一个失效 id 的索引行对齐到权威库当前内容；有写动作返回 True。

    权威库行已不存在（如 clear 后重插换了 rowid）时返回 False——该形态由
    全量重建兜底，单行修补无法定位旧 rowid 上的残留索引行。
    """
    row = auth.execute(
        f"SELECT {', '.join(select_cols)} FROM canonical_messages "
        "WHERE canonical_message_id = ?",
        (message_id,),
    ).fetchone()
    if row is None:
        return False
    rec = dict(zip(select_cols, row))
    rid = rec["rowid"]
    content = rec.get("content")
    should_index = bool(content and content.strip())
    have = idx.execute(
        f"SELECT content FROM {MESSAGES_TABLE} WHERE id = ?", (rid,)
    ).fetchone()
    if have is not None and should_index and have[0] == content:
        return False  # 索引已一致（台账幂等重放，例如改写后又改回同文）
    if have is not None:
        # 外部内容表（content='messages'）：必须先用 'delete' 命令显式丢掉旧
        # token（传入与当初索引一致的 cjk 分字文本），再删 messages 行；直接
        # DELETE FROM messages_fts 会让 FTS5 回读内容表取旧值，而行即将不存在。
        idx.execute(
            f"INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, content) "
            "VALUES ('delete', ?, ?)",
            (rid, cjk_separate(have[0] or "")),
        )
        idx.execute(f"DELETE FROM {MESSAGES_TABLE} WHERE id = ?", (rid,))
    if should_index:
        idx.execute(
            f"INSERT INTO {MESSAGES_TABLE} "
            "(id, session_id, ordinal, role, timestamp, source, content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                rid,
                rec["canonical_session_id"],
                rec["ordinal"],
                rec["role"],
                rec.get("timestamp"),
                rec.get("source"),
                content,
            ),
        )
        idx.execute(
            f"INSERT INTO {FTS_TABLE} (rowid, content) VALUES (?, ?)",
            (rid, cjk_separate(content)),
        )
    return True


def _consume_invalidations(
    auth: sqlite3.Connection,
    idx: sqlite3.Connection,
    select_cols: list[str],
    entries: list[str],
) -> int:
    """增量消费失效台账：逐 id 幂等校验并对齐索引行，返回实际重写条数。

    台账只增不清（权威库只读契约）：同一 id 反复出现在台账上是常态，比对
    内容一致即零成本跳过，因此消费天然幂等，不依赖 changed_at 游标。
    """
    refreshed = 0
    for message_id in entries:
        if _refresh_indexed_message(auth, idx, select_cols, message_id):
            refreshed += 1
    idx.commit()
    return refreshed


# ---------------------------------------------------------------- 构建


def _sync_sessions_meta(auth: sqlite3.Connection, idx: sqlite3.Connection) -> None:
    """把 canonical_sessions 元数据同步进索引库，使检索面自包含（不再依赖权威库）。

    列名同样以实测为准：canonical_session_id 缺失时整体跳过（检索仍可用，
    只是没有会话元数据）。
    """
    cols = {r[1] for r in auth.execute("PRAGMA table_info(canonical_sessions)")}
    wanted = [
        "canonical_session_id",
        "primary_source",
        "agent",
        "started_at",
        "ended_at",
        "message_count",
        "user_message_count",
        "cwd",
        "git_branch",
        "model",
    ]
    use = [c for c in wanted if c in cols]
    if "canonical_session_id" not in use:
        return
    updates = ", ".join(f"{c} = excluded.{c}" for c in use if c != "canonical_session_id")
    if not updates:
        return
    rows = auth.execute(f"SELECT {', '.join(use)} FROM canonical_sessions").fetchall()
    placeholders = ", ".join("?" * len(use))
    idx.executemany(
        f"INSERT INTO {SESSIONS_META_TABLE} ({', '.join(use)}) VALUES ({placeholders}) "
        f"ON CONFLICT(canonical_session_id) DO UPDATE SET {updates}",
        rows,
    )


def build(
    index_db: str | Path | None = None,
    authority_db: str | Path | None = None,
    full: bool = False,
) -> dict:
    """构建 / 增量刷新 FTS 索引，返回统计 dict。

    权威库只读打开，逐批（BATCH_ROWS）读 canonical_messages 中 rowid 大于
    watermark 的新行写入索引库；空 content 行跳过但 watermark 照常推进。
    full=True 或 schema 不匹配时先整体重建。P1-14：增量路径先只读消费
    ce_fts_invalidate 失效台账（内容被 in-place UPDATE 改写的消息 id）；
    台账中的全量重建哨兵（clear 写入）未被消费过时强制整体重建。
    """
    index_path = Path(index_db) if index_db else DEFAULT_INDEX_DB
    authority_path = Path(authority_db) if authority_db else DEFAULT_AUTHORITY_DB
    started = time.perf_counter()
    auth = _open_authority(authority_path)
    idx = _open_index(index_path)
    try:
        # P1-14b：哨兵表示存储被 clear 过（重插行 rowid 跌破 watermark），
        # 只有整体重建能保证索引一致；哨兵的 changed_at 已记入索引库 meta
        # 则视为消费过，不再重复重建。
        rebuild_marker = _fts_rebuild_requested(auth)
        rebuild_forced = (
            rebuild_marker is not None
            and _schema_ready(idx)
            and rebuild_marker != _get_meta(idx, REBUILD_DONE_META_KEY)
        )
        if rebuild_forced:
            full = True
        if (
            full
            or not _schema_ready(idx)
            or _get_meta(idx, "schema_version") != SCHEMA_VERSION
        ):
            _reset_index(idx)
            reindexed_all = True
        else:
            reindexed_all = False
        # 权威库列名以实测为准：缺必需列直接失败，不做任何假设
        cols = {r[1] for r in auth.execute("PRAGMA table_info(canonical_messages)")}
        missing = [c for c in _REQUIRED_MESSAGE_COLUMNS if c not in cols]
        if missing:
            raise ConversationFtsError(
                f"canonical_messages 缺少必需列: {missing}（实测列: {sorted(cols)}）"
            )
        select_cols = [
            "rowid",
            *_REQUIRED_MESSAGE_COLUMNS,
            *[c for c in _OPTIONAL_MESSAGE_COLUMNS if c in cols],
        ]
        select_sql = (
            f"SELECT {', '.join(select_cols)} FROM canonical_messages "
            "WHERE rowid > ? ORDER BY rowid"
        )
        watermark = _get_meta_int(idx, "watermark_rowid", 0)
        new_rows = 0
        skipped_empty = 0
        # 索引库是可重建产物，构建期牺牲崩溃恢复换速度；保持默认 DELETE 日志
        # 模式——WAL 模式下无 -shm 副产物时只读连接会 SQLITE_CANTOPEN，
        # 检索侧必须能 mode=ro 打开
        idx.execute("PRAGMA synchronous=OFF")
        cursor = auth.execute(select_sql, (watermark,))
        last_rowid = watermark
        while True:
            batch = cursor.fetchmany(BATCH_ROWS)
            if not batch:
                break
            for row in batch:
                rec = dict(zip(select_cols, row))
                content = rec.get("content")
                if not content or not content.strip():
                    skipped_empty += 1
                else:
                    idx.execute(
                        f"INSERT INTO {MESSAGES_TABLE} "
                        "(id, session_id, ordinal, role, timestamp, source, content) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            rec["rowid"],
                            rec["canonical_session_id"],
                            rec["ordinal"],
                            rec["role"],
                            rec.get("timestamp"),
                            rec.get("source"),
                            content,
                        ),
                    )
                    idx.execute(
                        f"INSERT INTO {FTS_TABLE} (rowid, content) VALUES (?, ?)",
                        (rec["rowid"], cjk_separate(content)),
                    )
                    new_rows += 1
                last_rowid = rec["rowid"]
            idx.commit()
        # P1-14：全量路径（含哨兵强制）已重读所有行，把哨兵记为已消费；
        # 否则先增量消费失效台账，再同步会话元数据。
        invalidate_entries = _load_invalidate_entries(auth)
        invalidated_refreshed = 0
        if full or reindexed_all:
            if rebuild_marker is not None:
                _set_meta(idx, REBUILD_DONE_META_KEY, rebuild_marker)
        else:
            invalidated_refreshed = _consume_invalidations(
                auth, idx, select_cols, invalidate_entries
            )
        _sync_sessions_meta(auth, idx)
        total = idx.execute(f"SELECT COUNT(*) FROM {MESSAGES_TABLE}").fetchone()[0]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _set_meta(idx, "watermark_rowid", last_rowid)
        _set_meta(idx, "indexed_messages", total)
        _set_meta(idx, "last_build_new_rows", new_rows)
        _set_meta(idx, "last_build_skipped_empty", skipped_empty)
        _set_meta(idx, "last_build_invalidated", invalidated_refreshed)
        _set_meta(idx, "last_build_at", now)
        if full:
            _set_meta(idx, "full_built_at", now)
        _set_meta(idx, "schema_version", SCHEMA_VERSION)
        _set_meta(idx, "tokenizer", TOKENIZER)
        _set_meta(idx, "authority_db", str(authority_path))
        idx.commit()
        elapsed = time.perf_counter() - started
        return {
            "index_db": str(index_path),
            "authority_db": str(authority_path),
            "full": bool(full),
            "new_rows": new_rows,
            "skipped_empty": skipped_empty,
            "invalidations_pending": len(invalidate_entries),
            "invalidations_refreshed": invalidated_refreshed,
            "fts_rebuild_forced": bool(rebuild_forced),
            "indexed_messages": total,
            "watermark_rowid": last_rowid,
            "elapsed_seconds": round(elapsed, 3),
            "built_at": now,
        }
    finally:
        auth.close()
        idx.close()


# ---------------------------------------------------------------- 检索

_CONTENT_HITS_SQL = (
    f"SELECT m.id, m.session_id, m.ordinal, m.role, m.timestamp, m.source, "
    f"m.content, bm25({FTS_TABLE}) AS score "
    f"FROM {FTS_TABLE} f JOIN {MESSAGES_TABLE} m ON m.id = f.rowid "
    f"WHERE {FTS_TABLE} MATCH ? {{extra}} ORDER BY score LIMIT ?"
)


def search_content(
    query: str,
    limit: int = DEFAULT_LIMIT,
    index_db: str | Path | None = None,
) -> list[dict]:
    """消息级检索：返回命中消息（bm25 排序，附原文高亮 snippet）。

    score 为 -bm25（越大越相关）；查询串经 prepare_fts_query 转义。
    """
    limit = _clamp_limit(limit)
    match = prepare_fts_query(query)
    if not match:
        return []
    con = _open_index_readonly(Path(index_db) if index_db else DEFAULT_INDEX_DB)
    try:
        rows = con.execute(
            _CONTENT_HITS_SQL.format(extra=""), (match, limit)
        ).fetchall()
    finally:
        con.close()
    terms = query_terms(query)
    return [
        {
            "message_id": rid,
            "session_id": session_id,
            "ordinal": ordinal,
            "role": role,
            "timestamp": timestamp or "",
            "source": source or "",
            "score": round(-float(score), 6),
            "snippet": build_snippet(content, terms),
        }
        for rid, session_id, ordinal, role, timestamp, source, content, score in rows
    ]


def search_sessions(
    query: str,
    limit: int = DEFAULT_LIMIT,
    index_db: str | Path | None = None,
) -> list[dict]:
    """会话级检索：命中消息归并到会话，按命中数降序、最佳 bm25 升序排列。

    排序为有界启发式：SQL 侧精确 COUNT 分组（过量取 SESSION_OVERFETCH 倍），
    再对每个候选会话取最佳命中消息，Python 侧用其 bm25 打破 hits 并列。
    每条结果附最佳命中消息的 snippet 与 canonical_sessions 元数据。
    """
    limit = _clamp_limit(limit)
    match = prepare_fts_query(query)
    if not match:
        return []
    con = _open_index_readonly(Path(index_db) if index_db else DEFAULT_INDEX_DB)
    try:
        grouped = con.execute(
            f"SELECT m.session_id, COUNT(*) AS hits "
            f"FROM {FTS_TABLE} f JOIN {MESSAGES_TABLE} m ON m.id = f.rowid "
            f"WHERE {FTS_TABLE} MATCH ? "
            f"GROUP BY m.session_id ORDER BY hits DESC LIMIT ?",
            (match, min(limit * SESSION_OVERFETCH, MAX_LIMIT * SESSION_OVERFETCH)),
        ).fetchall()
        terms = query_terms(query)
        results = []
        for session_id, hits in grouped:
            best = con.execute(
                _CONTENT_HITS_SQL.format(extra="AND m.session_id = ?"),
                (match, session_id, 1),
            ).fetchone()
            if best is None:
                continue
            rid, _sid, ordinal, role, timestamp, _source, content, score = best
            meta = con.execute(
                f"SELECT primary_source, agent, started_at, ended_at, message_count, "
                f"cwd, git_branch FROM {SESSIONS_META_TABLE} "
                "WHERE canonical_session_id = ?",
                (session_id,),
            ).fetchone()
            results.append(
                {
                    "session_id": session_id,
                    "hits": hits,
                    "best_score": round(-float(score), 6),
                    "best_message_id": rid,
                    "best_ordinal": ordinal,
                    "best_role": role,
                    "best_timestamp": timestamp or "",
                    "primary_source": meta[0] if meta else "",
                    "agent": meta[1] if meta else "",
                    "started_at": meta[2] if meta else "",
                    "ended_at": meta[3] if meta else "",
                    "message_count": meta[4] if meta else None,
                    "cwd": meta[5] if meta else "",
                    "git_branch": meta[6] if meta else "",
                    "snippet": build_snippet(content, terms),
                }
            )
        results.sort(key=lambda r: (-r["hits"], -r["best_score"]))
        return results[:limit]
    finally:
        con.close()


def stats(index_db: str | Path | None = None) -> dict:
    """索引库状态摘要（只读）。"""
    index_path = Path(index_db) if index_db else DEFAULT_INDEX_DB
    con = _open_index_readonly(index_path)
    try:
        meta = {
            key: value
            for key, value in con.execute(
                f"SELECT key, value FROM {INDEX_META_TABLE}"
            )
        }
        indexed = con.execute(f"SELECT COUNT(*) FROM {MESSAGES_TABLE}").fetchone()[0]
        sessions = con.execute(
            f"SELECT COUNT(DISTINCT session_id) FROM {MESSAGES_TABLE}"
        ).fetchone()[0]
        sessions_meta = con.execute(
            f"SELECT COUNT(*) FROM {SESSIONS_META_TABLE}"
        ).fetchone()[0]
        return {
            "index_db": str(index_path),
            "size_bytes": index_path.stat().st_size,
            "indexed_messages": indexed,
            "distinct_sessions": sessions,
            "sessions_meta_rows": sessions_meta,
            "meta": meta,
        }
    finally:
        con.close()


# ---------------------------------------------------------------- CLI


def _print_content_results(results: list[dict]) -> None:
    for i, r in enumerate(results, 1):
        print(
            f"#{i} [{r['score']:.6f}] session={r['session_id']} "
            f"ordinal={r['ordinal']} role={r['role']} ts={r['timestamp']}"
        )
        print(f"    {r['snippet']}")


def _print_session_results(results: list[dict]) -> None:
    for i, r in enumerate(results, 1):
        print(
            f"#{i} hits={r['hits']} session={r['session_id']} "
            f"agent={r['agent'] or '-'} started={r['started_at'] or '-'} "
            f"messages={r['message_count'] if r['message_count'] is not None else '-'}"
        )
        print(f"    {r['snippet']}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="personal_knowledge.retrieval.conversation_fts",
        description="会话 FTS5 检索层（权威库只读，索引独立落库）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="构建 / 增量刷新 FTS 索引")
    p_build.add_argument("--full", action="store_true", help="整体重建（默认增量）")
    p_build.add_argument("--index-db", default=None, help="索引库路径")
    p_build.add_argument("--authority-db", default=None, help="权威库路径（只读）")

    p_search = sub.add_parser("search", help="检索会话 / 消息")
    p_search.add_argument("--query", required=True, help="查询串（自动转义 FTS 运算符）")
    p_search.add_argument(
        "--mode", choices=("content", "sessions"), default="content", help="检索粒度"
    )
    p_search.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p_search.add_argument("--index-db", default=None, help="索引库路径")

    p_stats = sub.add_parser("stats", help="索引库状态")
    p_stats.add_argument("--index-db", default=None, help="索引库路径")

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build(
                index_db=args.index_db,
                authority_db=args.authority_db,
                full=args.full,
            )
            print(
                f"build {'full' if result['full'] else 'incremental'}: "
                f"new_rows={result['new_rows']} skipped_empty={result['skipped_empty']} "
                f"invalidated={result['invalidations_refreshed']}"
                f"/{result['invalidations_pending']} "
                f"indexed_messages={result['indexed_messages']} "
                f"watermark_rowid={result['watermark_rowid']} "
                f"elapsed={result['elapsed_seconds']}s"
            )
            if result["fts_rebuild_forced"]:
                print("fts rebuild forced by clear sentinel (ce_fts_invalidate)")
            print(f"index_db={result['index_db']}")
        elif args.command == "search":
            if args.mode == "content":
                results = search_content(
                    args.query, limit=args.limit, index_db=args.index_db
                )
                _print_content_results(results)
            else:
                results = search_sessions(
                    args.query, limit=args.limit, index_db=args.index_db
                )
                _print_session_results(results)
            print(f"共 {len(results)} 条结果")
        else:
            info = stats(index_db=args.index_db)
            print(f"index_db={info['index_db']}")
            print(f"size_bytes={info['size_bytes']}")
            print(f"indexed_messages={info['indexed_messages']}")
            print(f"distinct_sessions={info['distinct_sessions']}")
            print(f"sessions_meta_rows={info['sessions_meta_rows']}")
            for key in sorted(info["meta"]):
                print(f"meta.{key}={info['meta'][key]}")
    except IndexNotReady as exc:
        print(f"索引未就绪: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
