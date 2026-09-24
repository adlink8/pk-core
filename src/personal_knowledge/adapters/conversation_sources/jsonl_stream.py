"""Phase 62-02: shared JSONL stream parsing primitives.

One JSON object per line (JSONL / NDJSON). Intended as the shared
low-level parser for conversation-source family adapters.

Behaviour is fail-closed by default:

  - corrupt / non-JSON non-blank lines raise :class:`JSONLLineError` with the
    offending line number; pass ``strict=False`` to skip such lines instead.
  - ``max_entries`` / ``max_bytes`` limits raise :class:`JSONLLimitExceeded`
    before the offending entry is yielded (``max_bytes`` is checked against
    the file size before any line is read).

Pure standard library (``json`` / ``pathlib``); no external dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


class JSONLStreamError(Exception):
    """Base class for JSONL stream failures."""


class JSONLLineError(JSONLStreamError):
    """A non-blank line is not a valid JSON object."""


class JSONLLimitExceeded(JSONLStreamError):
    """Input exceeds a declared ``max_bytes`` or ``max_entries`` limit."""


def iter_jsonl_lines(
    path: Path,
    *,
    max_bytes: int | None = None,
    max_entries: int | None = None,
    strict: bool = True,
) -> Iterator[dict]:
    """Yield one parsed JSON object per non-blank line of ``path``.

    Args:
        path: UTF-8 JSONL file to read.
        max_bytes: fail closed when the file is larger than this many bytes.
        max_entries: fail closed when the file holds more than this many
            JSON objects.
        strict: ``True`` (default) raises :class:`JSONLLineError` on a corrupt
            non-blank line; ``False`` skips corrupt lines.

    Yields:
        Parsed JSON object per line, in file order.

    Raises:
        JSONLLimitExceeded: ``max_bytes`` or ``max_entries`` exceeded.
        JSONLLineError: a non-blank line is invalid JSON and ``strict``.
        FileNotFoundError: ``path`` does not exist.
    """
    p = Path(path)

    if max_bytes is not None and p.stat().st_size > max_bytes:
        raise JSONLLimitExceeded(
            f"file {p} exceeds max_bytes={max_bytes} "
            f"(size={p.stat().st_size})"
        )

    entries = 0
    with p.open("r", encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                if strict:
                    raise JSONLLineError(
                        f"invalid JSON at {p} line {lineno}: {exc}"
                    ) from exc
                continue
            if max_entries is not None and entries >= max_entries:
                raise JSONLLimitExceeded(
                    f"file {p} has more than max_entries={max_entries}"
                )
            entries += 1
            yield obj
