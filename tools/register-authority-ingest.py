#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""注册 / 重建「每周一次」的权威库直写入库计划任务（pk-authority-ingest）。

替代已退役的 pk-live-weekly（v2 live-sync → staging 增量同步，随 staging 库
一同弃用）。新流程每周一 01:00 跑一次 authority_ingest --write：

    inventory → normalized（secret/回填 fail-closed 修订门）
    → 正确性门禁（硬门拦批 / 软门隔离 quarantine）
    → canonical 原子发布进权威库（发布前旧库备份为 backup.sqlite 单份滚动）
    → 审计落账 var/db/ingest_audit.sqlite

门禁任一硬项不过则权威库零接触，退出码非零，下周自然重试；软门问题会话进
normalized 库的 ingest_quarantine 隔离表，只排除问题会话，批次其余照常发布。

入口脚本生成在 var/run/ 下（不入库），本脚本用于从零恢复整条链路。

为什么是 .py 而不是 .ps1：路径含中文（数据分析），PowerShell 5.1 会把无 BOM
的 UTF-8 脚本按 GBK 解析导致路径乱码；Python 源文件默认 UTF-8，没有这个坑。

用法（在项目根目录执行）：
    python tools/register-authority-ingest.py              # 写入口脚本 + 注册任务
    python tools/register-authority-ingest.py --check      # 只看现状，不写
    python tools/register-authority-ingest.py --run-now    # 立即触发一次
    python tools/register-authority-ingest.py --unregister # 删除任务
    python tools/register-authority-ingest.py --retire-live # 停用旧 v2 live 任务（不删除）
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
LOG = ROOT / "var/logs/authority-ingest-weekly.log"
ENTRY = ROOT / "var/run/pk_authority_ingest_entry.py"
TASK = r"\pk-authority-ingest"
USER_SID = "S-1-5-21-854921990-210623465-2341265901-1001"
TASK_XML_PATH = ROOT / "var/run/pk-authority-ingest.task.xml"

# 已退役的 v2 live-sync 任务（停用而不删除，保留定义可回滚）。
RETIRED_TASKS = [r"\pk-live-weekly", r"\pk-live-watch", r"\pk-live-catchup"]

# 每周触发点：周一 01:00（周日夜里跨过零点那一刻，机器通常已空闲）。
SCHEDULE = {"day": "Monday", "hour": 1, "minute": 0}
_WEEKDAY = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
            "Friday": 4, "Saturday": 5, "Sunday": 6}

ENTRY_SOURCE = '''#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""每周权威库直写入库入口（由 tools/register-authority-ingest.py 生成）。

流程：inventory -> normalized（secret/回填 fail-closed 修订门）-> authority_ingest
正确性门禁（硬门拦批 / 软门隔离）-> canonical 原子发布 -> 审计落账。

刻意不做任何"自愈"式调用：不复活已停用的 pk-live-* 任务。失败即退出码非零，
痕迹只在 var/logs/authority-ingest-weekly.log 与 var/db/ingest_audit.sqlite，
下周自然重试。
"""

from __future__ import annotations

import datetime
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(r"{root}")
PYTHON = Path(r"{python}")
LOG = Path(r"{log}")


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as log:
        log.write("\\n==== authority-ingest start %s ====\\n"
                  % datetime.datetime.now().isoformat(timespec="seconds"))
        log.flush()
        cmd = [str(PYTHON), "-m",
               "personal_knowledge.application.conversation.authority_ingest",
               "--write"]
        log.write("cmd: %s\\n" % " ".join(cmd))
        log.flush()
        try:
            proc = subprocess.run(
                cmd, cwd=str(ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="mbcs", errors="replace",
            )
            log.write(proc.stdout)
            log.write("==== authority-ingest exit %s @ %s ====\\n"
                      % (proc.returncode,
                         datetime.datetime.now().isoformat(timespec="seconds")))
            return proc.returncode
        except BaseException:
            log.write("==== authority-ingest FAILED ====\\n")
            log.write(traceback.format_exc())
            return 1


if __name__ == "__main__":
    sys.exit(main())
'''

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <URI>{task}</URI>
    <Description>pk-core weekly authority ingest: agentsview chain -&gt; correctness gates -&gt; canonical atomic publish into agent_conversations.sqlite (replaces the retired v2 live-sync staging task).</Description>
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
    <ExecutionTimeLimit>PT4H</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <IdleSettings>
      <Duration>PT10M</Duration>
      <WaitTimeout>PT1H</WaitTimeout>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start_boundary}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek>
          <{day}/>
        </DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{python}</Command>
      <Arguments>{entry}</Arguments>
      <WorkingDirectory>{root}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _next_start_boundary() -> str:
    """下一个 <SCHEDULE['day']> 的 HH:MM:00（本地时间）。"""
    import datetime as dt
    target = _WEEKDAY[SCHEDULE["day"]]
    now = dt.datetime.now()
    days_ahead = (target - now.weekday()) % 7
    candidate = (now + dt.timedelta(days=days_ahead)).replace(
        hour=SCHEDULE["hour"], minute=SCHEDULE["minute"], second=0, microsecond=0)
    if candidate <= now:
        candidate += dt.timedelta(days=7)
    return candidate.strftime("%Y-%m-%dT%H:%M:%S")


def _task_exists() -> bool:
    proc = subprocess.run(["schtasks", "/query", "/tn", TASK],
                          capture_output=True, text=True,
                          encoding="mbcs", errors="replace")
    return proc.returncode == 0


def check() -> int:
    print(f"task   : {TASK}  exists={_task_exists()}")
    print(f"entry  : {ENTRY}  exists={ENTRY.exists()}")
    print(f"log    : {LOG}  exists={LOG.exists()}")
    for t in RETIRED_TASKS:
        proc = subprocess.run(["schtasks", "/query", "/tn", t],
                              capture_output=True, text=True,
                              encoding="mbcs", errors="replace")
        state = "absent"
        if proc.returncode == 0:
            state = "disabled" if "已禁用" in proc.stdout or "Disabled" in proc.stdout else "ENABLED"
        print(f"retired: {t}  {state}")
    if LOG.exists():
        tail = LOG.read_text(encoding="mbcs", errors="replace").strip().splitlines()
        for line in tail[-6:]:
            print(f"  log| {line}")
    return 0


def write_entry() -> None:
    ENTRY.parent.mkdir(parents=True, exist_ok=True)
    ENTRY.write_text(ENTRY_SOURCE.format(
        root=ROOT, python=PYTHON, log=LOG), encoding="utf-8")
    print(f"[ok] entry script written: {ENTRY}")


def register() -> int:
    write_entry()
    xml = TASK_XML.format(
        task=TASK, sid=USER_SID,
        start_boundary=_next_start_boundary(),
        day=SCHEDULE["day"], python=PYTHON, entry=ENTRY, root=ROOT,
    )
    TASK_XML_PATH.parent.mkdir(parents=True, exist_ok=True)
    # UTF-16LE + BOM：XML 声明 encoding="UTF-16"，schtasks 按声明解码。
    TASK_XML_PATH.write_bytes(b"\xff\xfe" + xml.encode("utf-16-le"))
    proc = subprocess.run(
        ["schtasks", "/create", "/tn", TASK, "/xml", str(TASK_XML_PATH), "/f"],
        capture_output=True, text=True,
        encoding="mbcs", errors="replace")
    if proc.returncode != 0:
        print(f"[error] schtasks create failed:\n{proc.stdout}\n{proc.stderr}",
              file=sys.stderr)
        return 1
    print(f"[ok] registered {TASK} @ weekly {SCHEDULE['day']} "
          f"{SCHEDULE['hour']:02d}:{SCHEDULE['minute']:02d}")
    return 0


def unregister() -> int:
    proc = subprocess.run(["schtasks", "/delete", "/tn", TASK, "/f"],
                          capture_output=True, text=True,
                          encoding="mbcs", errors="replace")
    if proc.returncode != 0:
        print(f"[warn] delete failed (maybe absent): {proc.stdout.strip()}")
    else:
        print(f"[ok] deleted {TASK}")
    return 0


def run_now() -> int:
    proc = subprocess.run(["schtasks", "/run", "/tn", TASK],
                          capture_output=True, text=True,
                          encoding="mbcs", errors="replace")
    if proc.returncode != 0:
        print(f"[error] run failed: {proc.stdout}\n{proc.stderr}", file=sys.stderr)
        return 1
    print(f"[ok] triggered {TASK}; watch {LOG}")
    return 0


def retire_live() -> int:
    """停用（不删除）v2 live-sync 系列任务，保留定义可回滚。"""
    rc = 0
    for t in RETIRED_TASKS:
        proc = subprocess.run(["schtasks", "/query", "/tn", t],
                              capture_output=True, text=True,
                              encoding="mbcs", errors="replace")
        if proc.returncode != 0:
            print(f"[skip] {t} absent")
            continue
        proc = subprocess.run(["schtasks", "/change", "/tn", t, "/disable"],
                              capture_output=True, text=True,
                              encoding="mbcs", errors="replace")
        if proc.returncode == 0:
            print(f"[ok] disabled {t}")
        else:
            print(f"[error] cannot disable {t}: {proc.stdout.strip()}",
                  file=sys.stderr)
            rc = 1
    return rc


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", action="store_true")
    p.add_argument("--run-now", action="store_true")
    p.add_argument("--unregister", action="store_true")
    p.add_argument("--retire-live", action="store_true",
                   help="停用旧 v2 live-sync 任务（不删除）")
    args = p.parse_args()
    if args.check:
        return check()
    if args.unregister:
        return unregister()
    if args.run_now:
        return run_now()
    if args.retire_live:
        return retire_live()
    return register()


if __name__ == "__main__":
    raise SystemExit(main())
