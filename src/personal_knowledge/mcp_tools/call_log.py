# -*- coding: utf-8 -*-
"""MCP 真实调用详单（2026-09-29）：每一次 tools/call 落一条记录。

为何：personal-data 接入多个 AI 客户端后，需要知道哪个客户端、何时、调了什么工具、
参数是什么、多快、成功还是失败、返回了什么——排查与用量分析都靠它。
怎么：SQLite var/db/mcp_call_log.sqlite（WAL，多客户端进程并发写安全）；
参数与结果摘要在落库前先过 privacy_guard 封存（不在日志里落明文密钥）；
写日志任何失败都静默吞掉（fail-open），绝不影响工具调用本身。

查看：
  python -m personal_knowledge.mcp_tools.call_log --stats          # 汇总
  python -m personal_knowledge.mcp_tools.call_log --limit 20       # 最近 20 条
"""
from __future__ import annotations
import datetime
import json
import sqlite3
import sys

DB = r"D:/ADLINK/数据分析/var/db/mcp_call_log.sqlite"
_HEAD_CHARS = 400

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
  result_head TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_tool ON calls(tool);
"""


def client_label(server) -> str:
    """尽力识别调用方客户端（SDK 版本间字段有差异，全防御式取值）。"""
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
    return "unknown"


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
        head = guard_mcp_payload((result_text or "")[:_HEAD_CHARS])
        con = sqlite3.connect(DB, timeout=5)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(_SCHEMA)  # 多语句建表须用 executescript
            con.execute(
                "INSERT INTO calls(ts,client,tool,args_json,duration_ms,ok,error,"
                "result_chars,result_head) VALUES(?,?,?,?,?,?,?,?,?)",
                (datetime.datetime.now().isoformat(timespec="milliseconds"),
                 client, tool, args_s, int(duration_ms), 1 if ok else 0,
                 error, len(result_text or ""), head))
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
        "SELECT ts, client, tool, duration_ms, ok, error, result_chars FROM calls "
        "ORDER BY id DESC LIMIT ?", (a.limit,)).fetchall()
    print(f"\n最近 {len(rows)} 条：")
    for ts, client, tool, ms, ok, err, chars in rows:
        mark = "OK " if ok else "ERR"
        tail = f" error={err[:60]}" if err else ""
        print(f"  [{mark}] {ts} {client or 'unknown'} {tool} {ms}ms {chars}字{tail}")
    con.close()


if __name__ == "__main__":
    main()
