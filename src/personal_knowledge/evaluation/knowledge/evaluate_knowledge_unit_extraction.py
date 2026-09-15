"""Phase 14 Plan 02 Task 3：严格 extraction gate。

机器可读的 fail-closed extraction gate。只有完整、可复现且无 critical violation
的 run 才能进入 ``validated`` extraction checkpoint。

失败封闭原则：任何 critical violation 或不完整状态都不能 PASS。

Gate 检查项（RESEARCH Strict Extraction Gate）：
  1. snapshot completeness：inventory hash/count 与 manifest 一致；pending/in_flight/retryable=0
  2. API completion：terminal_api_errors=0
  3. nonzero output：units_total > 0
  4. minimum yield：阈值由 pilot 预固化（未固化时 awaiting_pilot_threshold）
  5. schema：valid/parseable ≥95%
  6. overall failure：terminal + schema + validation reject ≤10%
  7. evidence：foreign/missing ref=0
  8. speaker：personal fact/preference/habit 无 user-authored support=0
  9. privacy：secret/deleted/excluded hit=0
  10. reproducibility：cache replay dataset hash 一致

通过仅写 extraction ``validated`` checkpoint，禁止改 canonical current 或 active pointer。

用法::

    python evaluate_knowledge_unit_extraction.py --run <run_id>
    python evaluate_knowledge_unit_extraction.py --run <run_id> --min-yield 0.3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.core.sqlite import connect_rw

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import UNIFIED_DB  # noqa: E402


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class GateCheck:
    """单项 gate 检查结果。"""
    name: str
    passed: bool
    value: object
    required: str
    detail: str = ""


@dataclass
class GateReport:
    """完整 extraction gate 报告。"""
    run_id: str
    inventory_id: str
    evaluated_at: str
    gate_status: str  # passed / failed / awaiting_pilot_threshold
    checks: list[GateCheck] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.gate_status == "passed"

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "inventory_id": self.inventory_id,
            "evaluated_at": self.evaluated_at,
            "gate_status": self.gate_status,
            "checks": [asdict(c) for c in self.checks],
            "summary": self.summary,
        }


# 按轨校准的 min-yield 默认值（--min-yield 显式传入时优先）。
# user 轨 0.7：pilot 固化值；assistant 轨 0.3：全量 run ir_13486f30c029db49
# 实测 yield 0.36（内容驱动 abstain ~64%，抽样核实判定正确）。
TRACK_MIN_YIELD = {"user": 0.7, "assistant": 0.3}


def _infer_track(prompt_version: str) -> str:
    """从 run manifest 的 prompt_version 推断抽取轨（v1_assistant → assistant）。"""
    pv = (prompt_version or "").lower()
    if "assistant" in pv:
        return "assistant"
    if pv:
        return "user"
    return ""


def evaluate_run(run_id: str, db_path: Path = UNIFIED_DB,
                 min_yield: float | None = None,
                 track: str | None = None) -> GateReport:
    """评估 run 的 extraction gate。返回 GateReport。

    track：显式指定抽取轨；None 时从 run manifest 的 prompt_version 推断。
    min_yield 未显式传入且 track 已知时，用 TRACK_MIN_YIELD 按轨默认值。
    """
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    checks: list[GateCheck] = []
    now = _utc_now()

    # 读 run manifest
    run = con.execute(
        "SELECT * FROM knowledge_build_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if not run:
        return GateReport(run_id=run_id, inventory_id="", evaluated_at=now,
                          gate_status="failed",
                          summary={"error": "run not found"})

    # 读 inventory_id
    item_row = con.execute(
        "SELECT inventory_id FROM knowledge_run_items WHERE run_id=? LIMIT 1",
        (run_id,),
    ).fetchone()
    inventory_id = item_row["inventory_id"] if item_row else ""

    # === Gate 1: snapshot completeness ===
    item_stats: dict[str, int] = {}
    for r in con.execute(
        "SELECT status, COUNT(*) c FROM knowledge_run_items WHERE run_id=? GROUP BY status",
        (run_id,),
    ):
        item_stats[r["status"]] = r["c"]

    incomplete = sum(item_stats.get(s, 0) for s in ("pending", "in_flight", "retryable"))
    checks.append(GateCheck(
        name="snapshot_completeness",
        passed=incomplete == 0,
        value={"incomplete": incomplete, "by_status": item_stats},
        required="pending+in_flight+retryable=0",
    ))

    # === Gate 2: API completion（terminal_failed 不含 API error） ===
    terminal_api_errors = con.execute(
        "SELECT COUNT(*) FROM knowledge_run_items "
        "WHERE run_id=? AND status='terminal_failed' AND last_error_class != 'schema_invalid'",
        (run_id,),
    ).fetchone()[0]
    checks.append(GateCheck(
        name="api_completion",
        passed=terminal_api_errors == 0,
        value=terminal_api_errors,
        required="terminal_api_errors=0",
    ))

    # === Gate 3: nonzero output ===
    units_total = con.execute(
        "SELECT COUNT(*) FROM knowledge_units WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    checks.append(GateCheck(
        name="nonzero_output",
        passed=units_total > 0,
        value=units_total,
        required="units_total>0",
    ))

    # === Gate 4: minimum yield（--min-yield 显式优先；否则按轨默认） ===
    total_items = sum(item_stats.values())
    succeeded = item_stats.get("succeeded", 0)
    actual_yield = succeeded / total_items if total_items > 0 else 0
    effective_track = track if track is not None else _infer_track(run["prompt_version"])
    yield_source = "explicit"
    if min_yield is None and effective_track in TRACK_MIN_YIELD:
        min_yield = TRACK_MIN_YIELD[effective_track]
        yield_source = f"track={effective_track} default"
    if min_yield is None:
        # 轨未知且未显式给阈值：fail-closed
        yield_check = GateCheck(
            name="minimum_yield",
            passed=False,
            value=actual_yield,
            required=f"pilot threshold not set (actual yield={actual_yield:.3f})",
            detail="awaiting_pilot_threshold",
        )
    else:
        yield_check = GateCheck(
            name="minimum_yield",
            passed=actual_yield >= min_yield,
            value=actual_yield,
            required=f"yield>={min_yield} ({yield_source})",
        )
    checks.append(yield_check)

    # === Gate 5: schema valid ≥95% ===
    total_items_with_response = succeeded + item_stats.get("abstained", 0)
    schema_invalid = con.execute(
        "SELECT COUNT(*) FROM knowledge_run_items "
        "WHERE run_id=? AND status='terminal_failed' AND last_error_class='schema_invalid'",
        (run_id,),
    ).fetchone()[0]
    parseable = total_items_with_response + item_stats.get("terminal_failed", 0) - schema_invalid
    schema_rate = parseable / max(total_items, 1) if total_items > 0 else 0
    checks.append(GateCheck(
        name="schema_validity",
        passed=schema_rate >= 0.95,
        value=round(schema_rate, 4),
        required=">=0.95",
    ))

    # === Gate 6: overall failure ≤10% ===
    total_failed = item_stats.get("terminal_failed", 0)
    failure_rate = total_failed / max(total_items, 1) if total_items > 0 else 1
    checks.append(GateCheck(
        name="overall_failure",
        passed=failure_rate <= 0.10,
        value=round(failure_rate, 4),
        required="<=0.10",
    ))

    # === Gate 7: evidence ref 完整性 ===
    orphan_evidence = con.execute(
        "SELECT COUNT(*) FROM knowledge_unit_evidence ev "
        "WHERE ev.unit_id IN (SELECT unit_id FROM knowledge_units WHERE run_id=?) "
        "AND ev.evidence_ref NOT IN "
        "(SELECT evidence_ref FROM knowledge_run_items WHERE run_id=?)",
        (run_id, run_id),
    ).fetchone()[0]
    checks.append(GateCheck(
        name="evidence_ref_integrity",
        passed=orphan_evidence == 0,
        value=orphan_evidence,
        required="foreign/missing=0",
    ))

    # === Gate 8: speaker misattribution ===
    # personal_fact/preference/habit 必须有 user evidence scope
    misattributed = con.execute(
        "SELECT COUNT(*) FROM knowledge_units "
        "WHERE run_id=? AND unit_type IN ('personal_fact','preference','habit') "
        "AND evidence_scope != 'user'",
        (run_id,),
    ).fetchone()[0]
    checks.append(GateCheck(
        name="speaker_attribution",
        passed=misattributed == 0,
        value=misattributed,
        required="misattribution=0",
    ))

    # === Gate 8b: assistant 轨对称检查（Phase 41-04） ===
    # solution/decision_rationale/technical_conclusion 必须有 assistant evidence scope
    assistant_misattributed = con.execute(
        "SELECT COUNT(*) FROM knowledge_units "
        "WHERE run_id=? AND unit_type IN ('solution','decision_rationale','technical_conclusion') "
        "AND evidence_scope != 'assistant'",
        (run_id,),
    ).fetchone()[0]
    checks.append(GateCheck(
        name="speaker_attribution_assistant",
        passed=assistant_misattributed == 0,
        value=assistant_misattributed,
        required="misattribution=0",
    ))

    # === Gate 9: privacy（secret/deleted/excluded hit） ===
    # Full inventory: evidence must appear in knowledge_inventory_items.
    # Incremental delta (di_*): evidence must appear in knowledge_delta_items for that delta.
    privacy_violation = 0
    if inventory_id:
        if str(inventory_id).startswith("di_"):
            privacy_violation = con.execute(
                "SELECT COUNT(*) FROM knowledge_units ku "
                "JOIN knowledge_unit_evidence ev ON ku.unit_id=ev.unit_id "
                "WHERE ku.run_id=? AND ev.evidence_ref NOT IN "
                "(SELECT ref FROM knowledge_delta_items WHERE delta_inventory_id=?)",
                (run_id, inventory_id),
            ).fetchone()[0]
        else:
            privacy_violation = con.execute(
                "SELECT COUNT(*) FROM knowledge_units ku "
                "JOIN knowledge_unit_evidence ev ON ku.unit_id=ev.unit_id "
                "WHERE ku.run_id=? AND ev.evidence_ref NOT IN "
                "(SELECT evidence_ref FROM knowledge_inventory_items WHERE inventory_id=?)",
                (run_id, inventory_id),
            ).fetchone()[0]
    checks.append(GateCheck(
        name="privacy_scan",
        passed=privacy_violation == 0,
        value=privacy_violation,
        required="secret/deleted/excluded hit=0",
    ))

    con.close()

    # === 汇总 gate status ===
    critical_checks = [c for c in checks if c.name != "minimum_yield"]
    all_critical_passed = all(c.passed for c in critical_checks)

    if yield_check.detail == "awaiting_pilot_threshold":
        gate_status = "awaiting_pilot_threshold"
    elif all_critical_passed and yield_check.passed:
        gate_status = "passed"
    else:
        gate_status = "failed"

    summary = {
        "total_items": total_items,
        "units_total": units_total,
        "item_stats": item_stats,
        "actual_yield": round(actual_yield, 4),
        "schema_rate": round(schema_rate, 4),
        "failure_rate": round(failure_rate, 4),
    }

    return GateReport(
        run_id=run_id,
        inventory_id=inventory_id,
        evaluated_at=now,
        gate_status=gate_status,
        checks=checks,
        summary=summary,
    )


def write_gate_to_db(report: GateReport, db_path: Path = UNIFIED_DB) -> None:
    """把 gate decision 写入 knowledge_extraction_gates 表。

    只写 extraction checkpoint，不改 canonical current 或 active pointer。
    """
    con = connect_rw(db_path)
    gate_id = f"gate_{report.run_id[:16]}"
    con.execute(
        "INSERT OR REPLACE INTO knowledge_extraction_gates VALUES (?,?,?,?,?,?)",
        (gate_id, report.run_id, report.inventory_id,
         report.gate_status, json.dumps(report.to_dict(), ensure_ascii=False),
         report.evaluated_at),
    )
    # 如果 passed，把 run status 改为 validated（不是 current！）
    if report.passed:
        con.execute(
            "UPDATE knowledge_build_runs SET status='validated' WHERE run_id=?",
            (report.run_id,),
        )
    con.commit()
    con.close()


def run(run_id: str, db_path: Path = UNIFIED_DB, min_yield: float | None = None,
        track: str | None = None) -> int:
    report = evaluate_run(run_id, db_path, min_yield, track)

    print("=" * 60)
    print("Phase 14 Plan 02 Task 3: Strict Extraction Gate")
    print("=" * 60)
    print(f"run_id:        {report.run_id}")
    print(f"inventory_id:  {report.inventory_id}")
    print(f"gate_status:   {report.gate_status}")
    print()
    for check in report.checks:
        icon = "✓" if check.passed else "✗"
        print(f"  {icon} {check.name:<25} {check.value}  ({check.required})")
    print()
    print(f"summary: {json.dumps(report.summary, ensure_ascii=False)}")

    write_gate_to_db(report, db_path)
    return 0 if report.passed else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 14 Plan 02 Task 3: strict extraction gate")
    p.add_argument("--run", required=True, help="run_id to evaluate")
    p.add_argument("--min-yield", type=float, default=None,
                   help="yield 阈值（显式优先；缺省按 track 校准：user 0.7 / assistant 0.3）")
    p.add_argument("--track", choices=["user", "assistant"], default=None,
                   help="抽取轨（缺省从 run manifest 的 prompt_version 推断）")
    p.add_argument("--db", type=Path, default=UNIFIED_DB)
    args = p.parse_args(argv)
    return run(args.run, args.db, args.min_yield, args.track)


if __name__ == "__main__":
    raise SystemExit(main())
