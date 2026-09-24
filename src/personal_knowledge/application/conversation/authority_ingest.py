# -*- coding: utf-8 -*-
"""权威库直写入库编排：inventory → normalized → 正确性门禁 → canonical 发布 → 审计。

本模块替代已退役的 v2 live-sync → staging → 人工激活三段链路。Phase 62 D-18/D-31
的"永不自动激活"人工门，由这里的 fail-closed 门禁接替：

* **硬门**（secret 泄漏残留、消息丢失、时间倒挂、重复源会话、孤儿消息、空会话）：
  任一不过即抛 :class:`HardGateError`，canonical 构建不会启动，权威库零接触。
* **软门**（时间戳 null/''/'0'/epoch 秒/非 ISO、消息数盈余）：问题会话写入
  normalized 库的 ``ingest_quarantine`` 隔离表并计入报告，canonical 构建排除这些
  会话，批次其余部分照常发布。

每次运行（含失败）都写入独立审计库 ``var/db/ingest_audit.sqlite``——与权威库物理
分离，失败时权威库保持原状，审计痕迹不依赖被写入方存活。人类可读日志追加到
``var/logs/authority-ingest.log``。

canonical 发布沿用 staging + 原子 replace，旧权威库发布前备份为
``agent_conversations.backup.sqlite``（单份滚动覆盖）；发布语义为 union——
以现有权威库为底按 canonical id 增量并集，投影没有的 id 原样保留。

缺口家族 feeder 通道 :func:`run_gap_family_channel`：只喂适配器 discovery
在本机没有原生根的家族（见
:data:`build_canonical_agent_conversations.GAP_CHANNEL_FAMILIES` 的查询证据），
不经默认摄入路径的门禁，审计 status 用可区分的 ``gap_channel_*`` 值。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.application.conversation.build_canonical_agent_conversations import (
    GAP_CHANNEL_FAMILIES,
)
from personal_knowledge.core.project_paths import (
    AGENTSVIEW_NORMALIZED_DB,
    AGENT_CONVERSATIONS_DB,
    ROOT,
)

AUDIT_DB = ROOT / "var/db/ingest_audit.sqlite"
HUMAN_LOG = ROOT / "var/logs/authority-ingest.log"

# 合法时间戳：ISO-8601（允许日期-only 与空格分隔），其余一律视为可疑。
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?([+-]\d{2}:?\d{2}|Z)?$")
# 10 位整数 = epoch 秒（qoder 家族实测把裸秒存进了 started_at）。
_EPOCH_SECONDS_RE = re.compile(r"^\d{10}$")
# 13 位整数 = epoch 毫秒，视为可救回的软缺陷（换算而非丢弃由人工复查决定）。
_EPOCH_MILLIS_RE = re.compile(r"^\d{13}$")

QUARANTINE_TABLE = "ingest_quarantine"


class HardGateError(Exception):
    """硬门失败：批次作废，权威库不得发布。"""

    def __init__(self, findings: list["GateFinding"]) -> None:
        self.findings = findings
        codes = sorted({f.code for f in findings})
        super().__init__(
            f"hard gate blocked: {len(findings)} finding(s), codes={codes}"
        )


@dataclass(frozen=True)
class GateFinding:
    """一条门禁发现。severity: hard=拦批；soft=隔离会话、批次放行。"""

    code: str
    severity: str
    session_id: str
    source_session_id: str
    agent: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class IngestReport:
    run_id: str
    status: str = "running"
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    steps: list[dict] = field(default_factory=list)
    hard_findings: list[GateFinding] = field(default_factory=list)
    soft_findings: list[GateFinding] = field(default_factory=list)
    quarantined_sessions: int = 0
    eligible_sessions: int = 0
    published_sessions: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["hard_findings"] = [f.to_dict() for f in self.hard_findings]
        d["soft_findings"] = [f.to_dict() for f in self.soft_findings]
        return d


# ------------------------------------------------------------------ 门禁实现


def _bad_timestamp(value: object) -> str | None:
    """返回时间戳缺陷描述；合法返回 None。"""
    if value is None:
        return "null"
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


def check_normalized(av_db: Path) -> tuple[list[GateFinding], list[GateFinding]]:
    """对 normalized 库跑正确性门禁，返回 (硬门发现, 软门发现)。

    只读：不写 quarantine、不改任何数据。quarantine 写入由 :func:`write_quarantine`
    在门禁通过后单独执行。
    """
    hard: list[GateFinding] = []
    soft: list[GateFinding] = []
    if not av_db.exists():
        raise HardGateError([
            GateFinding("normalized_missing", "hard", "-", "-", "-",
                        f"normalized DB not found: {av_db}")
        ])

    con = sqlite3.connect(f"file:{av_db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        sessions = [dict(r) for r in con.execute(
            "SELECT session_id, source_session_id, agent, started_at, ended_at, "
            "message_count, secret_leak_count, evidence_eligible, excluded "
            "FROM sessions"
        )]

        # 重复源会话：同一条 AgentView 会话被解析出两条 normalized 记录。
        seen_source: dict[str, str] = {}
        for s in sessions:
            src = str(s["source_session_id"])
            if src in seen_source:
                hard.append(GateFinding(
                    "duplicate_source_session", "hard", s["session_id"], src,
                    str(s["agent"]),
                    f"source_session_id {src} already emitted as {seen_source[src]}",
                ))
            else:
                seen_source[src] = str(s["session_id"])

        # 孤儿消息：FK 理论上拦得住，这里显式验证（normalized 可能由旧代码构建）。
        orphans = con.execute(
            "SELECT COUNT(*) FROM messages m WHERE NOT EXISTS "
            "(SELECT 1 FROM sessions s WHERE s.session_id = m.session_id)"
        ).fetchone()[0]
        if orphans:
            hard.append(GateFinding(
                "orphan_messages", "hard", "-", "-", "-",
                f"{orphans} message(s) reference a missing session",
            ))

        msg_counts = {
            str(r["session_id"]): int(r["n"])
            for r in con.execute(
                "SELECT session_id, COUNT(*) AS n FROM messages GROUP BY session_id"
            )
        }
        content_counts = {
            str(r["session_id"]): int(r["n"])
            for r in con.execute(
                "SELECT session_id, COUNT(*) AS n FROM messages "
                "WHERE content IS NOT NULL GROUP BY session_id"
            )
        }

        for s in sessions:
            sid = str(s["session_id"])
            src = str(s["source_session_id"])
            agent = str(s["agent"] or "")
            eligible = bool(s["evidence_eligible"]) and not bool(s["excluded"])
            actual = msg_counts.get(sid, 0)
            declared = s["message_count"]

            # ---- 硬门 ----
            # message_loss 只对 eligible 会话成立：secret/excluded/deleted 会话被
            # normalized 构建器合法 tombstone（0 消息行），其 message_count 保留
            # 源库声明值，不构成丢失。
            if (eligible and isinstance(declared, int) and actual < declared):
                hard.append(GateFinding(
                    "message_loss", "hard", sid, src, agent,
                    f"declared message_count={declared} but only {actual} row(s) present",
                ))

            started_bad = _bad_timestamp(s["started_at"])
            ended_bad = _bad_timestamp(s["ended_at"])
            if started_bad is None and ended_bad is None and eligible:
                # 两侧都可解析才比较先后；必须按时间值比较——字符串比较会把
                # "…37.62Z" 与 "…37.621Z"（毫秒差）误判为倒挂。
                if _parse_ts(str(s["ended_at"])) < _parse_ts(str(s["started_at"])):
                    hard.append(GateFinding(
                        "temporal_inversion", "hard", sid, src, agent,
                        f"ended_at {s['ended_at']} < started_at {s['started_at']}",
                    ))

            if int(s["secret_leak_count"] or 0) > 0 and content_counts.get(sid, 0) > 0:
                hard.append(GateFinding(
                    "secret_content_leak", "hard", sid, src, agent,
                    f"secret_leak_count={s['secret_leak_count']} but "
                    f"{content_counts[sid]} message(s) still carry content",
                ))

            if eligible and actual > 0 and content_counts.get(sid, 0) == 0:
                hard.append(GateFinding(
                    "empty_eligible_session", "hard", sid, src, agent,
                    f"{actual} message(s) but none carries content (parse failure?)",
                ))

            # ---- 软门 ----
            if started_bad is not None:
                soft.append(GateFinding(
                    f"started_at_{started_bad}", "soft", sid, src, agent,
                    f"started_at={s['started_at']!r}",
                ))
            if ended_bad is not None and s["ended_at"] is not None:
                soft.append(GateFinding(
                    f"ended_at_{ended_bad}", "soft", sid, src, agent,
                    f"ended_at={s['ended_at']!r}",
                ))
            if isinstance(declared, int) and actual > declared:
                soft.append(GateFinding(
                    "message_surplus", "soft", sid, src, agent,
                    f"declared message_count={declared} but {actual} row(s) present",
                ))
    finally:
        con.close()
    return hard, soft


def write_quarantine(av_db: Path, findings: list[GateFinding], run_id: str) -> int:
    """把软门发现写进 normalized 库的隔离表，返回被隔离的去重会话数。

    隔离表只保留最近一轮的发现：先清空旧 run 的行再写入。否则某家族适配器修好、
    下一轮通过门禁后，会话仍被历史记录永久排除——隔离必须是自查愈的。
    """
    if not findings:
        return 0
    con = sqlite3.connect(str(av_db), timeout=60)
    try:
        con.execute(
            f"CREATE TABLE IF NOT EXISTS {QUARANTINE_TABLE} ("
            "  quarantine_id TEXT PRIMARY KEY,"
            "  run_id TEXT NOT NULL,"
            "  detected_at TEXT NOT NULL,"
            "  session_id TEXT NOT NULL,"
            "  source_session_id TEXT NOT NULL,"
            "  agent TEXT,"
            "  gate_code TEXT NOT NULL,"
            "  detail TEXT"
            ")"
        )
        con.execute(f"DELETE FROM {QUARANTINE_TABLE} WHERE run_id != ?", (run_id,))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        con.executemany(
            f"INSERT OR REPLACE INTO {QUARANTINE_TABLE} "
            "(quarantine_id, run_id, detected_at, session_id, source_session_id,"
            " agent, gate_code, detail) VALUES (?,?,?,?,?,?,?,?)",
            [
                (f"{run_id}:{f.code}:{f.session_id}", run_id, now, f.session_id,
                 f.source_session_id, f.agent, f.code, f.detail)
                for f in findings
            ],
        )
        con.commit()
        return len({f.session_id for f in findings})
    finally:
        con.close()


# ------------------------------------------------------------------ 审计日志


def _ensure_audit_schema(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS ingest_runs ("
        "  run_id TEXT PRIMARY KEY,"
        "  started_at TEXT,"
        "  finished_at TEXT,"
        "  status TEXT,"
        "  duration_s REAL,"
        "  eligible_sessions INTEGER,"
        "  published_sessions INTEGER,"
        "  quarantined_sessions INTEGER,"
        "  hard_count INTEGER,"
        "  soft_count INTEGER,"
        "  steps_json TEXT,"
        "  error TEXT"
        ")"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS ingest_findings ("
        "  finding_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  run_id TEXT NOT NULL,"
        "  severity TEXT,"
        "  gate_code TEXT,"
        "  session_id TEXT,"
        "  source_session_id TEXT,"
        "  agent TEXT,"
        "  detail TEXT"
        ")"
    )


def record_run(report: IngestReport) -> None:
    """独立连接写审计库：权威库发布失败时痕迹依然落库。"""
    AUDIT_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(AUDIT_DB), timeout=60)
    try:
        _ensure_audit_schema(con)
        con.execute(
            "INSERT OR REPLACE INTO ingest_runs "
            "(run_id, started_at, finished_at, status, duration_s, eligible_sessions,"
            " published_sessions, quarantined_sessions, hard_count, soft_count,"
            " steps_json, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                report.run_id, report.started_at, report.finished_at, report.status,
                report.duration_s, report.eligible_sessions,
                report.published_sessions, report.quarantined_sessions,
                len(report.hard_findings), len(report.soft_findings),
                json.dumps(report.steps, ensure_ascii=False), report.error,
            ),
        )
        con.executemany(
            "INSERT INTO ingest_findings "
            "(run_id, severity, gate_code, session_id, source_session_id, agent, detail)"
            " VALUES (?,?,?,?,?,?,?)",
            [
                (report.run_id, f.severity, f.code, f.session_id,
                 f.source_session_id, f.agent, f.detail)
                for f in list(report.hard_findings) + list(report.soft_findings)
            ],
        )
        con.commit()
    finally:
        con.close()


def _append_human_log(report: IngestReport) -> None:
    HUMAN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(HUMAN_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"==== {report.status} run={report.run_id} "
                 f"{report.finished_at} ({report.duration_s}s) ====\n")
        for step in report.steps:
            fh.write(f"  [{step.get('status')}] {step.get('label')} "
                     f"exit={step.get('exit')} {step.get('elapsed_s')}s\n")
        if report.quarantined_sessions:
            fh.write(f"  quarantined sessions: {report.quarantined_sessions} "
                     f"({len(report.soft_findings)} findings)\n")
        if report.error:
            fh.write(f"  error: {report.error}\n")
        for f in report.hard_findings[:20]:
            fh.write(f"  HARD {f.code} {f.source_session_id} {f.agent}: {f.detail}\n")
        if len(report.hard_findings) > 20:
            fh.write(f"  ... {len(report.hard_findings) - 20} more hard findings, "
                     f"see ingest_findings table\n")


# ------------------------------------------------------------------ 编排


def _run_step(label: str, module: str, extra: list[str]) -> dict:
    cmd = [sys.executable, "-m", module] + extra
    t0 = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(ROOT))
    elapsed = round(time.monotonic() - t0, 1)
    step = {"label": label, "module": module, "exit": proc.returncode,
            "elapsed_s": elapsed,
            "status": "ok" if proc.returncode == 0 else "failed"}
    if proc.returncode != 0:
        step["cmd"] = " ".join(cmd)
    return step


def _count_eligible(av_db: Path) -> int:
    if not av_db.exists():
        return 0
    con = sqlite3.connect(f"file:{av_db.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM sessions WHERE evidence_eligible=1 AND excluded=0"
        ).fetchone()[0]
    finally:
        con.close()


def _count_published() -> int:
    if not AGENT_CONVERSATIONS_DB.exists():
        return 0
    con = sqlite3.connect(
        f"file:{AGENT_CONVERSATIONS_DB.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM canonical_sessions").fetchone()[0]
    finally:
        con.close()


def _count_published_by_agents(agents: tuple[str, ...]) -> int:
    """权威库中指定家族（agent 标签）的 canonical 会话数。"""
    if not AGENT_CONVERSATIONS_DB.exists() or not agents:
        return 0
    con = sqlite3.connect(
        f"file:{AGENT_CONVERSATIONS_DB.as_posix()}?mode=ro", uri=True)
    try:
        marks = ",".join("?" * len(agents))
        return con.execute(
            f"SELECT COUNT(*) FROM canonical_sessions WHERE agent IN ({marks})",
            agents,
        ).fetchone()[0]
    finally:
        con.close()


def _count_eligible_by_agents(av_db: Path, agents: tuple[str, ...]) -> int:
    if not av_db.exists() or not agents:
        return 0
    con = sqlite3.connect(f"file:{av_db.as_posix()}?mode=ro", uri=True)
    try:
        marks = ",".join("?" * len(agents))
        return con.execute(
            f"SELECT COUNT(*) FROM sessions WHERE evidence_eligible=1 AND excluded=0 "
            f"AND agent IN ({marks})",
            agents,
        ).fetchone()[0]
    finally:
        con.close()


def _project_canonical_sessions() -> int:
    """dry-run 跑一遍 canonical 构建，解析其将发布的会话数（不写任何文件）。"""
    proc = subprocess.run(
        [sys.executable, "-m",
         "personal_knowledge.application.conversation."
         "build_canonical_agent_conversations", "--dry-run"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="mbcs",
        errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(
            f"canonical dry-run projection failed (exit {proc.returncode})")
    for line in proc.stdout.splitlines():
        # 形如 "Sessions:           2351"（--- canonical --- 段内）
        m = re.match(r"^Sessions:\s+(\d+)", line.strip())
        if m:
            return int(m.group(1))
    raise RuntimeError("canonical dry-run output missing 'Sessions:' line")


def _assert_no_shrink() -> tuple[int, int]:
    """发布前守卫：投影会话数不得低于权威库现有数。

    canonical 发布是整库原子替换——若投影数变少，发布即静默删除会话，与
    append-only 根本原则冲突。返回 (现有数, 投影数) 供调用方记账。
    """
    current = _count_published()
    projected = _project_canonical_sessions()
    if projected < current:
        raise HardGateError([GateFinding(
            "authority_shrink_guard", "hard", "-", "-", "-",
            f"publish would shrink authority: current canonical_sessions="
            f"{current} > projected={projected}; refusing to publish",
        )])
    return current, projected


def run_ingest(*, write: bool = False, dry_run: bool = False) -> IngestReport:
    """执行一次入库。dry_run=True 时只跑 inventory/normalized 与门禁，不发布。"""
    report = IngestReport(
        run_id=uuid.uuid4().hex[:12],
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    t0 = time.monotonic()
    try:
        mode = "--dry-run" if dry_run else "--write"

        step = _run_step("agentsview inventory",
                         "personal_knowledge.application.conversation."
                         "import_agentsview_sessions", ["--dry-run"])
        report.steps.append(step)
        if step["status"] == "failed":
            raise RuntimeError(f"inventory step failed (exit {step['exit']})")

        step = _run_step("normalized build",
                         "personal_knowledge.application.conversation."
                         "build_agentsview_normalized", [mode])
        report.steps.append(step)
        if step["status"] == "failed":
            raise RuntimeError(
                f"normalized build failed (exit {step['exit']}); "
                "its own revision gate (secret/backfill) may have blocked")

        report.eligible_sessions = _count_eligible(AGENTSVIEW_NORMALIZED_DB)

        hard, soft = check_normalized(AGENTSVIEW_NORMALIZED_DB)
        report.hard_findings = hard
        report.soft_findings = soft
        if hard:
            raise HardGateError(hard)

        if not dry_run:
            report.quarantined_sessions = write_quarantine(
                AGENTSVIEW_NORMALIZED_DB, soft, report.run_id)

            # 发布前收缩守卫：投影数不得低于现有权威库（整库替换语义）。
            current, projected = _assert_no_shrink()
            report.steps.append({
                "label": "shrink guard", "status": "ok",
                "current_sessions": current, "projected_sessions": projected,
            })
        else:
            # dry-run 不写库，但报告"若发布将隔离多少"，让人看得见代价。
            report.quarantined_sessions = len({f.session_id for f in soft})

        step = _run_step("canonical publish",
                         "personal_knowledge.application.conversation."
                         "build_canonical_agent_conversations",
                         [] if dry_run else ["--write"])
        report.steps.append(step)
        if step["status"] == "failed":
            raise RuntimeError(f"canonical publish failed (exit {step['exit']})")

        if not dry_run:
            report.published_sessions = _count_published()
            # 发布后校验：权威库会话数必须覆盖 eligible 减隔离，否则发布内容存疑。
            expected = report.eligible_sessions - report.quarantined_sessions
            if report.published_sessions < expected:
                report.status = "ok_with_post_warning"
                report.error = (
                    f"post-publish check: canonical_sessions="
                    f"{report.published_sessions} < eligible-quarantined={expected}")
        if report.status == "running":
            report.status = "ok"
    except HardGateError as exc:
        report.status = "blocked"
        report.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - 任何失败都记账后再抛出
        report.status = "failed"
        report.error = f"{type(exc).__name__}: {exc}"
    finally:
        report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        report.duration_s = round(time.monotonic() - t0, 1)
        record_run(report)
        _append_human_log(report)
    return report


def run_gap_family_channel(
    *, write: bool = False, dry_run: bool = False,
    families: tuple[str, ...] = GAP_CHANNEL_FAMILIES,
) -> IngestReport:
    """缺口家族 feeder 通道：inventory → normalized → 家族过滤 canonical union 发布 → 审计。

    只喂适配器 discovery 在本机没有原生根、v2 轨零覆盖的家族（清单与查询
    证据见 build_canonical_agent_conversations 模块 docstring）。与
    :func:`run_ingest` 的边界：

    * 不跑默认摄入路径的门禁（secret 泄漏/消息丢失/时间倒挂等硬门与
      quarantine 软门属于主摄入通道，本通道一律不碰）；normalized 构建器
      自身的 revision gate 仍然生效，union 发布又把爆炸半径限制在指定
      家族的 canonical id 内——投影没有的 id 原样保留。
    * canonical 步骤带 ``--families`` 白名单，legacy 仅作合并配对进入产物。
    * 审计落同样的 ingest_runs/ingest_findings 表，status 用可区分的
      ``gap_channel_ok`` / ``gap_channel_dry_run``。
    """
    report = IngestReport(
        run_id=uuid.uuid4().hex[:12],
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    t0 = time.monotonic()
    try:
        mode = "--dry-run" if dry_run else "--write"
        family_arg = ",".join(families)

        step = _run_step("agentsview inventory",
                         "personal_knowledge.application.conversation."
                         "import_agentsview_sessions", ["--dry-run"])
        report.steps.append(step)
        if step["status"] == "failed":
            raise RuntimeError(f"inventory step failed (exit {step['exit']})")

        step = _run_step("normalized build",
                         "personal_knowledge.application.conversation."
                         "build_agentsview_normalized", [mode])
        report.steps.append(step)
        if step["status"] == "failed":
            raise RuntimeError(
                f"normalized build failed (exit {step['exit']}); "
                "its own revision gate (secret/backfill) may have blocked")

        report.eligible_sessions = _count_eligible_by_agents(
            AGENTSVIEW_NORMALIZED_DB, families)

        step = _run_step("canonical publish (gap families)",
                         "personal_knowledge.application.conversation."
                         "build_canonical_agent_conversations",
                         ["--families", family_arg]
                         + ([] if dry_run else ["--write"]))
        step["families"] = list(families)
        report.steps.append(step)
        if step["status"] == "failed":
            raise RuntimeError(f"canonical publish failed (exit {step['exit']})")

        if not dry_run:
            report.published_sessions = _count_published_by_agents(families)
        report.status = "gap_channel_dry_run" if dry_run else "gap_channel_ok"
    except Exception as exc:  # noqa: BLE001 - 任何失败都记账后再抛出
        report.status = "failed"
        report.error = f"{type(exc).__name__}: {exc}"
    finally:
        report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        report.duration_s = round(time.monotonic() - t0, 1)
        record_run(report)
        _append_human_log(report)
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--write", action="store_true",
                   help="发布到权威库（默认 dry-run）")
    p.add_argument("--dry-run", action="store_true",
                   help="只跑 inventory/normalized 与门禁，不发布（默认）")
    p.add_argument("--channel-gap-families", nargs="?", const="", default=None,
                   metavar="FAMILIES",
                   help="缺口家族 feeder 通道：inventory → normalized → 家族过滤 "
                        "canonical union 发布 → 审计。不带值用默认缺口家族清单，"
                        "带值则用逗号分隔的自定义清单。")
    args = p.parse_args(argv)
    if args.write and args.dry_run:
        print("[error] --write 与 --dry-run 互斥", file=sys.stderr)
        return 2

    if args.channel_gap_families is not None:
        families = tuple(
            f.strip() for f in args.channel_gap_families.split(",") if f.strip()
        ) or GAP_CHANNEL_FAMILIES
        report = run_gap_family_channel(
            write=args.write, dry_run=not args.write, families=families)
        print(json.dumps({
            "run_id": report.run_id,
            "status": report.status,
            "families": list(families),
            "eligible_sessions": report.eligible_sessions,
            "published_sessions": report.published_sessions,
            "duration_s": report.duration_s,
            "error": report.error,
        }, ensure_ascii=False, indent=2))
        if report.status == "failed":
            print(f"[error] gap channel failed: {report.error}", file=sys.stderr)
            return 1
        return 0

    report = run_ingest(write=args.write, dry_run=not args.write)
    print(json.dumps({
        "run_id": report.run_id,
        "status": report.status,
        "eligible_sessions": report.eligible_sessions,
        "published_sessions": report.published_sessions,
        "quarantined_sessions": report.quarantined_sessions,
        "hard_findings": len(report.hard_findings),
        "soft_findings": len(report.soft_findings),
        "duration_s": report.duration_s,
        "error": report.error,
    }, ensure_ascii=False, indent=2))
    if report.status in ("blocked", "failed"):
        print(f"[error] ingest {report.status}: {report.error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
