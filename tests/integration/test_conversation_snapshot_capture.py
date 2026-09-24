"""Capture seam: ``byte_limit`` 不再拒收整件，抓取必须「保留全部、不裁切」。

公开 seam 是 ``snapshots`` 的三个 capture 入口（``capture_file`` /
``capture_directory`` / ``capture_sqlite``）。历史上每处都有 ``byte_limit``，
超限即 fail closed —— 一整族因此变成 0 条。抓取层只做「原始字节的不可变副本」，
声明式约束是 allowlist / count_limit（文件数），不是体积：体积上限在这里失效。

Integration 层：真实临时文件 / 目录 / SQLite，不 mock、不读 data/var。
"""

from __future__ import annotations

import hashlib
import sqlite3
import tracemalloc
from pathlib import Path

from personal_knowledge.adapters.conversation_sources.snapshots import (
    capture_directory,
    capture_file,
    capture_sqlite,
    replay_manifest,
)

# 正常大小的单文件夹具：4096 字节，明文，远大于下面用的 byte_limit=1。
FILE_BYTES = b"x" * 4096

# 大文件夹具：8 MiB 确定性字节序列（0..255 循环，可复现，非随机）。
# 体积上限撤掉后，目录抓取不得再把这样一份文件整读进内存。
BIG_FILE_BYTES = 8 * 1024 * 1024
BIG_FILE_NAME = "huge.jsonl"

# 目录夹具：每份 48 字节；byte_limit 取 100（< 144 总大小，> 单份大小）。
DIR_FILES = {
    "a.jsonl": b"alpha\n" * 8,
    "b.jsonl": b"beta\n" * 8,
    "c.jsonl": b"gamma\n" * 8,
}


def test_capture_file_keeps_normal_file_under_a_tiny_byte_limit(tmp_path: Path) -> None:
    source = tmp_path / "source" / "session.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(FILE_BYTES)

    artifact, blob = capture_file(
        source,
        tmp_path / "store",
        relative_path="session.jsonl",
        byte_limit=1,
        count_limit=4,
    )

    assert artifact.byte_size == 4096
    assert blob.is_file()
    assert blob.read_bytes() == FILE_BYTES


def test_capture_directory_keeps_every_file_when_total_exceeds_byte_limit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "session"
    source.mkdir()
    for name, data in DIR_FILES.items():
        (source / name).write_bytes(data)

    manifest, artifacts = capture_directory(
        source,
        tmp_path / "store",
        include_relative=("a.jsonl", "b.jsonl", "c.jsonl"),
        byte_limit=100,
        count_limit=8,
    )

    assert len(artifacts) == 3
    assert {a.relative_path for a in artifacts} == {"a.jsonl", "b.jsonl", "c.jsonl"}
    blobs = tmp_path / "store" / "artifacts"
    for artifact in artifacts:
        assert (blobs / artifact.content_hash[:32]).read_bytes() == DIR_FILES[
            artifact.relative_path
        ]
    assert len(manifest.artifacts) == 3


def test_capture_sqlite_keeps_allowlisted_tables_under_a_tiny_byte_limit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "live" / "store.sqlite"
    source.parent.mkdir(parents=True)
    con = sqlite3.connect(source)
    try:
        con.execute("CREATE TABLE messages (id TEXT, role TEXT, body TEXT)")
        con.execute("CREATE TABLE secrets (token TEXT)")
        con.execute("INSERT INTO messages VALUES ('m1','user','alpha')")
        con.execute("INSERT INTO messages VALUES ('m2','assistant','beta')")
        con.execute("INSERT INTO messages VALUES ('m3','user','gamma')")
        con.execute("INSERT INTO secrets VALUES ('credential-value')")
        con.commit()
    finally:
        con.close()

    artifact, blob = capture_sqlite(
        source,
        tmp_path / "capture",
        allowed_tables=("messages",),
        allowed_columns={"messages": ("id", "role", "body")},
        byte_limit=1,
        count_limit=1,
    )

    assert blob.is_file()
    assert artifact.byte_size == blob.stat().st_size
    con = sqlite3.connect(blob)
    try:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        rows = con.execute(
            "SELECT id, role, body FROM messages ORDER BY id"
        ).fetchall()
    finally:
        con.close()
    assert "messages" in tables
    assert "secrets" not in tables
    assert rows == [
        ("m1", "user", "alpha"),
        ("m2", "assistant", "beta"),
        ("m3", "user", "gamma"),
    ]


def test_capture_directory_streams_a_file_far_larger_than_one_chunk(
    tmp_path: Path,
) -> None:
    """8 MiB 文件（> 分块尺寸 1 MiB）必须被完整捕获，且不得整份驻留内存。

    分块尺寸是 1 MiB，所以 8 MiB 的源文件必然跨多次 ``read``；抓取结果必须
    与原文件逐字节一致，峰值已跟踪内存必须远小于文件本身。
    """
    payload = bytes(range(256)) * (BIG_FILE_BYTES // 256)
    assert len(payload) == BIG_FILE_BYTES
    source = tmp_path / "big"
    source.mkdir()
    (source / BIG_FILE_NAME).write_bytes(payload)

    tracemalloc.start()
    try:
        manifest, artifacts = capture_directory(
            source,
            tmp_path / "store",
            include_relative=(BIG_FILE_NAME,),
            byte_limit=1 << 20,  # 远小于文件：体积上限不影响抓取结果
            count_limit=1,
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # 捕获成功，artifact 数正确
    assert len(artifacts) == 1
    assert len(manifest.artifacts) == 1
    artifact = artifacts[0]
    assert artifact.relative_path == BIG_FILE_NAME

    # 字面量：8 MiB
    assert artifact.byte_size == 8388608
    blob = tmp_path / "store" / "artifacts" / artifact.content_hash[:32]
    assert blob.stat().st_size == 8388608

    # 两条不同路径：源字节的字面哈希 vs 落盘 blob 回读后的哈希
    assert hashlib.sha256(blob.read_bytes()).hexdigest() == hashlib.sha256(
        payload
    ).hexdigest()
    assert blob.read_bytes() == payload
    assert artifact.content_hash == hashlib.sha256(payload).hexdigest()

    # 流式：峰值已跟踪内存 < 文件的一半（整份 read_bytes 必然 >= 8 MiB）
    assert peak < 4 * 1024 * 1024

    # blob 校验（replay）也走流式路径，8 MiB blob 必须判为一致
    replayed = replay_manifest(manifest, tmp_path / "store" / "artifacts")
    assert replayed.ok is True
    assert replayed.missing == []
    assert replayed.mismatched == []
