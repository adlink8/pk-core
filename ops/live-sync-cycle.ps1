# P5 调度循环：live_sync 增量入库 → FTS 增量索引（2026-09-26 起）
# 由计划任务 pk-live-sync-authority 每 15 分钟调用（ops/pk-live-sync-authority.task.xml）。
Set-Location -LiteralPath 'D:\ADLINK\数据分析'
$env:PYTHONPATH = 'D:\ADLINK\数据分析\src'
python -m personal_knowledge.application.sync conversations --live-sync 2>&1 |
    Out-File -FilePath 'D:\ADLINK\数据分析\var\logs\live-sync-scheduled.log' -Append -Encoding utf8
python -m personal_knowledge.retrieval.conversation_fts build 2>&1 |
    Out-File -FilePath 'D:\ADLINK\数据分析\var\logs\fts-scheduled.log' -Append -Encoding utf8
exit $LASTEXITCODE
