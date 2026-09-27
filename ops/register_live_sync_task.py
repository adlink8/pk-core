# -*- coding: utf-8 -*-
"""(重)生成 pk-live-sync-authority 计划任务 XML 并注册。

用法：python ops/register_live_sync_task.py
XML 以 UTF-16 落盘（schtasks 要求），源内容以本文件内的字符串为准（可审查）。
动作指向 ops/live-sync-cycle.ps1（入库 + 索引两步）。
"""
import subprocess
from pathlib import Path

TASK = "pk-live-sync-authority"
XML_PATH = Path(__file__).parent / f"{TASK}.task.xml"

XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>live_sync 唯一写者：增量采集各客户端会话直写权威库（fail-closed 门禁+quarantine），随后 FTS 增量索引。调度=每日 01:00（2026-09-26 由 15 分钟周期改为每日）。</Description>
    <URI>\\{task}</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-21-854921990-210623465-2341265901-1001</UserId>
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <IdleSettings>
      <Duration>PT10M</Duration>
      <WaitTimeout>PT1H</WaitTimeout>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-09-27T01:00:00+08:00</StartBoundary>
      <DaysInterval>1</DaysInterval>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>C:\\Program Files\\PowerShell\\7\\pwsh.exe</Command>
      <Arguments>-NoProfile -ExecutionPolicy Bypass -File "D:\\ADLINK\\数据分析\\ops\\live-sync-cycle.ps1"</Arguments>
      <WorkingDirectory>D:\\ADLINK\\数据分析</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
""".replace("{task}", TASK)

XML_PATH.write_text(XML, encoding="utf-16")
print(f"XML written: {XML_PATH}")
result = subprocess.run(
    ["schtasks", "/create", "/tn", TASK, "/xml", str(XML_PATH), "/f"],
    capture_output=True, text=True,
)
print(result.stdout or result.stderr)
raise SystemExit(result.returncode)
