"""Phase 14 Wave 1.2：run manifest 和 staging publish helper。

所有知识单元操作（抽取、合并、索引）都通过这个 helper 记录 run manifest、
写入 staging、通过 gate 后 promote status。失败 run 不清空旧 current rows。

核心契约（AI-SPEC）：
  - manifest 记录输入 dataset hash、source counts/time range、prompt/schema/model/embedding/config/git SHA
  - 新 run 先写 staging（status='staging'）；只有 gate 通过才 promote status='current'
  - 失败 run 不清空旧 current rows；修复"先删除旧候选再调用 LLM"的同类风险
  - 提供 checkpoint rollback 和 exact table/index reconciliation helper

用法::

    from personal_knowledge.application.knowledge.knowledge_unit_pipeline import RunManifest, StagingPublisher

    manifest = RunManifest.create(run_type='extraction', ...)
    publisher = StagingPublisher(manifest, db_path=UNIFIED_DB)
    publisher.begin_staging()
    ...  # 写 staging rows
    if gate_passed:
        publisher.promote()
    else:
        publisher.abort()
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.core.sqlite import assert_foreign_key_integrity, connect_rw

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import UNIFIED_DB  # noqa: E402


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(data: object) -> str:
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False) if not isinstance(data, str) else data
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class RunManifest:
    """一次知识单元操作的版本合同。"""
    run_id: str
    run_type: str           # extraction / merge / index / promote
    generated_at: str
    source_build_id: str    # canonical conversation build ID
    input_hash: str         # 输入 dataset hash
    prompt_version: str
    schema_version: str     # v1
    model: str
    embedding_model: str
    config_hash: str
    git_sha: str
    status: str = "staging" # staging / current / blocked / aborted
    dataset_hash: str = ""  # 输出内容 hash（promote 时填）
    stats_json: str = ""
    supersedes_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def create(
        cls,
        run_type: str,
        source_build_id: str,
        input_data: object,
        prompt_version: str = "",
        model: str = "",
        embedding_model: str = "",
        config: dict | None = None,
        git_sha: str = "",
        supersedes_id: str = "",
    ) -> "RunManifest":
        """创建新 manifest。run_id 由输入 hash 派生。"""
        input_hash = _hash(input_data)
        run_id = _hash(f"{run_type}|{source_build_id}|{input_hash}")
        config_hash = _hash(config or {})
        return cls(
            run_id=run_id,
            run_type=run_type,
            generated_at=_utc_now(),
            source_build_id=source_build_id,
            input_hash=input_hash,
            prompt_version=prompt_version,
            schema_version="v1",
            model=model,
            embedding_model=embedding_model,
            config_hash=config_hash,
            git_sha=git_sha,
            supersedes_id=supersedes_id,
        )


class StagingPublisher:
    """staging → gate → promote 管理器。

    流程：
      1. begin_staging()：在 knowledge_build_runs 写 status='staging' 的 manifest
      2. （调用方写 staging rows，status='staging'）
      3. promote()：gate 通过后，把当前 run 的 units 从 staging → current，
         同时把同 pass 族（unit_id 前缀相同）的旧 current units 降级回 staging
      4. abort()：gate 失败，标记 status='aborted'，不清旧 current
    """

    def __init__(self, manifest: RunManifest, db_path: Path = UNIFIED_DB) -> None:
        self.manifest = manifest
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        return connect_rw(self.db_path)

    def begin_staging(self) -> None:
        """在 DB 写入 manifest 行，status='staging'。

        幂等：同 run_id 重跑时，先清除该 run 的旧 units（避免残留）。
        """
        con = self._connect()
        try:
            # 清除同 run_id 的旧 units（幂等重跑安全）
            con.execute(
                "DELETE FROM knowledge_unit_evidence WHERE unit_id IN "
                "(SELECT unit_id FROM knowledge_units WHERE run_id=?)",
                (self.manifest.run_id,),
            )
            con.execute(
                "DELETE FROM knowledge_units WHERE run_id=?",
                (self.manifest.run_id,),
            )
            con.execute(
                "INSERT OR REPLACE INTO knowledge_build_runs VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.manifest.run_id, self.manifest.run_type,
                    self.manifest.generated_at, self.manifest.source_build_id,
                    self.manifest.input_hash, self.manifest.prompt_version,
                    self.manifest.schema_version, self.manifest.model,
                    self.manifest.embedding_model, self.manifest.config_hash,
                    self.manifest.git_sha, self.manifest.dataset_hash,
                    self.manifest.status, self.manifest.stats_json,
                    self.manifest.supersedes_id,
                ),
            )
            con.commit()
        finally:
            con.close()

    def promote(self, dataset_hash: str = "", stats: dict | None = None) -> None:
        """gate 通过：manifest → current，同 pass 族的旧 current units → staging。

        demote 范围按 unit_id 前缀（v1|/l2|/ku| 等 pass 族）收窄：
        只降级与本 run units 前缀相同的旧 current units，不触碰其他 pass 族
        （如 L2 的 l2| current）。被降级行保持 supersedes_id 原值，不自引用。

        注意：ku| 旧世代 units 不被任何新 run 的 promote 触碰——这是刻意的，
        其清理由独立的数据迁移任务处理。
        """
        con = self._connect()
        try:
            assert_foreign_key_integrity(con)
            # 本 run units 的 pass 族前缀（v1|/l2|/ku| 等）
            prefixes = [
                row[0]
                for row in con.execute(
                    "SELECT DISTINCT substr(unit_id,1,3) FROM knowledge_units WHERE run_id=?",
                    (self.manifest.run_id,),
                )
            ]
            if prefixes:
                # 旧 current units → staging（同族新一代接任），不改 supersedes_id
                placeholders = ",".join("?" for _ in prefixes)
                con.execute(
                    "UPDATE knowledge_units SET status='staging' "
                    "WHERE status='current' AND run_id != ? "
                    f"AND substr(unit_id,1,3) IN ({placeholders})",
                    (self.manifest.run_id, *prefixes),
                )
            # 当前 run 的 units → current
            con.execute(
                "UPDATE knowledge_units SET status='current' WHERE run_id=?",
                (self.manifest.run_id,),
            )
            # manifest → current
            con.execute(
                "UPDATE knowledge_build_runs SET status='current', "
                "dataset_hash=? WHERE run_id=?",
                (dataset_hash or self.manifest.dataset_hash, self.manifest.run_id),
            )
            if stats:
                con.execute(
                    "UPDATE knowledge_build_runs SET stats_json=? WHERE run_id=?",
                    (json.dumps(stats, ensure_ascii=False), self.manifest.run_id),
                )
            con.commit()
        finally:
            con.close()

    def abort(self, reason: str = "") -> None:
        """gate 失败：manifest → aborted，不清旧 current。"""
        con = self._connect()
        try:
            con.execute(
                "UPDATE knowledge_build_runs SET status='aborted' WHERE run_id=?",
                (self.manifest.run_id,),
            )
            # staging units → rejected
            con.execute(
                "UPDATE knowledge_units SET status='rejected' WHERE run_id=?",
                (self.manifest.run_id,),
            )
            if reason:
                existing = con.execute(
                    "SELECT stats_json FROM knowledge_build_runs WHERE run_id=?",
                    (self.manifest.run_id,),
                ).fetchone()[0]
                stats = json.loads(existing) if existing else {}
                stats["abort_reason"] = reason
                con.execute(
                    "UPDATE knowledge_build_runs SET stats_json=? WHERE run_id=?",
                    (json.dumps(stats, ensure_ascii=False), self.manifest.run_id),
                )
            con.commit()
        finally:
            con.close()

    def checkpoint_rollback(self, to_run_id: str) -> dict:
        """回滚到指定 run：恢复旧 run 的 current status，撤销当前 run。

        返回操作摘要。不删除行，只改 status。
        """
        con = self._connect()
        try:
            # 当前 run 的 units → rejected
            con.execute(
                "UPDATE knowledge_units SET status='rejected' WHERE run_id=?",
                (self.manifest.run_id,),
            )
            # 目标 run 的 units → current
            con.execute(
                "UPDATE knowledge_units SET status='current' WHERE run_id=?",
                (to_run_id,),
            )
            # manifest 状态
            con.execute(
                "UPDATE knowledge_build_runs SET status='aborted' WHERE run_id=?",
                (self.manifest.run_id,),
            )
            con.execute(
                "UPDATE knowledge_build_runs SET status='current' WHERE run_id=?",
                (to_run_id,),
            )
            con.commit()
            return {"rolled_back_from": self.manifest.run_id, "rolled_back_to": to_run_id}
        finally:
            con.close()

    def table_reconciliation(self) -> dict:
        """exact table reconciliation：检查 staging/current/rejected 计数。"""
        con = self._connect()
        try:
            counts = {}
            for status in ("staging", "current", "rejected"):
                counts[status] = con.execute(
                    "SELECT COUNT(*) FROM knowledge_units WHERE run_id=? AND status=?",
                    (self.manifest.run_id, status),
                ).fetchone()[0]
            # evidence ref 完整性
            orphan_evidence = con.execute(
                "SELECT COUNT(*) FROM knowledge_unit_evidence ev "
                "WHERE ev.unit_id NOT IN (SELECT unit_id FROM knowledge_units WHERE run_id=?)",
                (self.manifest.run_id,),
            ).fetchone()[0]
            counts["orphan_evidence"] = orphan_evidence
            return counts
        finally:
            con.close()
