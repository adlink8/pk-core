# -*- coding: utf-8 -*-
"""Entry point / compatibility shim -> personal_knowledge.services.mcp_http_server

为何不经 services 目录直接跑脚本：services/http/ 包会遮蔽标准库 http（见
services/mcp_http_server.py 文件头说明）。本 shim 目录无遮蔽物。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("PERSONAL_DATA_MCP_PROFILE", "core")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

from personal_knowledge.services.mcp_http_server import main

if __name__ == "__main__":
    main()
