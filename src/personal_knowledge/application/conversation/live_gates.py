# -*- coding: utf-8 -*-
"""P1-12: live-sync 数据质量门禁（硬门 fail-closed + 软门 quarantine）。

移植自 :mod:`authority_ingest`（权威库直写路径）的六项硬门 + 软门隔离，落到
live_sync 的增量 apply 路径——在那之前 live 是绕过全部数据质量门禁的后门。

与 authority_ingest 的边界差异（为什么不直接 import 它）：
  * authority_ingest 的 secret 模式清单实际定义在
    ``build_agentsview_normalized.SECRET_RULES``（该模块 import 时会做
    sys.path 注入并拉起 AgentView 摄入链路，不适合进 live 热路径），本模块
    按同一清单镜像（去掉了 email PII 规则，理由见 ``SECRET_RULES`` 注释）；
  * authority_ingest 的时间戳合法性判定（``_bad_timestamp`` / ``_parse_ts``）
    是三条正则 + 一个 ``fromisoformat``，在本模块按同一语义重写（十几行，
    为它引入整条摄入编排器的 import 依赖得不偿失）。

门禁清单与判定规则
------------------

硬门（任一不过 → :class:`LiveGateError`，整个 apply 中止，权威库零写入；
在 ``BEGIN IMMEDIATE`` 之前跑，只看本批次 prepared 槽位的适配结果）：

* ``secret_content_leak``  — 批次事件 content 命中 credential 模式。
* ``temporal_inversion``   — 同一会话 ``ended_at < started_at``（两侧都可
  解析才比较；按时间值比较，不做字符串比较）。
* ``event_time_out_of_range`` — 事件 ``occurred_at`` 落在
  ``[started_at - 容差, ended_at + 容差]`` 之外。容差
  ``EVENT_TIME_TOLERANCE_SECONDS = 300``：容忍 5 分钟时钟漂移（NTP 未同步
  的本机导出、跨时区换算误差），避免把正常会话误杀成硬门。
* ``duplicate_source_session`` — 本批次内同一 ``(family, native_session_id)``
  被两个不同槽位（mirror_path）emit，且两者都是"完整不同的会话"。判定标准：
  两份拷贝都有事件、各自的事件 content 集合都非空、且两个集合**完全不相交**
  （uuid 碰撞/两个真实会话共享 native id）。有任何内容交集即视为"同一会话被
  收集了两份"——那是投影塌缩的正常输入（cross-slot duplicate），不拦。

软门（quarantine，写隔离表、不阻断批次；批次其余照常）：

* 时间戳缺陷 — 会话 ``started_at`` / ``ended_at``、事件 ``occurred_at`` 为
  null/''/'0'/epoch 秒/epoch 毫秒/不可解析（``bad_timestamp`` 的六种缺陷，
  与 authority_ingest 同语义）。ended_at 为 null 不算缺陷（进行中的会话
  合法没有结束时间）。
* ``empty_session`` — 触达会话在当前计算（stale 过滤后）里 0 条消息事件。
  选择：软处理（隔离 + 从本轮投影集合剔除，不写/不刷新它的 canonical 行），
  不硬拦——只有工具事件的会话是合法输入，硬拦会把正常批次炸掉。已在库里的
  canonical 行按既定 P4 边界保留（投影 upsert 从不删除）。
* ``message_count_mismatch`` — 投影 upsert 之后，``canonical_sessions``
  的 ``message_count`` 与本轮投影输入里同 canonical id 各 ce 会话的实际
  消息事件数（stale 过滤后取 max，与投影 merge 规则一致）不一致。P0-1/P1-3
  之后两者应恒等，此门纯作守卫存在，触发即说明投影或 staleness 有回归。

性能边界：secret 扫描只对**本批次新增/变更**（prepared）的 content 做，
不重扫全库——prepared 之外的槽位走指纹快路径，本来就不被本轮改写。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from personal_knowledge.application.conversation.compatibility_projection import (
    MESSAGE_KINDS,
)
from personal_knowledge.application.conversation.uniform_id_migration import (
    make_session_id,
)

# ---- 时间戳合法性（镜像 authority_ingest 的判定语义） -----------------------

# 合法时间戳：ISO-8601（允许日期-only 与空格分隔），其余一律视为可疑。
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?([+-]\d{2}:?\d{2}|Z)?$"
)
# 10 位整数 = epoch 秒（qoder 家族实测把裸秒存进了 started_at）。
_EPOCH_SECONDS_RE = re.compile(r"^\d{10}$")
# 13 位整数 = epoch 毫秒。
_EPOCH_MILLIS_RE = re.compile(r"^\d{13}$")

# ---- secret 模式清单（镜像 build_agentsview_normalized.SECRET_RULES） -------
#
# 刻意去掉其中的 ``local-email-pii``：邮箱是 PII 不是 credential，normalized
# 构建器对它的处置是"正文不落库"，而 live 库是原文收集库——把邮箱当硬门会把
# 每一个正常对话批次都拦死。PII 处置属于内容发布策略，不属入库门禁。
SECRET_RULES: dict[str, re.Pattern[str]] = {
    # OpenAI: sk-... / sk-proj-...
    "openai-key": re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    # Google API key: AIza...
    "google-api-key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    # 通用 Bearer token
    "bearer-token": re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}"),
    # GitHub PAT
    "github-pat": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    # AWS access key
    "aws-key": re.compile(r"AKIA[0-9A-Z]{16}"),
    # 私钥头
    "private-key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # Slack token
    "slack-token": re.compile(r"xox[bpars]-[A-Za-z0-9\-]{10,}"),
}

# 事件时间窗容差：容忍 5 分钟时钟漂移（见模块 docstring）。
EVENT_TIME_TOLERANCE_SECONDS = 300

# detail 里最多列出的越界事件数（防止单会话刷爆 detail JSON）。
_DETAIL_EVENT_CAP = 5

# SQLite 参数上限分块（与 live_sync._chunks 同一常量与同一语义；本地复制一份
# 以免 live_gates ↔ live_sync 循环 import——live_sync 在模块顶层 import 本模块）。
_PARAM_CHUNK = 400


def _chunks(values: list, size: int = _PARAM_CHUNK):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def bad_timestamp(value: object) -> str | None:
    """返回时间戳缺陷描述（六种）；合法返回 None。ended_at=None 是合法的。"""
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return "empty"
    if text == "0":
        return "zero"
    if _ISO_RE.match(text):
        return None
    if _EPOCH_SECONDS_RE.match(text):
        return "epoch_seconds"
    if _EPOCH_MILLIS_RE.match(text):
        return "epoch_millis"
    return "unparseable"


def _parse_ts(text: str) -> datetime:
    """把 ISO-8601（含 Z 后缀与小数秒）解析为可比较的 datetime。"""
    cleaned = text.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        # 日期-only 或异常精度：退回日期部分，保证同一会话内可比。
        return datetime.fromisoformat(cleaned[:10])


def scan_content_for_secrets(text: str | None) -> list[str]:
    """对文本跑 credential 模式，返回命中的规则名（去重）。"""
    if not text:
        return []
    return [name for name, pattern in SECRET_RULES.items() if pattern.search(text)]


# ------------------------------------------------------------------ 发现记录


@dataclass(frozen=True)
class GateFinding:
    """一条门禁发现。severity: hard=拦批；soft=写隔离表、批次放行。

    ``detail`` 携带定位信息（事件 id、缺陷种类、命中的规则名），**永不携带
    命中的正文片段**——门禁报告本身不能成为 secret 的二次泄漏点。
    """

    code: str
    severity: str
    family: str
    session_id: str
    mirror_path: str
    detail: str

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "family": self.family,
            "session_id": self.session_id,
            "mirror_path": self.mirror_path,
            "detail": self.detail,
        }


class LiveGateError(RuntimeError):
    """硬门失败：整个 apply 作废，权威库零写入。"""

    def __init__(self, findings: list[GateFinding]) -> None:
        self.findings = findings
        codes = sorted({f.code for f in findings})
        super().__init__(
            f"live-sync hard gate blocked: {len(findings)} finding(s), "
            f"codes={codes}; nothing was written"
        )


# ------------------------------------------------------------------ 硬门


def run_hard_gates(prepared: list[dict]) -> list[GateFinding]:
    """对本批次 prepared 槽位跑全部硬门，返回全部发现（不是首个即停）。

    ``prepared`` 是 ``live_sync.live_sync_once`` 的捕获/适配产物列表（dict，
    键 ``mirror_path`` / ``family`` / ``artifact`` / ``result``）。调用方拿到
    非空返回即应抛 :class:`LiveGateError`，且必须在 ``BEGIN IMMEDIATE`` 之前。
    """
    findings: list[GateFinding] = []
    findings.extend(_gate_secret_leak(prepared))
    findings.extend(_gate_temporal_order(prepared))
    findings.extend(_gate_duplicate_native_session(prepared))
    return findings


def _gate_secret_leak(prepared: list[dict]) -> list[GateFinding]:
    findings: list[GateFinding] = []
    for item in prepared:
        result = item["result"]
        hits: dict[str, list[str]] = {}  # session_id -> rule names (dedup)
        event_ids: dict[str, list[str]] = {}
        for event in result.events:
            if event.content is None:
                continue
            rules = scan_content_for_secrets(event.content)
            if not rules:
                continue
            sid = str(event.session_id)
            hits.setdefault(sid, [])
            event_ids.setdefault(sid, [])
            for rule in rules:
                if rule not in hits[sid]:
                    hits[sid].append(rule)
            if len(event_ids[sid]) < _DETAIL_EVENT_CAP:
                event_ids[sid].append(str(event.event_id))
        for sid in sorted(hits):
            findings.append(GateFinding(
                "secret_content_leak", "hard", result.family, sid,
                item["mirror_path"],
                f"credential pattern(s) {hits[sid]} in event content "
                f"(events: {event_ids[sid]}); rule names only, match text "
                f"is deliberately not reproduced here",
            ))
    return findings


def _gate_temporal_order(prepared: list[dict]) -> list[GateFinding]:
    tolerance = timedelta(seconds=EVENT_TIME_TOLERANCE_SECONDS)
    findings: list[GateFinding] = []
    for item in prepared:
        result = item["result"]
        bounds = {
            str(s.session_id): (s.started_at, s.ended_at) for s in result.sessions
        }
        for session_id, (started_at, ended_at) in sorted(bounds.items()):
            # started_at > ended_at：两侧都可解析才比较——必须按时间值比较，
            # 字符串比较会把 "…37.62Z" 与 "…37.621Z"（毫秒差）误判为倒挂。
            if (
                started_at is not None
                and ended_at is not None
                and bad_timestamp(started_at) is None
                and bad_timestamp(ended_at) is None
                and _parse_ts(str(ended_at)) < _parse_ts(str(started_at))
            ):
                findings.append(GateFinding(
                    "temporal_inversion", "hard", result.family,
                    str(session_id), item["mirror_path"],
                    f"ended_at {ended_at!r} < started_at {started_at!r}",
                ))

        # 事件落在会话时间窗之外（带容差）。started/ended 任一不可解析则
        # 无从比较，跳过（那种缺陷走软门 quarantine）。
        windows: dict[str, tuple[datetime, datetime]] = {}
        for session_id, (started_at, ended_at) in bounds.items():
            if (
                started_at is None
                or ended_at is None
                or bad_timestamp(started_at) is not None
                or bad_timestamp(ended_at) is not None
            ):
                continue
            windows[str(session_id)] = (
                _parse_ts(str(started_at)) - tolerance,
                _parse_ts(str(ended_at)) + tolerance,
            )
        out_of_range: dict[str, list[str]] = {}
        for event in result.events:
            window = windows.get(str(event.session_id))
            if window is None or event.occurred_at is None:
                continue
            if bad_timestamp(event.occurred_at) is not None:
                continue  # 不可解析：软门领域
            when = _parse_ts(str(event.occurred_at))
            if when < window[0] or when > window[1]:
                listed = out_of_range.setdefault(str(event.session_id), [])
                if len(listed) < _DETAIL_EVENT_CAP:
                    listed.append(
                        f"{event.event_id}@{event.occurred_at}"
                    )
        for session_id in sorted(out_of_range):
            findings.append(GateFinding(
                "event_time_out_of_range", "hard", result.family, session_id,
                item["mirror_path"],
                f"event occurred_at outside [{started_at!r}, {ended_at!r}] "
                f"+/- {EVENT_TIME_TOLERANCE_SECONDS}s drift tolerance "
                f"(events: {out_of_range[session_id]})",
            ))
    return findings


def _gate_duplicate_native_session(prepared: list[dict]) -> list[GateFinding]:
    """拦截"同 (family, native_session_id) 的两个完整不同会话"的极端冲突。

    判定标准（写死在这里，见模块 docstring）：两份拷贝来自**不同槽位**
    （mirror_path 不同）、都有事件、各自的事件 content 集合都非空、且两个
    集合**完全不相交**。有任何交集都判为同一会话的多份收集（截断导出、文件
    副本、子代理 artifact），交给投影塌缩——那不是门禁该拦的。
    """
    seen: dict[tuple[str, str], list[tuple[str, frozenset[str]]]] = {}
    for item in prepared:
        result = item["result"]
        for session in result.sessions:
            native = (session.native_session_id or "").strip()
            if not native:
                continue  # 无 native id 的会话无从跨槽位判定
            contents = frozenset(
                event.content
                for event in result.events
                if event.session_id == session.session_id and event.content
            )
            seen.setdefault((result.family, native), []).append(
                (item["mirror_path"], contents)
            )

    findings: list[GateFinding] = []
    for (family, native), copies in sorted(seen.items()):
        if len(copies) < 2:
            continue
        for i in range(len(copies)):
            for j in range(i + 1, len(copies)):
                path_a, contents_a = copies[i]
                path_b, contents_b = copies[j]
                if path_a == path_b:
                    continue
                if not contents_a or not contents_b:
                    continue  # 一侧无 content：无法证明是"不同会话"，不拦
                if contents_a & contents_b:
                    continue  # 有交集：同一会话的多份收集，投影塌缩处理
                findings.append(GateFinding(
                    "duplicate_source_session", "hard", family, native,
                    path_b,
                    f"native session {native!r} emitted by two different "
                    f"slots ({path_a}, {path_b}) with completely disjoint "
                    f"event content sets ({len(contents_a)} vs "
                    f"{len(contents_b)} distinct bodies) — a native id "
                    f"collision between two genuinely different sessions",
                ))
    return findings


# ------------------------------------------------------------------ 软门（批次内可判部分）


def collect_timestamp_quarantine_findings(prepared: list[dict]) -> list[GateFinding]:
    """时间戳缺陷软门：null/''/'0'/epoch 秒/毫秒/不可解析 → 隔离，不阻断。

    ``ended_at=None`` 不算缺陷（进行中的会话合法地没有结束时间）；
    ``started_at=None`` / 事件 ``occurred_at=None`` 算（落不进任何时间窗）。
    每个会话每种缺陷只记一条（detail 带计数与样例），避免刷表。
    """
    findings: list[GateFinding] = []
    for item in prepared:
        result = item["result"]
        per_session: dict[str, dict[str, dict]] = {}

        def _note(session_id: str, code: str, sample: str) -> None:
            entry = per_session.setdefault(session_id, {}).setdefault(
                code, {"count": 0, "samples": []}
            )
            entry["count"] += 1
            if len(entry["samples"]) < _DETAIL_EVENT_CAP:
                entry["samples"].append(sample)

        for session in result.sessions:
            sid = str(session.session_id)
            started_bad = bad_timestamp(session.started_at)
            if started_bad is not None:
                _note(sid, f"started_at_{started_bad}", repr(session.started_at))
            ended_bad = bad_timestamp(session.ended_at)
            if ended_bad is not None:
                _note(sid, f"ended_at_{ended_bad}", repr(session.ended_at))
        for event in result.events:
            bad = bad_timestamp(event.occurred_at)
            if bad is not None:
                _note(
                    str(event.session_id), f"occurred_at_{bad}",
                    f"{event.event_id}={event.occurred_at!r}",
                )

        for sid in sorted(per_session):
            for code in sorted(per_session[sid]):
                entry = per_session[sid][code]
                findings.append(GateFinding(
                    code, "soft", result.family, sid, item["mirror_path"],
                    json.dumps(entry, sort_keys=True),
                ))
    return findings


# ------------------------------------------------------------------ 软门（需查库部分）


_MESSAGE_KIND_VALUES = tuple(kind.value for kind in MESSAGE_KINDS)


def _message_counts(
    con: sqlite3.Connection, generation_id: str, session_ids: set[str]
) -> dict[str, int]:
    """``{session_id: 当前消息事件数}``（stale 过滤，仅消息 kind）。"""
    counts: dict[str, int] = {}
    marks = ",".join("?" * len(_MESSAGE_KIND_VALUES))
    for chunk in _chunks(sorted(session_ids)):
        id_marks = ",".join("?" * len(chunk))
        for row in con.execute(
            "SELECT session_id, COUNT(*) FROM ce_events "
            f"WHERE generation_id=? AND stale_at IS NULL "
            f"AND kind IN ({marks}) AND session_id IN ({id_marks}) "
            "GROUP BY session_id",
            (generation_id, *_MESSAGE_KIND_VALUES, *chunk),
        ):
            counts[str(row[0])] = int(row[1])
    return counts


def detect_empty_sessions(
    con: sqlite3.Connection, generation_id: str, session_ids: set[str]
) -> tuple[set[str], list[GateFinding]]:
    """触达会话里当前 0 条消息事件者：返回（空会话 id 集, 隔离发现）。

    判定基于 stale 过滤后的消息事件数（P0-1 语义）：源把消息删光后会话
    计数为 0，即视为本轮投影不该再喂的空会话。
    """
    if not session_ids:
        return set(), []
    counts = _message_counts(con, generation_id, session_ids)
    empty = {sid for sid in session_ids if counts.get(sid, 0) == 0}
    if not empty:
        return set(), []
    families = {
        str(row[0]): str(row[1])
        for row in con.execute(
            "SELECT session_id, family FROM ce_sessions WHERE generation_id=?",
            (generation_id,),
        )
    }
    findings = [
        GateFinding(
            "empty_session", "soft",
            families.get(sid, ""), sid, "-",
            "0 current message events after staleness filtering; excluded "
            "from this round's projection",
        )
        for sid in sorted(empty)
    ]
    return empty, findings


def detect_message_count_mismatch(
    con: sqlite3.Connection, generation_id: str, session_ids: set[str]
) -> list[GateFinding]:
    """投影后守卫：canonical_sessions.message_count 应与实际消息行数恒等。

    比较基准与投影 merge 规则严格一致：对每个触达会话的 canonical id，
    期望值 = 本轮投影输入（``projectable``）里同 canonical id 各 ce 会话
    的当前消息事件数的 **max**（``_merge_session_pair`` 的计数规则）。
    只查触达会话，代价与本轮规模成正比。
    """
    if not session_ids:
        return []
    keys = {
        str(row[0]): make_session_id(
            str(row[1]).strip().lower(),
            (str(row[2]).strip() if row[2] else "") or f"ce:{row[0]}",
        )
        for row in con.execute(
            "SELECT session_id, family, native_session_id FROM ce_sessions "
            "WHERE generation_id=? AND stale_at IS NULL",
            (generation_id,),
        )
        if row[0] in session_ids
    }
    counts = _message_counts(con, generation_id, session_ids)
    expected: dict[str, int] = {}
    for sid, canon in keys.items():
        value = counts.get(sid, 0)
        expected[canon] = max(expected.get(canon, 0), value)

    findings: list[GateFinding] = []
    for canon in sorted(set(keys.values())):
        stored_row = con.execute(
            "SELECT message_count FROM canonical_sessions "
            "WHERE canonical_session_id=?",
            (canon,),
        ).fetchone()
        if stored_row is None:
            continue  # 未投影（如被 empty_session 剔除）：无行可比
        stored = int(stored_row[0] or 0)
        if stored == expected.get(canon, 0):
            continue
        sid = next(s for s, c in keys.items() if c == canon)
        families = con.execute(
            "SELECT family FROM ce_sessions WHERE generation_id=? AND session_id=?",
            (generation_id, sid),
        ).fetchone()
        findings.append(GateFinding(
            "message_count_mismatch", "soft",
            str(families[0]) if families else "", sid, "-",
            f"canonical_sessions.message_count={stored} but max current "
            f"message-event count across copies is {expected.get(canon, 0)} "
            f"— projection/staleness regression guard fired",
        ))
    return findings


# ------------------------------------------------------------------ 隔离表写入


def write_quarantine(
    con: sqlite3.Connection,
    generation_id: str,
    findings: list[GateFinding],
    covered_session_ids: set[str],
) -> int:
    """把软门发现写进 ``ce_ingest_quarantine``（apply 事务内调用）。

    自查愈语义（对齐 authority_ingest.write_quarantine 的"只留最近一轮"）：
    本批次覆盖（emit）的会话，先删其全部历史隔离行，再写入本轮发现——某个
    会话的缺陷上游修好、下一轮被重新收集后，旧隔离行不会永久残留。本轮未
    触达的会话的历史隔离行原样保留（其数据仍在库内，隔离仍是有效证据）。
    返回本轮被隔离的去重会话数。
    """
    # 先清本批次覆盖会话的旧行（自查愈），再写本轮发现。
    for chunk in _chunks(sorted(covered_session_ids)):
        marks = ",".join("?" * len(chunk))
        con.execute(
            f"DELETE FROM ce_ingest_quarantine WHERE session_id IN ({marks})",
            chunk,
        )
    if not findings:
        return 0
    now = _utc_now()
    con.executemany(
        "INSERT OR REPLACE INTO ce_ingest_quarantine "
        "(quarantine_id, generation_id, family, session_id, reason, detail, "
        " created_at) VALUES (?,?,?,?,?,?,?)",
        [
            (
                hashlib.sha256(
                    f"{generation_id}|{f.session_id}|{f.code}".encode("utf-8")
                ).hexdigest()[:24],
                generation_id,
                f.family,
                f.session_id,
                f.code,
                f.detail,
                now,
            )
            for f in findings
        ],
    )
    return len({f.session_id for f in findings})


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


__all__ = [
    "EVENT_TIME_TOLERANCE_SECONDS",
    "SECRET_RULES",
    "GateFinding",
    "LiveGateError",
    "bad_timestamp",
    "collect_timestamp_quarantine_findings",
    "detect_empty_sessions",
    "detect_message_count_mismatch",
    "run_hard_gates",
    "scan_content_for_secrets",
    "write_quarantine",
]
