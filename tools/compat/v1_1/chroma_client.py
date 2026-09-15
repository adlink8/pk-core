"""Compatibility shim -> core.chroma_client

Legacy CLI: python integration/scripts/chroma_client.py
Preferred:  python -m core.chroma_client
"""
from __future__ import annotations

from importlib import import_module
import sys

_real = import_module("personal_knowledge.core.chroma_client")

if __name__ == "__main__":
    # Do not rebind __main__; invoke real entrypoint cleanly.
    if hasattr(_real, "main") and callable(getattr(_real, "main")):
        raise SystemExit(_real.main())
    import runpy
    raise SystemExit(runpy.run_module("personal_knowledge.core.chroma_client", run_name="__main__"))

# When imported as legacy top-level name, rebind so private symbols work.
sys.modules[__name__] = _real
