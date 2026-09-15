"""Entry point / compatibility shim -> personal_knowledge.services.mcp_server"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Add src to sys.path
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Default embed device to CPU for lightweight IDE invocation
os.environ.setdefault("PERSONAL_DATA_EMBED_DEVICE", "cpu")

from personal_knowledge.services.mcp_server import main

if __name__ == "__main__":
    asyncio.run(main())
