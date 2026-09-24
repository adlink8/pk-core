"""Gemini（family ``gemini``）专用夹具构造器。

Gemini 导出**单个 JSON 文档**：有序 ``messages`` 数组加元数据；用户轮把
``content`` 存成 ``[{text}]``，模型轮存字符串，``thoughts`` 是推理，
``type=info|error`` 是非对话消息。这里只负责写出「文档 -> 捕获 blob -> 指向
它的 ``SourceArtifact``」这条链，正文一律是合成句子。

约定：blob 写在 ``<root>/<content_hash[:32]>``，适配器就是按这个路径读回字节。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact

# 夹具 blob 的确定性摘要：[:32] 同时是文件名。
DOC_HASH = hashlib.sha256(b"fixture|gemini.session").hexdigest()


def messages_document(messages: list[dict], *, session_id: str = "s-synthetic", **metadata) -> dict:
    """一个 Gemini 单 JSON 文档：有序 ``messages`` 加可选元数据字段。

    ``metadata`` 例如 ``model=...`` / ``created_at=...``；只描述文档形状，
    不含真实会话。
    """
    document: dict = {"sessionId": session_id, "messages": list(messages)}
    document.update(metadata)
    return document


def user_message(text: str, *, native_id: str = "u1", timestamp: str | None = None) -> dict:
    """用户轮：``content`` 是 ``[{text}]``。"""
    message = {"id": native_id, "type": "user", "content": [{"text": text}]}
    if timestamp:
        message["timestamp"] = timestamp
    return message


def model_message(
    text: str, *, native_id: str = "m1", timestamp: str | None = None,
    thoughts: list[dict] | None = None,
) -> dict:
    """模型轮：``content`` 是字符串，``thoughts`` 是推理条目。"""
    message = {"id": native_id, "type": "gemini", "content": text}
    if timestamp:
        message["timestamp"] = timestamp
    if thoughts is not None:
        message["thoughts"] = thoughts
    return message


def write_document(
    root: Path, document: dict, *, relative_path: str = "session-1.json",
    artifact_id: str = "art-gemini-session",
) -> SourceArtifact:
    """把文档写成确定性的捕获 blob，返回指向它的 ``SourceArtifact``。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    blob = root / DOC_HASH[:32]
    blob.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return SourceArtifact(
        artifact_id=artifact_id,
        family="gemini",
        source_kind="file",
        content_hash=DOC_HASH,
        capture_method="fixture",
        relative_path=relative_path,
        byte_size=blob.stat().st_size,
    )
