"""Phase 41 Plan 02 Task 6：D-03 确认信号检测（修饰非硬 gate）。

检测 assistant 锚消息之后的第一条 user 消息是否含采纳/纠正信号，
结果只做 confidence 修饰（adopted +0.05 / corrected -0.2，封顶封底），
不做任何丢弃/skip。corrected 行是未来 lifecycle supersede 候选，
自动路由属 deferred（见 docs/runbooks/ku-incremental.md §3F）。

词表集中一处（ADOPTION_PATTERNS / CORRECTION_PATTERNS）便于后续调；
同条消息双命中时纠正优先（保守）。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

# 采纳信号（中英双语初版）
ADOPTION_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"解决了",
        r"可以了",
        r"就这样",
        r"谢谢",
        r"多谢",
        r"管用",
        r"生效了",
        r"\bworks\b",
        r"\bworked\b",
        r"\bthanks\b",
        r"\bthank you\b",
        r"\bperfect\b",
        r"\bgreat\b",
    )
]

# 纠正信号（中英双语初版）
CORRECTION_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"不对",
        r"错了",
        r"应该是",
        r"其实不是",
        r"并非如此",
        r"搞错",
        r"\bno,",
        r"\bwrong\b",
        r"\bincorrect\b",
        r"\bactually\b",
    )
]


def classify_confirmation_text(text: str) -> str:
    """对后续 user 消息文本分类：corrected / adopted / none（双命中纠正优先）。"""
    if not text:
        return "none"
    if any(p.search(text) for p in CORRECTION_PATTERNS):
        return "corrected"
    if any(p.search(text) for p in ADOPTION_PATTERNS):
        return "adopted"
    return "none"


def detect_confirmation_signal(
    canonical_db: Path,
    *,
    session_id: str,
    anchor_message_ref: str,
    con: sqlite3.Connection | None = None,
) -> str:
    """检测锚 assistant 消息的确认信号。返回 "adopted" / "corrected" / "none"。

    取同 session、时序上紧随锚消息之后的第一条 role='user' 消息；
    无后续 user 消息 → "none"。
    con 可传入已打开的 canonical 连接（row_factory=Row 或默认均可）。
    """
    if not session_id or not anchor_message_ref:
        return "none"

    own_con = con is None
    if own_con:
        con = sqlite3.connect(f"file:{Path(canonical_db).as_posix()}?mode=ro", uri=True)
    try:
        anchor = con.execute(
            "SELECT ordinal FROM canonical_messages WHERE canonical_message_id=?",
            (anchor_message_ref,),
        ).fetchone()
        if not anchor or anchor[0] is None:
            return "none"
        nxt = con.execute(
            "SELECT content FROM canonical_messages "
            "WHERE canonical_session_id=? AND role='user' AND ordinal > ? "
            "AND content IS NOT NULL "
            "ORDER BY ordinal ASC LIMIT 1",
            (session_id, anchor[0]),
        ).fetchone()
        if not nxt or not nxt[0]:
            return "none"
        return classify_confirmation_text(nxt[0])
    finally:
        if own_con:
            con.close()
