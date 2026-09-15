"""Phase 16: aggregate Google light assertions (NOT dialogue knowledge units).

Privacy policy (service **and** category/content — see 15-16-AUDIT / retrieval-ssot):
  - restricted services: Maps (and service names containing 地图)
  - restricted category substrings: payment/finance (支付/金融/卡) **and**
    location intent (地图/地点/位置/导航) even when the activity is Search/Gemini
  - aggregate_ok: other Search / YouTube / Gemini / AI Mode theme & frequency signals
  - restricted activities may still enter normalized_events; they do not form
    interest_topic / frequent_service / channel / domain assertions

Phase 16-02 lifecycle:
  - write stages assertions (status=staging, run_id)
  - privacy/reconcile gate
  - promote staging → current (previous current → superseded)
  - abort leaves current untouched

Usage::

    python src/personal_knowledge/application/build_google_light_assertions.py --dry-run
    python src/personal_knowledge/application/build_google_light_assertions.py --write
    python src/personal_knowledge/application/build_google_light_assertions.py --rollback
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.core.project_paths import ROOT, AI_CONTEXT_DIR  # noqa: E402
from personal_knowledge.application.google_structure_lifecycle import (  # noqa: E402
    PRIVACY_POLICY_VERSION,
    current_run_id,
    dataset_hash_ids,
    ensure_lifecycle_tables,
    input_hash_from_activities,
    insert_run,
    is_restricted,
    log_promote,
    make_run_id,
    privacy_gate_assertions,
    sha256_text,
    utc_now,
    write_active_pointer,
)

GOOGLE_DB = ROOT / "Google" / "structured" / "db" / "google_data.sqlite"
REPORT_PATH = AI_CONTEXT_DIR / "google_light_structure_report.json"
ACTIVE_POINTER = (
    ROOT / "Google" / "structured" / "db" / "google_structure_active_run.txt"
)
ASSERT_CONFIG_HASH = sha256_text(
    f"light_assertions|privacy={PRIVACY_POLICY_VERSION}|stage_promote_v1"
)
MIN_TOPIC = 5
MIN_CHANNEL = 3
MIN_DOMAIN = 3


@dataclass
class AssertStats:
    activities_scanned: int = 0
    eligible_for_assert: int = 0
    restricted_skipped: int = 0
    assertions: int = 0
    by_type: dict = field(default_factory=dict)
    dry_run: bool = True
    normalized_events: int = 0
    run_id: str = ""
    input_hash: str = ""
    dataset_hash: str = ""
    gate_passed: bool | None = None
    promoted: bool = False
    supersedes_run_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS google_light_assertions (
    assertion_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    assertion_type TEXT NOT NULL,
    subject TEXT NOT NULL,
    claim TEXT NOT NULL,
    evidence_count INTEGER NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    services_json TEXT,
    categories_json TEXT,
    privacy_tier TEXT NOT NULL DEFAULT 'aggregate_ok',
    status TEXT NOT NULL DEFAULT 'current',
    created_at TEXT NOT NULL,
    PRIMARY KEY (assertion_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_gla_type ON google_light_assertions(assertion_type);
CREATE INDEX IF NOT EXISTS idx_gla_status ON google_light_assertions(status);
CREATE INDEX IF NOT EXISTS idx_gla_run ON google_light_assertions(run_id);
"""


def _is_restricted(service: str, category: str) -> bool:
    """Public alias for tests / callers."""
    return is_restricted(service, category)


def _aid(kind: str, subject: str) -> str:
    h = hashlib.sha256(f"{kind}|{subject}".encode("utf-8")).hexdigest()[:20]
    return f"gla|{kind}|{h}"


def _event_id_for_activity(con: sqlite3.Connection, activity_id: int) -> str | None:
    row = con.execute(
        "SELECT event_id FROM normalized_events WHERE source_file_id=?",
        (str(activity_id),),
    ).fetchone()
    return row[0] if row else None


def _ensure_assertion_schema(con: sqlite3.Connection) -> None:
    """Migrate legacy PK(assertion_id) → composite (assertion_id, run_id)."""
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='google_light_assertions'"
    ).fetchone()
    if not row:
        con.executescript(SCHEMA_V2)
        return
    cols = {r[1]: r for r in con.execute("PRAGMA table_info(google_light_assertions)")}
    pk_cols = [r[1] for r in con.execute("PRAGMA table_info(google_light_assertions)") if r[5]]
    if "run_id" in cols and pk_cols == ["assertion_id", "run_id"]:
        return
    # Rebuild table with composite PK; preserve existing current rows
    con.execute("ALTER TABLE google_light_assertions RENAME TO google_light_assertions_legacy")
    con.executescript(SCHEMA_V2)
    legacy_cols = {r[1] for r in con.execute("PRAGMA table_info(google_light_assertions_legacy)")}
    has_run = "run_id" in legacy_cols
    if has_run:
        con.execute(
            "INSERT INTO google_light_assertions "
            "(assertion_id, run_id, assertion_type, subject, claim, evidence_count, "
            "evidence_refs_json, services_json, categories_json, privacy_tier, status, created_at) "
            "SELECT assertion_id, COALESCE(NULLIF(run_id,''), 'legacy'), assertion_type, subject, claim, "
            "evidence_count, evidence_refs_json, services_json, categories_json, privacy_tier, "
            "status, created_at FROM google_light_assertions_legacy"
        )
    else:
        con.execute(
            "INSERT INTO google_light_assertions "
            "(assertion_id, run_id, assertion_type, subject, claim, evidence_count, "
            "evidence_refs_json, services_json, categories_json, privacy_tier, status, created_at) "
            "SELECT assertion_id, 'legacy', assertion_type, subject, claim, "
            "evidence_count, evidence_refs_json, services_json, categories_json, privacy_tier, "
            "status, created_at FROM google_light_assertions_legacy"
        )
    con.execute("DROP TABLE google_light_assertions_legacy")


def build(
    db_path: Path = GOOGLE_DB,
    write: bool = False,
    active_pointer_path: Path | None = None,
) -> tuple[AssertStats, list[dict]]:
    stats = AssertStats(dry_run=not write)
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    if active_pointer_path is None:
        active_pointer_path = (
            ACTIVE_POINTER
            if db_path.resolve() == GOOGLE_DB.resolve()
            else db_path.with_name("google_structure_active_run.txt")
        )

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    _ensure_assertion_schema(con)
    ensure_lifecycle_tables(con)
    try:
        stats.normalized_events = con.execute(
            "SELECT COUNT(*) FROM normalized_events"
        ).fetchone()[0]
    except Exception:
        stats.normalized_events = 0

    rows = con.execute(
        "SELECT id, service, category, title_or_query, channel_or_source, domain "
        "FROM activities"
    ).fetchall()
    stats.activities_scanned = len(rows)
    stats.input_hash = input_hash_from_activities(
        [(r["id"], r["service"], r["title_or_query"], r["category"]) for r in rows]
    )
    prev = current_run_id(con, "light_assertions")
    stats.supersedes_run_id = prev
    # Unique run_id per write so stage rows never clobber current PK (assertion_id, run_id).
    stats.run_id = make_run_id(
        "light_assertions", stats.input_hash, ASSERT_CONFIG_HASH + "|" + utc_now()
    )

    topic_refs: dict[str, list[str]] = defaultdict(list)
    service_refs: dict[str, list[str]] = defaultdict(list)
    channel_refs: dict[str, list[str]] = defaultdict(list)
    domain_refs: dict[str, list[str]] = defaultdict(list)
    topic_services: dict[str, set[str]] = defaultdict(set)

    for r in rows:
        service = r["service"] or ""
        category = r["category"] or ""
        if _is_restricted(service, category):
            stats.restricted_skipped += 1
            continue
        stats.eligible_for_assert += 1
        eid = _event_id_for_activity(con, int(r["id"]))
        if not eid:
            raw = f"{service}|{r['title_or_query'] or ''}|{r['id']}"
            eid = "g|" + hashlib.sha256(raw.encode()).hexdigest()[:24]

        if category:
            topic_refs[category].append(eid)
            topic_services[category].add(service)
        if service:
            service_refs[service].append(eid)
        ch = (r["channel_or_source"] or "").strip()
        if service == "YouTube" and ch:
            channel_refs[ch].append(eid)
        dom = (r["domain"] or "").strip().lower()
        if dom and dom not in ("google.com", "youtube.com", "www.google.com"):
            domain_refs[dom].append(eid)

    now = utc_now()
    assertions: list[dict] = []

    def add(atype: str, subject: str, claim: str, refs: list[str], services=None, cats=None, tier="aggregate_ok"):
        refs_u = list(dict.fromkeys(refs))[:50]
        assertions.append(
            {
                "assertion_id": _aid(atype, subject),
                "assertion_type": atype,
                "subject": subject[:200],
                "claim": claim[:500],
                "evidence_count": len(refs),
                "evidence_refs_json": json.dumps(refs_u, ensure_ascii=False),
                "services_json": json.dumps(sorted(services or []), ensure_ascii=False),
                "categories_json": json.dumps(sorted(cats or []), ensure_ascii=False),
                "privacy_tier": tier,
                "status": "staging",
                "created_at": now,
                "run_id": stats.run_id,
            }
        )

    for cat, refs in sorted(topic_refs.items(), key=lambda x: -len(x[1])):
        if len(refs) < MIN_TOPIC:
            continue
        add(
            "interest_topic",
            cat,
            f"用户在 Google 活动中频繁涉及主题「{cat}」（约 {len(refs)} 次，聚合信号，非对话断言）",
            refs,
            services=topic_services.get(cat),
            cats=[cat],
        )

    for svc, refs in sorted(service_refs.items(), key=lambda x: -len(x[1])):
        if len(refs) < MIN_TOPIC:
            continue
        add(
            "frequent_service",
            svc,
            f"用户常用 Google 服务「{svc}」（约 {len(refs)} 条活动）",
            refs,
            services=[svc],
        )

    for ch, refs in sorted(channel_refs.items(), key=lambda x: -len(x[1])):
        if len(refs) < MIN_CHANNEL:
            continue
        add(
            "frequent_channel",
            ch,
            f"用户在 YouTube 上多次观看/互动频道「{ch}」（约 {len(refs)} 次）",
            refs,
            services=["YouTube"],
        )

    for dom, refs in sorted(domain_refs.items(), key=lambda x: -len(x[1])):
        if len(refs) < MIN_DOMAIN:
            continue
        add(
            "domain_affinity",
            dom,
            f"用户活动中反复出现域名「{dom}」（约 {len(refs)} 次）",
            refs,
        )

    stats.assertions = len(assertions)
    stats.by_type = {}
    for a in assertions:
        stats.by_type[a["assertion_type"]] = stats.by_type.get(a["assertion_type"], 0) + 1

    stats.dataset_hash = dataset_hash_ids([a["assertion_id"] for a in assertions])
    gate = privacy_gate_assertions(assertions)
    # Floor: do not promote empty accidental wipe
    if stats.assertions == 0 and stats.eligible_for_assert > 0:
        gate = {
            **gate,
            "passed": False,
            "violations": gate.get("violations", [])
            + [{"reason": "zero_assertions_with_eligible_activities"}],
        }
    stats.gate_passed = bool(gate.get("passed"))

    if write:
        # Clear only this run's staging leftovers
        con.execute(
            "DELETE FROM google_light_assertions WHERE status='staging' AND run_id=?",
            (stats.run_id,),
        )
        if assertions:
            con.executemany(
                "INSERT OR REPLACE INTO google_light_assertions "
                "(assertion_id, run_id, assertion_type, subject, claim, evidence_count, "
                "evidence_refs_json, services_json, categories_json, privacy_tier, "
                "status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        a["assertion_id"],
                        a["run_id"],
                        a["assertion_type"],
                        a["subject"],
                        a["claim"],
                        a["evidence_count"],
                        a["evidence_refs_json"],
                        a["services_json"],
                        a["categories_json"],
                        a["privacy_tier"],
                        "staging",
                        a["created_at"],
                    )
                    for a in assertions
                ],
            )

        insert_run(
            con,
            run_id=stats.run_id,
            run_type="light_assertions",
            input_hash=stats.input_hash,
            dataset_hash=stats.dataset_hash,
            activities_count=stats.activities_scanned,
            config_hash=ASSERT_CONFIG_HASH,
            status="staging" if gate["passed"] else "rejected",
            stats=stats.to_dict(),
            supersedes_run_id=prev,
            gate=gate,
        )

        if gate["passed"]:
            # Promote: old current → superseded; staging → current
            if prev:
                con.execute(
                    "UPDATE google_light_assertions SET status='superseded' "
                    "WHERE status='current'"
                )
                con.execute(
                    "UPDATE google_structure_runs SET status='superseded' "
                    "WHERE run_id=? AND status='current'",
                    (prev,),
                )
            else:
                # First publish or legacy rows without run_id
                con.execute(
                    "UPDATE google_light_assertions SET status='superseded' "
                    "WHERE status='current'"
                )
            con.execute(
                "UPDATE google_light_assertions SET status='current' "
                "WHERE status='staging' AND run_id=?",
                (stats.run_id,),
            )
            con.execute(
                "UPDATE google_structure_runs SET status='current' WHERE run_id=?",
                (stats.run_id,),
            )
            log_promote(
                con,
                action="promote_assertions",
                run_id=stats.run_id,
                previous_run_id=prev,
                dataset_hash=stats.dataset_hash,
                detail={"assertions": stats.assertions, "gate": gate},
            )
            write_active_pointer(active_pointer_path, stats.run_id)
            stats.promoted = True
            for a in assertions:
                a["status"] = "current"
        else:
            # Abort: drop staging for this run; keep previous current
            con.execute(
                "DELETE FROM google_light_assertions WHERE status='staging' AND run_id=?",
                (stats.run_id,),
            )
            log_promote(
                con,
                action="abort_assertions",
                run_id=stats.run_id,
                previous_run_id=prev,
                dataset_hash=stats.dataset_hash,
                detail={"gate": gate},
            )
            stats.promoted = False

        con.commit()
    else:
        for a in assertions:
            a["status"] = "current"  # dry-run preview as if current

    con.close()
    return stats, assertions


def rollback_to_previous(
    db_path: Path = GOOGLE_DB,
    active_pointer_path: Path | None = None,
) -> dict:
    if active_pointer_path is None:
        active_pointer_path = (
            ACTIVE_POINTER
            if db_path.resolve() == GOOGLE_DB.resolve()
            else db_path.with_name("google_structure_active_run.txt")
        )
    """Restore previous superseded assertion run (ops-only)."""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    _ensure_assertion_schema(con)
    ensure_lifecycle_tables(con)
    cur = current_run_id(con, "light_assertions")
    if not cur:
        con.close()
        return {"ok": False, "reason": "no current run"}
    row = con.execute(
        "SELECT supersedes_run_id, dataset_hash FROM google_structure_runs WHERE run_id=?",
        (cur,),
    ).fetchone()
    prev = row["supersedes_run_id"] if row else None
    if not prev:
        con.close()
        return {"ok": False, "reason": "no previous run to restore", "current": cur}

    con.execute(
        "UPDATE google_light_assertions SET status='superseded' WHERE status='current'"
    )
    con.execute(
        "UPDATE google_light_assertions SET status='current' WHERE run_id=? AND status='superseded'",
        (prev,),
    )
    # If no rows had that run_id (legacy), fail closed
    n = con.execute(
        "SELECT COUNT(*) FROM google_light_assertions WHERE status='current'"
    ).fetchone()[0]
    if n == 0:
        con.rollback()
        con.close()
        return {"ok": False, "reason": "previous run has no rows", "previous": prev}

    con.execute(
        "UPDATE google_structure_runs SET status='rolled_back' WHERE run_id=?",
        (cur,),
    )
    con.execute(
        "UPDATE google_structure_runs SET status='current' WHERE run_id=?",
        (prev,),
    )
    log_promote(
        con,
        action="rollback_assertions",
        run_id=prev,
        previous_run_id=cur,
        dataset_hash=None,
        detail={"restored_count": n},
    )
    write_active_pointer(active_pointer_path, prev)
    con.commit()
    con.close()
    return {"ok": True, "restored_run_id": prev, "from_run_id": cur, "count": n}


def write_report(stats: AssertStats, assertions: list[dict], path: Path = REPORT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sample = [
        {
            "assertion_type": a["assertion_type"],
            "subject": a["subject"],
            "evidence_count": a["evidence_count"],
            "privacy_tier": a["privacy_tier"],
            "status": a.get("status"),
        }
        for a in sorted(assertions, key=lambda x: -x["evidence_count"])[:20]
    ]
    doc = {
        "generated_at": utc_now(),
        "phase": 16,
        "stats": stats.to_dict(),
        "top_assertions": sample,
        "notes": [
            "Light assertions are aggregate signals, not dialogue knowledge units.",
            "event_id namespace uses g| prefix; dialogue uses cm|.",
            "Privacy: restricted by service (Maps) AND category/content "
            "(支付/金融/卡 + 地图/地点/位置/导航), including Search/Gemini location topics.",
            "Lifecycle: stage → privacy gate → promote; superseded retained for rollback.",
        ],
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 16: Google light assertions")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--write", action="store_true")
    p.add_argument("--rollback", action="store_true", help="Restore previous assertion run")
    p.add_argument("--db", type=Path, default=GOOGLE_DB)
    p.add_argument("--report", type=Path, default=REPORT_PATH)
    args = p.parse_args(argv)
    if args.rollback:
        result = rollback_to_previous(args.db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    write = bool(args.write)
    stats, assertions = build(args.db, write=write)
    write_report(stats, assertions, args.report)
    print(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
    print(f"report: {args.report}")
    if write and stats.gate_passed is False:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
