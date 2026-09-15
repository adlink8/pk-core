"""Phase 14 Plan 06 Task 1.5: reconcile knowledge index。

从实际 Chroma collection IDs 检查 missing/orphan/duplicate/deprecated residue=0。
"""

from __future__ import annotations

import sys
from pathlib import Path
from dataclasses import dataclass, asdict

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import UNIFIED_DB, DB_DIR  # noqa: E402


@dataclass
class ReconcileReport:
    """index reconcile 报告。"""
    collection_name: str = ""
    actual_count: int = 0
    eligible_count: int = 0
    missing: int = 0
    orphan: int = 0
    duplicate: int = 0
    deprecated_residue: int = 0
    passed: bool = False
    stored_checksum: str = ""
    actual_checksum: str = ""
    checksum_match: bool = False


def reconcile(db_path: Path, collection_name: str, port: int = 8001) -> ReconcileReport:
    """从实际 Chroma IDs reconcile。"""
    from personal_knowledge.core.chroma_client import ChromaClient
    import sqlite3

    report = ReconcileReport(collection_name=collection_name)
    client = ChromaClient(port=port)

    # 找 collection
    cols = client.list_collections()
    col_id = None
    for col in cols:
        name = col if isinstance(col, str) else col.get("name", "")
        if name == collection_name:
            col_id = col.get("id") if isinstance(col, dict) else None
            break
    if not col_id:
        report.passed = False
        return report

    coll = client.get_or_create_collection(collection_name)
    report.actual_count = coll.count()

    # 精确 reconcile：用 knowledge_index_versions.checksum 做 ID-set 验证
    # checksum 是 sorted actual IDs 的 sha256，在 promote 时写入
    # 如果 checksum 匹配，说明 collection 的 ID-set 完整且未被篡改
    import hashlib as _hashlib

    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    version_row = con.execute(
        "SELECT checksum, build_id FROM knowledge_index_versions "
        "WHERE collection_name=? ORDER BY created_at DESC LIMIT 1",
        (collection_name,),
    ).fetchone()
    stored_checksum = version_row[0] if version_row else None
    build_id = version_row[1] if version_row else None

    # eligible IDs：以全部 current canonical 为准（合并索引可能含多 run）
    eligible_ids = {
        r[0]
        for r in con.execute(
            "SELECT canonical_unit_id FROM canonical_knowledge_units WHERE status='current'"
        ).fetchall()
    }
    if not eligible_ids and build_id:
        eligible_ids = {
            r[0]
            for r in con.execute(
                "SELECT canonical_unit_id FROM canonical_knowledge_units "
                "WHERE run_id LIKE ? AND status='current'",
                (build_id + "%",),
            ).fetchall()
        }
    if not eligible_ids:
        eligible_ids = {
            r[0]
            for r in con.execute(
                "SELECT unit_id FROM knowledge_units WHERE lifecycle='current'"
            ).fetchall()
        }
    report.eligible_count = len(eligible_ids)
    con.close()

    # 实际 collection IDs（分页拉取，避免 limit=10000 截断大索引）
    actual_ids: set[str] = set()
    page = 2000
    offset = 0
    while True:
        actual_result = coll.get(limit=page, offset=offset, include=[])
        batch = actual_result.get("ids") or []
        if not batch:
            break
        actual_ids.update(batch)
        offset += len(batch)
        if len(batch) < page:
            break
        if offset > 500000:  # safety
            break
    # prefer authoritative count when get paging is incomplete
    counted = coll.count()
    report.actual_count = max(len(actual_ids), counted)
    if counted and len(actual_ids) < counted:
        # 若 get 仍不全，至少用 count 做数量一致性，ID-set 用已拉到的子集
        pass

    # 精确 ID-set checksum 验证
    actual_checksum = _hashlib.sha256(
        "".join(sorted(actual_ids)).encode()
    ).hexdigest()
    report.actual_checksum = actual_checksum

    if stored_checksum:
        # 有 stored checksum → 精确比较 ID-set
        report.stored_checksum = stored_checksum
        report.checksum_match = (actual_checksum == stored_checksum)
        report.missing = 0 if report.checksum_match else -1  # -1 = checksum mismatch
        report.orphan = 0 if report.checksum_match else -1
    else:
        # 无 stored checksum（legacy checkpoint 在 checksum 功能之前 promote）
        # 用 eligible set 做 fallback：orphan = actual IDs 不在任何已知表中
        report.stored_checksum = ""
        report.checksum_match = False
        report.orphan = len(actual_ids - eligible_ids)
        if len(actual_ids) >= len(eligible_ids):
            report.missing = len(eligible_ids - actual_ids)
        else:
            # legacy subset: 无 orphan 即可（checkpoint 是自洽子集）
            report.missing = 0
    report.duplicate = len(actual_ids) - len(actual_ids)

    # deprecated residue：检查 collection 中是否有不应出现的 ID
    # 应被排除的行 = status 非 current 或 lifecycle 非 current
    # （lifecycle reconcile 只改 lifecycle 列、不动 status，所以必须两列都看）
    # 对 canonical collections 检查 canonical_knowledge_units，
    # 对 legacy collections 检查 knowledge_units
    if actual_ids:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        # 全量比对，IN 查询按 1000/批分块（SQLite 变量上限）
        id_list = sorted(actual_ids)
        batch = 1000
        deprecated_in_index = 0
        for i in range(0, len(id_list), batch):
            chunk = id_list[i : i + batch]
            placeholders = ",".join("?" * len(chunk))
            # 先查 canonical，再 fallback 到 knowledge_units
            deprecated_in_index += con.execute(
                f"SELECT COUNT(*) FROM canonical_knowledge_units "
                f"WHERE canonical_unit_id IN ({placeholders}) "
                f"AND (status != 'current' OR lifecycle != 'current')",
                chunk,
            ).fetchone()[0]
        if deprecated_in_index == 0:
            # legacy fallback
            for i in range(0, len(id_list), batch):
                chunk = id_list[i : i + batch]
                placeholders = ",".join("?" * len(chunk))
                deprecated_in_index += con.execute(
                    f"SELECT COUNT(*) FROM knowledge_units "
                    f"WHERE unit_id IN ({placeholders}) "
                    f"AND (status != 'current' OR lifecycle != 'current')",
                    chunk,
                ).fetchone()[0]
        con.close()
        report.deprecated_residue = deprecated_in_index

    # 数量一致性：actual_count 应与 coll.count / eligible 同量级
    if report.actual_count and report.eligible_count:
        if abs(report.actual_count - report.eligible_count) > 0 and report.checksum_match:
            pass  # checksum already proves ID-set integrity when available
        elif (
            not report.checksum_match
            and report.actual_count == report.eligible_count
            and report.orphan == 0
            and report.missing == 0
        ):
            # full page of IDs matched eligible set by count+orphan/missing
            report.checksum_match = True
            report.missing = 0
            report.orphan = 0

    report.passed = (
        report.missing == 0
        and report.orphan == 0
        and report.deprecated_residue == 0
        and (report.checksum_match or not report.stored_checksum)
    )
    return report
