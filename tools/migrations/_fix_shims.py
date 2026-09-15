"""Rewrite compatibility shims to rebind full real modules (incl. _private names)."""
from __future__ import annotations

from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
PACKAGES = [
    "core",
    "knowledge",
    "memory",
    "conversation",
    "graph",
    "vector",
    "services",
    "pipeline",
]


def make_shim(mod: str, pkg: str) -> str:
    return f'''"""Compatibility shim -> {pkg}.{mod}

Legacy CLI: python integration/scripts/{mod}.py
Preferred:  python -m {pkg}.{mod}
"""
from __future__ import annotations

from importlib import import_module
import sys

_real = import_module("{pkg}.{mod}")

if __name__ == "__main__":
    # Do not rebind __main__; invoke real entrypoint cleanly.
    if hasattr(_real, "main") and callable(getattr(_real, "main")):
        raise SystemExit(_real.main())
    import runpy
    raise SystemExit(runpy.run_module("{pkg}.{mod}", run_name="__main__"))

# When imported as legacy top-level name, rebind so private symbols work.
sys.modules[__name__] = _real
'''


def main() -> int:
    n = 0
    for pkg in PACKAGES:
        d = SCRIPTS / pkg
        if not d.is_dir():
            continue
        for f in d.glob("*.py"):
            if f.name == "__init__.py":
                continue
            # never shim meta helpers
            if f.stem.startswith("_"):
                continue
            mod = f.stem
            # project_paths already lived under core/; keep core.project_paths package path
            # but still provide flat shim for any legacy "import project_paths"
            shim = SCRIPTS / f"{mod}.py"
            shim.write_text(make_shim(mod, pkg), encoding="utf-8")
            n += 1
    print("rewrote", n, "shims")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
