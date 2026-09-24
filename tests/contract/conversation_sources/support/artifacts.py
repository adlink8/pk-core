"""合成夹具与断言助手：确定性、脱敏、不读权威库。

这是 conversation_sources 测试目录的公共底座。测试文件之间**不得**互相
import 私有 helper —— 需要跨模块复用的 builder 一律放这里，或放
``support/<module>.py``（按生产模块一一对应）。

夹具只构造 ``SourceArtifact`` 与字节，正文全是合成句子。真实对话正文、
凭据、账号一律不进仓库；每个 sqlite 夹具都会被塞入 :data:`CANARY`，用来
断言凭据从不抵达事件、清单或日志。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from personal_knowledge.adapters.conversation_sources.contracts import (
    SourceArtifact,
    SourceArtifactSet,
)
from personal_knowledge.adapters.conversation_sources.snapshots import (
    capture_directory,
    capture_file,
    capture_sqlite,
)

# 塞进夹具库的哨兵值：它出现任何地方都算隐私泄漏。
CANARY = "canary-secret-value-314159"

# 合成正文：不复制真实会话。
USER_TEXT = "合成用户句"
ASSISTANT_TEXT = "合成助手句"


# --------------------------------------------------------------------- bytes

def jsonl(records: list[dict]) -> str:
    """一个 JSON 对象一行（JSONL / NDJSON）。"""
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)


def blob_name(label: str) -> str:
    """由 label 确定性派生的内容哈希；同时用作 blob 文件名前缀。"""
    return hashlib.sha256(f"fixture|{label}".encode("utf-8")).hexdigest()


def blobs(artifact_summary: str) -> str:
    return hashlib.sha256(artifact_summary.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- artifacts

def file_artifact(
    root: Path,
    label: str,
    relative_path: str,
    raw: str | bytes,
    *,
    family: str | None = None,
    source_kind: str = "file",
) -> tuple[SourceArtifact, Path]:
    """写一份字节并把 ``SourceArtifact`` 指过去。

    ``label`` 必须在该次适配的 artifact set 内唯一：它同时决定
    ``artifact_id``，而事件 id 由 artifact id 派生。
    """
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    digest = blob_name(label)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / digest[:32]).write_bytes(data)
    artifact = SourceArtifact(
        artifact_id=f"art-{label}",
        family=family or label.split(".", 1)[0],
        source_kind=source_kind,
        content_hash=digest,
        capture_method="fixture",
        relative_path=relative_path,
        byte_size=len(data),
    )
    return artifact, root


def single(artifact: SourceArtifact) -> SourceArtifactSet:
    """只有一个 artifact 的集合。"""
    return SourceArtifactSet((artifact,))


def captured_sqlite(
    db: Path,
    store: Path,
    *,
    allowed_tables,
    allowed_columns,
    family: str | None = None,
    byte_limit: int = 1_000_000,
    count_limit: int = 8,
    mirror_path: str | None = None,
) -> tuple[SourceArtifact, Path]:
    """经真实 capture seam 抓一份 sqlite，返回 ``(artifact, artifact_root)``。"""
    artifact, blob = capture_sqlite(
        db,
        store,
        allowed_tables=allowed_tables,
        allowed_columns=allowed_columns,
        byte_limit=byte_limit,
        count_limit=count_limit,
        family=family,
        mirror_path=mirror_path,
    )
    return artifact, blob.parent


def captured_file(
    src: Path,
    store: Path,
    *,
    relative_path: str,
    byte_limit: int = 1_000_000,
    count_limit: int = 4,
) -> tuple[SourceArtifact, Path]:
    """经真实 capture seam 抓单个文件，返回 ``(artifact, artifact_root)``。"""
    artifact, blob = capture_file(
        src,
        store,
        relative_path=relative_path,
        byte_limit=byte_limit,
        count_limit=count_limit,
    )
    return artifact, blob.parent


def captured_directory(
    src: Path,
    store: Path,
    *,
    include_relative,
    byte_limit: int = 1_000_000,
    count_limit: int = 8,
):
    """经真实 capture seam 抓一个目录，返回 ``(manifest, artifacts)``。"""
    return capture_directory(
        src,
        store,
        include_relative=include_relative,
        byte_limit=byte_limit,
        count_limit=count_limit,
    )


# ----------------------------------------------------------------- assertions

def events_of(result, kind):
    """某一种事件的全部事件。"""
    return tuple(event for event in result.events if event.kind is kind)


def has_event(result, kind, content: str) -> bool:
    """结果里存在一条该种类、正文恰好等于 ``content`` 的事件。"""
    return any(event.content == content for event in events_of(result, kind))


def event_with(result, kind):
    """第一条该种类的事件；没有则抛 AssertionError。"""
    found = events_of(result, kind)
    assert found, f"no {kind} event in {[e.kind for e in result.events]}"
    return found[0]


def reasons(event) -> str:
    """一条事件上全部 field disposition 的 reason 拼接。"""
    return " ".join(item.reason for item in event.field_dispositions)


def record_text(event) -> str:
    """一条事件上正文、摘要与全部 disposition reason 的拼接。

    和 :func:`event_text` 的区别：这里只看**一条**事件，并把 reason 也算进去，
    用于「某条记录本身没正文，但必须在 reason 里点名」的断言。
    """
    return "\n".join(
        part for part in (event.content, event.summary, reasons(event)) if part
    )


def any_reason(result, needle: str) -> bool:
    """任意事件的 disposition reason 里出现 ``needle``。"""
    return any(needle in reasons(event) for event in result.events)


def body(event) -> str:
    """一条事件的正文与摘要拼接（正文在前，摘要补位）。"""
    return "\n".join(
        value for value in (event.content, event.summary) if value
    )


def event_text(result) -> str:
    """结果里全部正文与摘要的拼接，用于跑 canary 泄漏断言。"""
    return " ".join(
        value
        for event in result.events
        for value in (event.content, event.summary)
        if value
    )


def bare_unknowns(result):
    """既无正文、又无摘要、也无 disposition 的 unknown 事件（不允许存在）。"""
    return tuple(
        event
        for event in result.events
        if event.kind.value == "unknown_native"
        and not (event.content or event.summary or event.field_dispositions)
    )
