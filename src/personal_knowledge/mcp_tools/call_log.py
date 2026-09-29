# -*- coding: utf-8 -*-
"""MCP 真实调用详单（2026-09-29）：每一次 tools/call 落一条记录。

为何：personal-data 接入多个 AI 客户端后，需要知道哪个客户端、何时、调了什么工具、
参数是什么、多快、成功还是失败、返回了什么——排查与用量分析都靠它。
怎么：SQLite var/db/mcp_call_log.sqlite（WAL，多客户端进程并发写安全）；
参数与结果在落库前先过 privacy_guard 封存（不在日志里落明文密钥）；
召回内容全量落库（result_text，含每条命中的名次/会话/分数，64KB 兜底截断）；
quality_note 记录本次检索置信路径（vector_confident/judge_rerank/fts_degraded/fts_forced）
——线上调用无标准答案算不了真召回率，置信路径+逐条分数是逐调用代理指标；
写日志任何失败都静默吞掉（fail-open），绝不影响工具调用本身。

查看：
  python -m personal_knowledge.mcp_tools.call_log --stats          # 汇总
  python -m personal_knowledge.mcp_tools.call_log --limit 20       # 最近 20 条
"""
from __future__ import annotations
import datetime
import json
import os
import sqlite3
import sys

DB = r"D:/ADLINK/数据分析/var/db/mcp_call_log.sqlite"
_MAX_TEXT = 65536  # 召回内容全量上限(k=20 约 5KB,64KB 兜底)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  client TEXT,
  tool TEXT NOT NULL,
  args_json TEXT,
  duration_ms INTEGER,
  ok INTEGER NOT NULL,
  error TEXT,
  result_chars INTEGER,
  result_head TEXT,
  result_text TEXT,
  quality_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_tool ON calls(tool);
"""

# 质量信号：从 conversation 域输出提示里提取本次检索的置信路径（召回率的
# 逐调用代理指标——线上无标准答案算不了真召回率，置信路径+分数是可用代理）
import re as _re
_QUALITY_PATS = [
    ("vector_confident", _re.compile(r"纯向量置信充足")),
    ("judge_rerank", _re.compile(r"裁判重排")),
    ("fts_forced", _re.compile(r"--fts")),
    ("fts_degraded", _re.compile(r"自动降级 FTS")),
]


def _quality_note(text: str) -> str:
    for tag, pat in _QUALITY_PATS:
        if pat.search(text):
            return tag
    return ""


def client_label(server) -> str:
    """尽力识别调用方客户端（SDK 版本间字段有差异，全防御式取值）。
    2026-09-29 夜测补：HTTP stateless 模式下 request_context.session 不携带
    initialize 参数，实测全部落成 unknown（304 条轰炸 0 条带标签）——
    兜底读环境变量 PERSONAL_DATA_MCP_CLIENT_TAG（HTTP 服务启动时置 'http'），
    让 http 与 stdio 调用至少可分。"""
    try:
        sess = getattr(server.request_context, "session", None)
        for attr in ("client_params", "initialize_params"):
            params = getattr(sess, attr, None)
            info = getattr(params, "clientInfo", None) or getattr(params, "client_info", None)
            if info is not None:
                name = getattr(info, "name", None) or "unknown"
                ver = getattr(info, "version", "") or ""
                return f"{name}@{ver}" if ver else str(name)
    except Exception:
        pass
    return os.environ.get("PERSONAL_DATA_MCP_CLIENT_TAG") or "unknown"


def record(server, tool: str, arguments: dict, duration_ms: int,
           ok: bool, error: str | None, result_text: str) -> None:
    """落一条调用记录；任何异常静默吞掉（日志永不影响服务）。"""
    try:
        from personal_knowledge.core.privacy_guard import guard_mcp_payload
        client = client_label(server)
        try:
            args_s = guard_mcp_payload(json.dumps(arguments, ensure_ascii=False))
        except Exception:
            args_s = "<args unserializable>"
        head = guard_mcp_payload((result_text or "")[:400])
        full = guard_mcp_payload((result_text or "")[:_MAX_TEXT])
        con = sqlite3.connect(DB, timeout=5)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(_SCHEMA)  # 多语句建表须用 executescript
            con.execute(
                "INSERT INTO calls(ts,client,tool,args_json,duration_ms,ok,error,"
                "result_chars,result_head,result_text,quality_note) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.datetime.now().isoformat(timespec="milliseconds"),
                 client, tool, args_s, int(duration_ms), 1 if ok else 0,
                 error, len(result_text or ""), head, full,
                 _quality_note(result_text or "")))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="MCP 调用详单查询")
    ap.add_argument("--stats", action="store_true", help="按客户端×工具汇总")
    ap.add_argument("--limit", type=int, default=20, help="最近 N 条明细")
    a = ap.parse_args()
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    if a.stats:
        for row in con.execute(
                "SELECT client, tool, COUNT(*), SUM(ok), ROUND(AVG(duration_ms)) "
                "FROM calls GROUP BY client, tool ORDER BY 3 DESC"):
            print(f"{row[0] or 'unknown':28s} {row[1]:32s} 调用{row[2]:4d} 成功{row[3] or 0:4d} "
                  f"均耗{row[4] or 0}ms")
    rows = con.execute(
        "SELECT ts, client, tool, duration_ms, ok, error, result_chars, quality_note "
        "FROM calls ORDER BY id DESC LIMIT ?", (a.limit,)).fetchall()
    print(f"\n最近 {len(rows)} 条：")
    for ts, client, tool, ms, ok, err, chars, q in rows:
        mark = "OK " if ok else "ERR"
        tail = f" error={err[:60]}" if err else ""
        print(f"  [{mark}] {ts} {client or 'unknown'} {tool} {ms}ms {chars}字 "
              f"[{q or '-'}]{tail}")
    con.close()


if __name__ == "__main__":
    main()
