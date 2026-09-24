#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""注册 / 重建「每周一次」的 live 增量同步计划任务（pk-live-weekly）。

背景：2026-09-18 停用了实时后台轮询（`pk-live-watch` 30s 守护 + `pk-live-catchup` 每
15 分钟），改为每周一次。入口脚本放在 git worktree 的 `var/run/` 下（不入库），
worktree 被重建时会丢失 —— 本脚本用于从零恢复整条链路。

用法（在项目根目录执行）：
    python tools/register-weekly-sync.py              # 写入入口脚本 + 注册任务
    python tools/register-weekly-sync.py --check      # 只看现状，不写
    python tools/register-weekly-sync.py --run-now    # 立即触发一次
    python tools/register-weekly-sync.py --unregister # 删除任务

为什么是 .py 而不是 .ps1：路径含中文（数据分析 / 数据分析-live-wt），
PowerShell 5.1 会把无 BOM 的 UTF-8 脚本按 GBK 解析导致路径乱码；
Python 源文件默认 UTF-8，没有这个坑。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------- 可配置项

WORKTREE = Path(r"D:\ADLINK\数据分析-live-wt")   # 含 watch.py / live_sync.py 的分支工作树
MAIN_ROOT = Path(r"D:\ADLINK\数据分析")           # 主工作树（数据落在它下面）
PYTHON = Path(r"C:\Users\li\AppData\Local\Programs\Python\Python312\python.exe")

DB = MAIN_ROOT / "data/staging/v2/agent_conversations_v2.sqlite"
MIRROR = MAIN_ROOT / "data/staging/v2/native"
ENTRY = WORKTREE / "var/run/pk_weekly_entry.py"
LOG = WORKTREE / "var/run/weekly.log"

TASK = r"\pk-live-weekly"
USER_SID = "S-1-5-21-854921990-210623465-2341265901-1001"

# 每周触发点。2026-09-18 由「周日 23:00」调整为「周一 01:00」——即周日夜里跨过零点那一刻，
# 仍属「周末收尾」，但机器此时通常已空闲（23:00 常与游戏/交互重叠）。
# ⚠ 改这里之后必须重跑本脚本才会生效（任务是从本文件生成的 XML 注册的）。
SCHEDULE = {"day": "Monday", "hour": 1, "minute": 0}
_WEEKDAY = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
            "Friday": 4, "Saturday": 5, "Sunday": 6}
TASK_XML_PATH = MAIN_ROOT / "var/run/pk-live-weekly.task.xml"

ENTRY_SOURCE = '''# pk-core weekly live sync entry  (由 tools/register-weekly-sync.py 生成)
#
# 替代原先的实时轮询（pk-live-watch 30s 守护 + pk-live-catchup 每 15 分钟）。
#
# ⚠ 与 pk_live_entry.py 的 once 模式唯一区别：那个版本跑完会调用
#   _ensure_watch_alive()，用 `schtasks /run /tn pk-live-watch` 把轮询守护重新拉起。
#   本脚本刻意不做该调用，否则每周同步都会顺带复活被停掉的轮询。
#
# 依赖 git worktree: {worktree}  （分支 fix/audit-20260915）
import sys

sys.path.insert(0, r"{worktree_src}")

from pathlib import Path  # noqa: E402

DB = Path(r"{db}")
MIRROR = Path(r"{mirror}")
LOG = Path(r"{log}")


def main() -> int:
    import contextlib
    import datetime
    import traceback

    with open(LOG, "a", encoding="utf-8") as log:
        log.write("\\n==== weekly start %s ====\\n"
                  % datetime.datetime.now().isoformat(timespec="seconds"))
        log.write("db     = %s\\n" % DB)
        log.write("mirror = %s\\n" % MIRROR)
        log.flush()
        try:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                sys.argv = ["pk", "conversations", "--live-sync",
                            "--live-db", str(DB), "--live-mirror", str(MIRROR)]
                from personal_knowledge.cli import sync

                sync()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 0
            log.write("==== weekly exit %s @ %s ====\\n"
                      % (code, datetime.datetime.now().isoformat(timespec="seconds")))
            return code            # 刻意不调用 watch 自愈：轮询已停用
        except BaseException:
            log.write("==== weekly FAILED ====\\n")
            log.write(traceback.format_exc())
            return 1
        log.write("==== weekly exit 0 @ %s ====\\n"
                  % datetime.datetime.now().isoformat(timespec="seconds"))
        return 0


if __name__ == "__main__":
    sys.exit(main())
'''

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <URI>{task}</URI>
    <Description>pk-core weekly live conversation sync (replaces the retired 30s polling daemon and the every-15-minute catchup task). Runs pk_weekly_entry.py once, which does NOT revive the watch daemon.</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>{sid}</UserId>
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <IdleSettings>
      <Duration>PT10M</Duration><WaitTimeout>PT1H</WaitTimeout>
      <StopOnIdleEnd>true</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek><{day} /></DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{py}</Command>
      <Arguments>-u "{entry}"</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _run(*args: str) -> tuple[int, str]:
    r = subprocess.run([r"C:\Windows\System32\schtasks.exe", *args], capture_output=True)
    out = b""
    for enc in ("gbk", "utf-8-sig"):
        try:
            out = (r.stdout + r.stderr).decode(enc)
            break
        except Exception:
            out = (r.stdout + r.stderr).decode("utf-8", "replace")
    return r.returncode, out.strip()


def write_entry() -> None:
    ENTRY.parent.mkdir(parents=True, exist_ok=True)
    text = ENTRY_SOURCE.format(
        worktree=WORKTREE, worktree_src=WORKTREE / "src",
        db=DB, mirror=MIRROR, log=LOG,
    )
    ENTRY.write_text(text, encoding="utf-8")
    print("[entry] 已写入 %s (%d bytes)" % (ENTRY, ENTRY.stat().st_size))


def register() -> int:
    import datetime
    today = datetime.date.today()
    target = _WEEKDAY[SCHEDULE["day"]]
    days = (target - today.weekday()) % 7 or 7     # 下一个 SCHEDULE["day"]
    start = (today + datetime.timedelta(days=days)).isoformat()
    xml = TASK_XML.format(task=TASK, sid=USER_SID, day=SCHEDULE["day"],
                          start="%sT%02d:%02d:00" % (start, SCHEDULE["hour"], SCHEDULE["minute"]),
                          py=PYTHON, entry=ENTRY)
    TASK_XML_PATH.parent.mkdir(parents=True, exist_ok=True)
    TASK_XML_PATH.write_bytes(b"\xff\xfe" + xml.encode("utf-16-le"))  # UTF-16LE + BOM
    rc, out = _run("/create", "/tn", TASK, "/xml", str(TASK_XML_PATH), "/f")
    print("[task ] schtasks /create rc=%d | %s" % (rc, out))
    return rc


def check() -> int:
    print("[ paths ]")
    for label, p in (("worktree", WORKTREE), ("entry", ENTRY), ("log", LOG), ("db", DB)):
        print("   %-9s %-56s %s" % (label, p, "OK" if p.exists() else "MISSING"))
    print("[ tasks ]")
    rc, out = _run("/query", "/tn", TASK, "/fo", "LIST")
    print("   rc=%d %s" % (rc, out[:400]))
    for t in (r"\pk-live-watch", r"\pk-live-catchup"):
        rc2, out2 = _run("/query", "/tn", t, "/fo", "LIST")
        state = "禁用/不存在" if rc2 != 0 else "存在"
        print("   %-22s %s" % (t, state))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="注册 pk-live-weekly 每周同步任务")
    ap.add_argument("--check", action="store_true", help="只体检，不写入")
    ap.add_argument("--run-now", action="store_true", help="立即触发一次")
    ap.add_argument("--unregister", action="store_true", help="删除任务")
    args = ap.parse_args()

    if args.unregister:
        rc, out = _run("/delete", "/tn", TASK, "/f")
        print("[task ] schtasks /delete rc=%d | %s" % (rc, out))
        return rc
    if args.check:
        return check()
    if args.run_now:
        rc, out = _run("/run", "/tn", TASK)
        print("[task ] schtasks /run rc=%d | %s" % (rc, out))
        return rc

    write_entry()
    rc = register()
    print()
    check()
    print()
    print("下次运行时间见上方；日志: %s" % LOG)
    return rc


if __name__ == "__main__":
    sys.exit(main())
