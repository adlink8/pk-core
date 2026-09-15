"""Immutable local evaluation run registry (SQLite + JSON snapshots)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    dataset_checksum TEXT NOT NULL,
    config_checksum TEXT NOT NULL,
    scorer_version TEXT NOT NULL,
    top_k INTEGER NOT NULL,
    modes_json TEXT NOT NULL,
    status TEXT NOT NULL,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS eval_targets (
    run_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    collection TEXT,
    collection_checksum TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, target_id),
    FOREIGN KEY (run_id) REFERENCES eval_runs(run_id)
);
CREATE TABLE IF NOT EXISTS eval_metrics (
    run_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    PRIMARY KEY (run_id, mode),
    FOREIGN KEY (run_id) REFERENCES eval_runs(run_id)
);
CREATE TABLE IF NOT EXISTS eval_cases (
    run_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    query_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, mode, query_id),
    FOREIGN KEY (run_id) REFERENCES eval_runs(run_id)
);
CREATE TABLE IF NOT EXISTS eval_artifacts (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    checksum TEXT,
    PRIMARY KEY (run_id, name),
    FOREIGN KEY (run_id) REFERENCES eval_runs(run_id)
);
"""


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EvalRegistry:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        return con

    def _init(self) -> None:
        con = self._connect()
        con.executescript(SCHEMA)
        con.commit()
        con.close()

    def has_run(self, run_id: str) -> bool:
        con = self._connect()
        row = con.execute(
            "SELECT 1 FROM eval_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        con.close()
        return row is not None

    def create_run(
        self,
        run_id: str,
        *,
        dataset_checksum: str,
        config_checksum: str,
        scorer_version: str,
        top_k: int,
        modes: list[str],
        notes: str = "",
        overwrite: bool = False,
    ) -> None:
        if self.has_run(run_id) and not overwrite:
            raise FileExistsError(
                f"eval run {run_id} already exists (immutable; refuse overwrite)"
            )
        con = self._connect()
        if overwrite and self.has_run(run_id):
            # Only allowed for tests; production path never passes overwrite
            for table in (
                "eval_artifacts",
                "eval_cases",
                "eval_metrics",
                "eval_targets",
                "eval_runs",
            ):
                con.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
        con.execute(
            "INSERT INTO eval_runs(run_id, created_at, dataset_checksum, config_checksum, "
            "scorer_version, top_k, modes_json, status, notes) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                _utc(),
                dataset_checksum,
                config_checksum,
                scorer_version,
                top_k,
                json.dumps(modes, ensure_ascii=False),
                "running",
                notes,
            ),
        )
        con.commit()
        con.close()

    def add_target(self, run_id: str, target: Mapping[str, Any]) -> None:
        con = self._connect()
        con.execute(
            "INSERT INTO eval_targets(run_id, target_id, mode, collection, "
            "collection_checksum, payload_json) VALUES (?,?,?,?,?,?)",
            (
                run_id,
                target.get("target_id") or target.get("mode"),
                target["mode"],
                target.get("collection"),
                target.get("collection_checksum"),
                json.dumps(dict(target), ensure_ascii=False, sort_keys=True),
            ),
        )
        con.commit()
        con.close()

    def add_metrics(self, run_id: str, mode: str, metrics: Mapping[str, Any]) -> None:
        con = self._connect()
        # immutable: reject replace
        exists = con.execute(
            "SELECT 1 FROM eval_metrics WHERE run_id=? AND mode=?",
            (run_id, mode),
        ).fetchone()
        if exists:
            con.close()
            raise FileExistsError(f"metrics for {run_id}/{mode} already recorded")
        con.execute(
            "INSERT INTO eval_metrics(run_id, mode, metrics_json) VALUES (?,?,?)",
            (run_id, mode, json.dumps(dict(metrics), ensure_ascii=False, sort_keys=True)),
        )
        con.commit()
        con.close()

    def add_case_scores(
        self, run_id: str, mode: str, scores: list[Mapping[str, Any]]
    ) -> None:
        con = self._connect()
        for s in scores:
            qid = s["query_id"]
            con.execute(
                "INSERT INTO eval_cases(run_id, mode, query_id, payload_json) "
                "VALUES (?,?,?,?)",
                (
                    run_id,
                    mode,
                    qid,
                    json.dumps(dict(s), ensure_ascii=False, sort_keys=True),
                ),
            )
        con.commit()
        con.close()

    def add_artifact(
        self, run_id: str, name: str, path: str, checksum: str = ""
    ) -> None:
        con = self._connect()
        con.execute(
            "INSERT INTO eval_artifacts(run_id, name, path, checksum) VALUES (?,?,?,?)",
            (run_id, name, path, checksum),
        )
        con.commit()
        con.close()

    def finalize(self, run_id: str, status: str = "completed") -> None:
        con = self._connect()
        con.execute(
            "UPDATE eval_runs SET status=? WHERE run_id=?", (status, run_id)
        )
        con.commit()
        con.close()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        con = self._connect()
        row = con.execute(
            "SELECT * FROM eval_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not row:
            con.close()
            return None
        metrics = {
            r["mode"]: json.loads(r["metrics_json"])
            for r in con.execute(
                "SELECT mode, metrics_json FROM eval_metrics WHERE run_id=?",
                (run_id,),
            )
        }
        artifacts = {
            r["name"]: {"path": r["path"], "checksum": r["checksum"]}
            for r in con.execute(
                "SELECT name, path, checksum FROM eval_artifacts WHERE run_id=?",
                (run_id,),
            )
        }
        con.close()
        return {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "dataset_checksum": row["dataset_checksum"],
            "config_checksum": row["config_checksum"],
            "scorer_version": row["scorer_version"],
            "top_k": row["top_k"],
            "modes": json.loads(row["modes_json"]),
            "status": row["status"],
            "notes": row["notes"],
            "metrics": metrics,
            "artifacts": artifacts,
        }
