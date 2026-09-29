# -*- coding: utf-8 -*-
"""MCP HTTP 常驻服务看门狗：health 探测失败则静默拉起（计划任务每 5 分钟跑一次，
登录触发器也跑同一条——开机保活与自动重启同一个入口，无第二套逻辑）。

静默：本脚本由计划任务以 pythonw.exe 运行（无窗口）；拉起的服务进程用
DETACHED_PROCESS|CREATE_NO_WINDOW，stdout/stderr 重定向到 var/logs/mcp_http.log。
回退：注销计划任务 pk-mcp-http 即停用保活；本脚本单独删除无副作用。
"""
from __future__ import annotations
import datetime
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HEALTH_URL = "http://127.0.0.1:8789/health"
LOG_DIR = ROOT / "var" / "logs"
WATCH_LOG = LOG_DIR / "mcp_watchdog.log"
SERVER_LOG = LOG_DIR / "mcp_http.log"
SERVER_CMD = [sys.executable, "-u",
              str(ROOT / "integration" / "scripts" / "mcp_http_server.py")]


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(WATCH_LOG, "ab") as f:
        f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n".encode("utf-8"))


def alive() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def main() -> int:
    if alive():
        return 0  # 活着，无事可做（绝大多数轮次走这里，零开销）
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_fh = open(SERVER_LOG, "ab")
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
             | subprocess.CREATE_NEW_PROCESS_GROUP)
    subprocess.Popen(SERVER_CMD, stdout=log_fh, stderr=log_fh,
                     creationflags=flags, cwd=str(ROOT), close_fds=True)
    log("service not responding -> restarted (detached)")
    # 2026-09-29 夜测改动：轮询自检最多 120s——服务 boot 期有预热（池+嵌入+裁判
    # 焐热后才起 uvicorn，见 mcp_http_server._prewarm），固定 sleep(4) 必然误报
    # "still down"；且轮询期间若一直起不来，下一轮 5 分钟兜底不变。
    import time
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(4)
        if alive():
            log("post-restart health: ok")
            return 0
    log("post-restart health: still down after 120s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
