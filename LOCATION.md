# Location migration 2026-07-13

- From: C:\Users\li\Desktop\数据分析
- To:   D:\ADLINK\数据分析
- Method: robocopy /E /COPY:DAT /MT:8 /XJ
- Desktop shortcut: 数据分析.lnk -> this folder
- Code paths: project_paths resolves from package __file__ (no hard-coded Desktop path)

Source on Desktop is removed after successful verification.
