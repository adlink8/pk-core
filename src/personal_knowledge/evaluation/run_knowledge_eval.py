"""Single entrypoint for Phase 17 knowledge evaluation.

Stages: dataset audit → extraction quality → five-mode retrieval → answer →
render → gate. Active pointer is never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.core.project_paths import (  # noqa: E402
    AGENT_CONVERSATIONS_DB,
    ANALYSIS_DIR,
    DB_DIR,
    ROOT,
    UNIFIED_DB,
)
from personal_knowledge.evaluation.eval_contracts import (  # noqa: E402
    ContractError,
    audit_dataset,
    cases_checksum,
    content_checksum,
    compute_run_id,
    config_checksum,
    dump_json,
    load_cases_jsonl,
)
from personal_knowledge.evaluation.eval_registry import EvalRegistry  # noqa: E402
from personal_knowledge.evaluation.knowledge_eval_metrics import (  # noqa: E402
    SCORER_VERSION,
    aggregate_scores,
    compare_modes,
    per_scenario,
    score_case,
)
from personal_knowledge.evaluation.retrieval_adapters import (  # noqa: E402
    audit_l2_collection,
    capture_serving_binding,
    l2_eval_collection_name,
    load_l2_unit_ids,
    resolve_targets,
    run_adapter,
    validate_eval_binding,
)

EVAL_ROOT = ANALYSIS_DIR / "evaluations"
DEFAULT_CONFIG = (
    ROOT / "assets" / "evals" / "knowledge_units" / "eval_v1.yaml"
)
DEFAULT_POLICY = (
    ROOT / "assets" / "evals" / "knowledge_units" / "eval_policy_v1.yaml"
)
REGISTRY_DB = DB_DIR / "evaluation_registry.sqlite"

_EVAL_IMPLEMENTATION_FILES = (
    ROOT / "src" / "personal_knowledge" / "evaluation" / "run_knowledge_eval.py",
    ROOT / "src" / "personal_knowledge" / "evaluation" / "retrieval_adapters.py",
    ROOT / "src" / "personal_knowledge" / "evaluation" / "gate_knowledge_candidate.py",
    ROOT / "src" / "personal_knowledge" / "evaluation" / "answer_eval.py",
    ROOT / "src" / "personal_knowledge" / "retrieval" / "relevance.py",
    ROOT / "src" / "personal_knowledge" / "retrieval" / "semantic_search.py",
    ROOT / "src" / "personal_knowledge" / "retrieval" / "evidence.py",
)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def implementation_binding(files: tuple[Path, ...] | None = None) -> dict[str, str]:
    """Bind an evaluation run ID to the exact executable evaluation logic."""
    binding: dict[str, str] = {}
    for path in files or _EVAL_IMPLEMENTATION_FILES:
        key = path.name if files is not None else path.relative_to(ROOT).as_posix()
        binding[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return binding


def _read_active() -> str:
    p = DB_DIR / "knowledge_index_active.txt"
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def _active_checksum_proxy() -> str:
    """Lightweight checksum: active name + unit_count from SQLite (not full Chroma)."""
    import hashlib
    import sqlite3

    active = _read_active()
    count = ""
    if UNIFIED_DB.exists():
        con = sqlite3.connect(f"file:{UNIFIED_DB.as_posix()}?mode=ro", uri=True)
        row = con.execute(
            "SELECT unit_count FROM knowledge_index_versions WHERE collection_name=?",
            (active,),
        ).fetchone()
        count = str(row[0] if row else "")
        con.close()
    return hashlib.sha256(f"{active}|{count}".encode()).hexdigest()


def load_config(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore

            return yaml.safe_load(text) or {}
        except Exception:
            # allow JSON content in yaml file for zero-dep CI
            return json.loads(text)
    return json.loads(text)


def resolve_cases_path(cfg: dict[str, Any]) -> Path:
    ds = cfg.get("dataset") or {}
    # prefer private full suite when present
    private = ds.get("private_path")
    if private:
        pp = ROOT / private if not Path(private).is_absolute() else Path(private)
        if pp.exists():
            return pp
    public = ds.get("path") or "assets/evals/knowledge_units/comprehensive_v1.synthetic.jsonl"
    path = ROOT / public if not Path(public).is_absolute() else Path(public)
    if not path.exists():
        raise FileNotFoundError(f"dataset not found: {path}")
    return path


def _is_real_gold_case(case) -> bool:
    return (
        not case.gold_provenance.startswith("synthetic")
        and not case.expected_abstain
        and bool(
            case.gold_evidence_refs
            or case.gold_unit_ids
            or case.gold_title_substrings
        )
    )


def _load_ineligible_evidence_refs() -> set[str]:
    """Load identifiers whose canonical source is explicitly evidence-ineligible."""
    import sqlite3

    if not AGENT_CONVERSATIONS_DB.exists():
        return set()
    con = sqlite3.connect(
        f"file:{AGENT_CONVERSATIONS_DB.as_posix()}?mode=ro", uri=True
    )
    rows = con.execute(
        "SELECT m.canonical_message_id, m.source_message_ref "
        "FROM canonical_messages m "
        "JOIN canonical_sessions s ON s.canonical_session_id=m.canonical_session_id "
        "WHERE COALESCE(s.evidence_eligible,0)=0 "
        "OR LOWER(COALESCE(m.evidence_scope,'')) IN ('secret','excluded') "
        "OR LOWER(COALESCE(s.evidence_scope,'')) IN ('secret','excluded')"
    ).fetchall()
    con.close()
    return {
        str(value)
        for row in rows
        for value in row
        if value is not None and str(value)
    }


def _annotate_evidence_eligibility(ranked, ineligible_refs: set[str]) -> None:
    for hit in ranked:
        refs = {
            str(hit.source_ref or ""),
            str(hit.meta.get("source_message_ref") or ""),
            str(hit.meta.get("canonical_message_id") or ""),
        }
        hit.meta["source_evidence_ineligible"] = bool(
            ineligible_refs.intersection(refs - {""})
        )


def stage_dataset_audit(
    cases,
    run_dir: Path,
    *,
    require_private_gold: bool = False,
) -> dict[str, Any]:
    import sqlite3

    resolvable_refs: set[str] = set()
    if AGENT_CONVERSATIONS_DB.exists():
        con = sqlite3.connect(
            f"file:{AGENT_CONVERSATIONS_DB.as_posix()}?mode=ro", uri=True
        )
        resolvable_refs = {
            str(row[0])
            for row in con.execute(
                "SELECT canonical_message_id FROM canonical_messages"
            )
        }
        con.close()
    audit = audit_dataset(
        cases,
        require_gold_resolvable=True,
        resolvable_refs=resolvable_refs,
    )
    real_refs = {
        ref
        for case in cases
        for ref in case.gold_evidence_refs
        if not ref.startswith("syn-")
    }
    audit["gold_evidence_refs"] = len(real_refs)
    audit["gold_evidence_refs_resolved"] = len(real_refs & resolvable_refs)
    audit["gold_resolvable_rate"] = (
        len(real_refs & resolvable_refs) / len(real_refs) if real_refs else None
    )
    real_gold_cases = [case for case in cases if _is_real_gold_case(case)]
    real_cross_turn = [case for case in real_gold_cases if case.requires_cross_turn]
    audit["real_gold_cases"] = len(real_gold_cases)
    audit["real_cross_turn_gold_cases"] = len(real_cross_turn)
    if require_private_gold:
        if len(real_gold_cases) < 30:
            audit["errors"].append(
                f"private suite requires >=30 real gold cases; found {len(real_gold_cases)}"
            )
        if len(real_cross_turn) < 30:
            audit["errors"].append(
                "private suite requires >=30 real cross-turn gold cases; "
                f"found {len(real_cross_turn)}"
            )
        audit["ok"] = not audit["errors"]
    dump_json(run_dir / "dataset_audit.json", audit)
    return audit


def stage_human_review(*, enabled: bool) -> dict[str, Any]:
    """Capture a metadata-only, checksum-bound view of required review evidence.

    The function name and stage key remain for run-manifest compatibility. The
    bound manifests explicitly identify whether evidence came from a human or LLM.
    """
    if not enabled:
        return {"skipped": True, "ok": False}
    from personal_knowledge.evaluation.review_packets import status

    review = status()
    proofs: dict[str, Any] = {}
    for kind, manifest in sorted((review.get("manifests") or {}).items()):
        if not manifest:
            proofs[kind] = {"present": False}
            continue
        proofs[kind] = {
            "present": True,
            "kind": manifest.get("kind"),
            "count": manifest.get("count"),
            "cross_turn_count": manifest.get("cross_turn_count"),
            "reviewer_id_hash": manifest.get("reviewer_id_hash"),
            "reviewer_type": manifest.get("reviewer_type", "human"),
            "model_id": manifest.get("model_id"),
            "review_run_id": manifest.get("review_run_id"),
            "prompt_version": manifest.get("prompt_version"),
            "reviewed_at": manifest.get("reviewed_at"),
            "source_checksum": manifest.get("source_checksum"),
            "import_checksum": manifest.get("import_checksum"),
            "human_checksum": manifest.get("human_checksum"),
            "judge_cache_checksum": manifest.get("judge_cache_checksum"),
            "judge_gate_enabled": manifest.get("judge_gate_enabled"),
            "manifest_checksum": content_checksum(manifest),
        }
    binding_body = {"checks": review.get("checks") or {}, "proofs": proofs}
    return {
        "ok": bool(review.get("ok")),
        **binding_body,
        "binding_checksum": content_checksum(binding_body),
    }


def stage_extraction(run_dir: Path, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {"skipped": True}
    from personal_knowledge.evaluation.extraction_quality_eval import evaluate_extraction
    from personal_knowledge.evaluation.reconcile_l2_lineage import reconcile
    from personal_knowledge.evaluation.review_packets import (
        GROUNDED_IMPORT,
        GROUNDED_MANIFEST,
        checksum,
    )

    if not GROUNDED_IMPORT.exists() or not GROUNDED_MANIFEST.exists():
        raise ContractError("reviewed grounded labels and manifest are required")
    grounded_labels = [
        json.loads(line)
        for line in GROUNDED_IMPORT.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    grounded_manifest = json.loads(GROUNDED_MANIFEST.read_text(encoding="utf-8"))
    if checksum(grounded_labels) != grounded_manifest.get("import_checksum"):
        raise ContractError("reviewed grounded labels checksum mismatch")

    lineage = reconcile(UNIFIED_DB)
    dump_json(run_dir / "l2_lineage.json", lineage)
    eq = evaluate_extraction(
        UNIFIED_DB,
        sample_limit=50,
        human_labels=grounded_labels,
    )
    dump_json(run_dir / "extraction_quality.json", eq)
    return {"lineage": lineage, "extraction_quality": eq}


def stage_retrieval(
    cases,
    cfg: dict[str, Any],
    run_dir: Path,
    *,
    offline: bool = False,
    serving_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    targets_cfg = cfg.get("targets") or {}
    top_k = int(cfg.get("top_k") or 5)
    l1 = targets_cfg.get("l1_collection") or "knowledge_units_run_76c6259e_20260712062418"
    l1l2 = targets_cfg.get("l1_l2_collection") or _read_active()
    raw = targets_cfg.get("raw_collection") or "personal_events"
    l2_runs = targets_cfg.get("l2_run_ids")
    l2_ids = load_l2_unit_ids(UNIFIED_DB, l2_runs)
    ineligible_refs = _load_ineligible_evidence_refs()
    l2_collection = targets_cfg.get("l2_only_collection") or ""
    l2_audit = (
        audit_l2_collection(l2_collection, l2_ids)
        if l2_collection
        else {
            "collection": "",
            "expected": len(l2_ids),
            "actual": 0,
            "missing": len(l2_ids),
            "orphan": 0,
            "ok": False,
            "error": "L2-only collection is not configured",
        }
    )
    expected_l2_collection = l2_eval_collection_name(l1l2, l2_ids) if l2_ids else ""
    l2_audit["source_collection"] = l1l2
    l2_audit["expected_collection"] = expected_l2_collection
    l2_audit["source_binding_ok"] = l2_collection == expected_l2_collection
    if not l2_audit["source_binding_ok"]:
        l2_audit["ok"] = False
        l2_audit["error"] = "L2-only collection is not bound to the evaluated L1+L2 source"
    binding_check = validate_eval_binding(
        serving_binding or capture_serving_binding(),
        targets_cfg,
        l2_audit=l2_audit,
    )
    if not binding_check["ok"]:
        raise ContractError(
            "evaluation snapshot binding failed: "
            + ";".join(binding_check["errors"])
        )
    # L2-only: blocked unless we can filter — collection is shared; mark blocked
    # when purify not reliable for canonical-only index
    l2_blocked_reason = ""
    pure_l2 = (
        bool(targets_cfg.get("l2_only_purified"))
        and bool(l2_ids)
        and bool(l2_audit.get("ok"))
    )

    targets = resolve_targets(
        l1_collection=l1,
        l1_l2_collection=l1l2,
        raw_collection=raw,
        l2_only_collection=l2_collection or l1l2,
        l2_lineage_runs=l2_runs,
        top_k=top_k,
        l2_filter_ids=l2_ids if pure_l2 else None,
        l2_blocked_reason=l2_blocked_reason
        or (
            ""
            if pure_l2
            else "L2-only collection failed exact lineage audit: "
            + str(
                l2_audit.get("error")
                or {
                    "missing": l2_audit.get("missing"),
                    "orphan": l2_audit.get("orphan"),
                }
            )
        ),
    )

    mode_scores: dict[str, list] = {}
    mode_ranked: dict[str, list[list[Any]]] = {}
    mode_aggs: dict[str, Any] = {}
    blocked_modes: list[str] = []

    if offline:
        # Fixture path: empty rankings for structure only
        for t in targets:
            scores = []
            ranked_by_case: list[list[Any]] = []
            for c in cases:
                ranked_by_case.append([])
                scores.append(
                    score_case(
                        c.id,
                        t.mode,
                        [],
                        gold_refs=c.gold_evidence_refs,
                        gold_unit_ids=c.gold_unit_ids,
                        expected_abstain=c.expected_abstain,
                        privacy_sensitive=c.privacy_sensitive,
                        secret_ineligible=c.secret_ineligible,
                        forbid_subject_substrings=c.forbid_subject_substrings,
                        score_retrieval=_is_real_gold_case(c),
                    )
                )
            mode_scores[t.mode] = scores
            mode_ranked[t.mode] = ranked_by_case
            mode_aggs[t.mode] = {
                "aggregate": aggregate_scores(scores),
                "per_scenario": per_scenario(
                    scores, {c.id: c.scenario or c.suite_tag or c.group for c in cases}
                ),
                "blocked": t.blocked,
                "target": t.to_dict(),
                "serving_snapshot": binding_check,
            }
        comparisons = compare_modes(mode_scores, baseline="raw")
        cross_turn = {
            mode: [s for s in scores if next((c.requires_cross_turn for c in cases if c.id == s.query_id), False)]
            for mode, scores in mode_scores.items()
        }
        return {
            "modes": mode_aggs,
            "comparisons": comparisons.get("comparisons") or {},
            "mode_scores": mode_scores,
            "mode_ranked": mode_ranked,
            "scenario_comparisons": {
                "cross_turn_l1_baseline": compare_modes(cross_turn, baseline="l1").get("comparisons", {})
            },
            "targets": [t.to_dict() for t in targets],
            "l2_collection_audit": l2_audit,
            "offline": True,
            "serving_snapshot": binding_check,
        }

    for t in targets:
        scores = []
        ranked_by_case: list[list[Any]] = []
        if t.blocked:
            blocked_modes.append(t.mode)
            for c in cases:
                ranked_by_case.append([])
                sc = score_case(
                    c.id,
                    t.mode,
                    [],
                    gold_refs=c.gold_evidence_refs,
                    gold_unit_ids=c.gold_unit_ids,
                    expected_abstain=c.expected_abstain,
                    score_retrieval=_is_real_gold_case(c),
                )
                sc.notes = t.blocked_reason
                scores.append(sc)
            mode_scores[t.mode] = scores
            mode_ranked[t.mode] = ranked_by_case
            mode_aggs[t.mode] = {
                "aggregate": aggregate_scores(scores),
                "per_scenario": per_scenario(
                    scores, {c.id: c.scenario or c.suite_tag or c.group for c in cases}
                ),
                "blocked": True,
                "blocked_reason": t.blocked_reason,
                "target": t.to_dict(),
                "serving_snapshot": binding_check,
            }
            continue

        for c in cases:
            try:
                ar = run_adapter(t, c, l2_unit_ids=l2_ids)
            except Exception as e:
                ar = type("X", (), {})()
                ar.ranked = []
                ar.latency_ms = 0.0
                ar.first_layer = ""
                ar.blocked = True
                ar.blocked_reason = str(e)[:200]
            if getattr(ar, "blocked", False) and t.mode == "l2_only":
                pass
            _annotate_evidence_eligibility(ar.ranked, ineligible_refs)
            sc = score_case(
                c.id,
                t.mode,
                ar.ranked,
                gold_refs=c.gold_evidence_refs,
                gold_unit_ids=c.gold_unit_ids,
                gold_title_substrings=c.gold_title_substrings,
                expected_abstain=c.expected_abstain,
                privacy_sensitive=c.privacy_sensitive,
                secret_ineligible=c.secret_ineligible,
                forbid_subject_substrings=c.forbid_subject_substrings,
                latency_ms=ar.latency_ms,
                first_layer=ar.first_layer,
                score_retrieval=_is_real_gold_case(c),
            )
            scores.append(sc)
            ranked_by_case.append(list(ar.ranked))
        mode_scores[t.mode] = scores
        mode_ranked[t.mode] = ranked_by_case
        # per-case jsonl
        with (run_dir / f"cases_{t.mode}.jsonl").open("w", encoding="utf-8") as f:
            for s in scores:
                f.write(json.dumps(s.to_dict(), ensure_ascii=False) + "\n")
        mode_aggs[t.mode] = {
            "aggregate": aggregate_scores(scores),
            "per_scenario": per_scenario(
                scores, {c.id: c.scenario or c.suite_tag or c.group for c in cases}
            ),
            "blocked": False,
            "target": t.to_dict(),
            "serving_snapshot": binding_check,
        }

    comparisons = compare_modes(mode_scores, baseline="raw")
    cross_turn = {
        mode: [s for s in scores if next((c.requires_cross_turn for c in cases if c.id == s.query_id), False)]
        for mode, scores in mode_scores.items()
    }
    dump_json(run_dir / "retrieval.json", {
        "modes": {k: v for k, v in mode_aggs.items()},
        "comparisons": comparisons.get("comparisons") or {},
        "l2_collection_audit": l2_audit,
        "blocked_modes": blocked_modes,
        "serving_snapshot": binding_check,
    })
    return {
        "modes": mode_aggs,
        "comparisons": comparisons.get("comparisons") or {},
        "mode_scores": mode_scores,
        "mode_ranked": mode_ranked,
        "scenario_comparisons": {
            "cross_turn_l1_baseline": compare_modes(cross_turn, baseline="l1").get("comparisons", {})
        },
        "targets": [t.to_dict() for t in targets],
        "l2_collection_audit": l2_audit,
        "blocked_modes": blocked_modes,
        "offline": False,
        "serving_snapshot": binding_check,
    }


def stage_answer(
    cases,
    retrieval: dict[str, Any],
    run_dir: Path,
    *,
    enabled: bool,
    offline: bool = False,
) -> dict[str, Any]:
    if not enabled:
        return {"skipped": True}
    from personal_knowledge.evaluation.answer_eval import (
        aggregate_answer_scores,
        generate_answer,
        score_answer,
    )
    from personal_knowledge.evaluation.knowledge_eval_metrics import RankedHit

    mode_scores = retrieval.get("mode_scores") or {}
    mode_ranked = retrieval.get("mode_ranked") or {}
    out_modes: dict[str, Any] = {}
    for mode, scores in mode_scores.items():
        if (retrieval.get("modes") or {}).get(mode, {}).get("blocked"):
            out_modes[mode] = {"blocked": True}
            continue
        ans_scores = []
        ranked_by_case = mode_ranked.get(mode) or []
        for index, (c, sc) in enumerate(zip(cases, scores)):
            ranked = list(ranked_by_case[index]) if index < len(ranked_by_case) else [
                RankedHit(id=i, snippet="", subject="") for i in (sc.ranked_ids or [])
            ]
            # In offline/synthetic without live retrieval, ranked empty
            ar = generate_answer(
                c.query,
                ranked,
                expected_abstain=c.expected_abstain,
            )
            ar.query_id = c.id
            ar.mode = mode
            ascore = score_answer(
                ar,
                ranked_ids=sc.ranked_ids or [],
                gold_refs=[*c.gold_evidence_refs, *c.gold_unit_ids],
                expected_abstain=c.expected_abstain,
                forbid_substrings=c.forbid_subject_substrings,
            )
            ascore.query_id = c.id
            ascore.mode = mode
            ans_scores.append(ascore)
        out_modes[mode] = {
            "aggregate": aggregate_answer_scores(ans_scores),
            "n": len(ans_scores),
        }
    dump_json(run_dir / "answer.json", out_modes)
    return {"modes": out_modes, "skipped": False}


def primary_claims(summary: dict[str, Any]) -> dict[str, Any]:
    cmp_ = summary.get("comparisons") or {}
    primary = cmp_.get("l1_l2") or cmp_.get("l1") or {}
    delta = primary.get("delta")
    boot = primary.get("bootstrap") or {}
    if delta is None or boot.get("insufficient_evidence"):
        return {
            "summary": "未证明提升 / 证据不足",
            "ku_vs_raw_recall5": delta,
            "ci_low": boot.get("ci_low"),
        }
    delta_pp = delta * 100
    ok = delta_pp >= 10 and (boot.get("ci_low") is None or boot.get("ci_low") > 0)
    return {
        "summary": "已证明提升" if ok else "未证明提升",
        "ku_vs_raw_recall5_pp": delta_pp,
        "ci_low": boot.get("ci_low"),
        "ok": ok,
    }


def run_eval(
    config_path: Path,
    *,
    retrieval_only: bool = False,
    full: bool = False,
    render: bool = False,
    gate: bool = False,
    dry_run: bool = False,
    offline: bool = False,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    cfg = load_config(config_path)
    declared_scorer = str(cfg.get("scorer_version") or "")
    if declared_scorer != SCORER_VERSION:
        raise ContractError(
            f"config scorer_version={declared_scorer!r} does not match runtime "
            f"{SCORER_VERSION!r}"
        )
    active_serving_before = capture_serving_binding()
    requested_snapshot_id = str(
        (cfg.get("targets") or {}).get("serving_snapshot_id") or ""
    )
    serving_before = capture_serving_binding(
        snapshot_id=requested_snapshot_id or None
    )
    cases_path = resolve_cases_path(cfg)
    cases = load_cases_jsonl(cases_path)
    ds_ck = cases_checksum(cases)
    # Include runtime flags so offline/live do not share one artifact slot
    cfg_for_hash = dict(cfg)
    cfg_for_hash["_runtime"] = {
        "offline": offline,
        "retrieval_only": retrieval_only,
        "full": full,
        "serving_snapshot_id": serving_before.get("snapshot_id"),
        "serving_manifest_hash": serving_before.get("manifest_hash"),
        "implementation_binding": implementation_binding(),
    }
    cfg_ck = config_checksum(cfg_for_hash)
    top_k = int(cfg.get("top_k") or 5)
    modes = list(cfg.get("modes") or ["raw", "l1", "l2_only", "l1_l2", "hybrid"])
    run_id = compute_run_id(ds_ck, cfg_ck, SCORER_VERSION, modes, top_k)

    active_before = _read_active()
    checksum_before = _active_checksum_proxy()

    run_dir = out_dir or (EVAL_ROOT / run_id[:16])
    run_dir.mkdir(parents=True, exist_ok=True)

    stages: dict[str, Any] = {}
    errors: list[str] = []
    initial_binding = validate_eval_binding(serving_before, cfg.get("targets") or {})
    if not initial_binding["ok"]:
        errors.append("serving_binding_before: " + ";".join(initial_binding["errors"]))

    # 1 dataset audit
    try:
        private_path_value = (cfg.get("dataset") or {}).get("private_path")
        private_path = (
            (
                ROOT / private_path_value
                if not Path(private_path_value).is_absolute()
                else Path(private_path_value)
            )
            if private_path_value
            else None
        )
        stages["dataset_audit"] = stage_dataset_audit(
            cases,
            run_dir,
            require_private_gold=(
                private_path is not None
                and cases_path.resolve() == private_path.resolve()
            ),
        )
        if not stages["dataset_audit"].get("ok"):
            errors.append("dataset_audit failed: " + str(stages["dataset_audit"].get("errors")))
    except Exception as e:
        errors.append(f"dataset_audit: {e}")
        stages["dataset_audit"] = {"ok": False, "error": str(e)}

    # Genuine full claims require imported human evidence. The binding is
    # metadata-only and becomes part of the immutable run manifest.
    stages["human_review"] = stage_human_review(enabled=full)
    if full and not stages["human_review"].get("ok"):
        errors.append("human_review_evidence_incomplete")

    # 2 extraction + lineage
    try:
        stages["extraction"] = stage_extraction(
            run_dir, enabled=full or not retrieval_only
        )
    except Exception as e:
        errors.append(f"extraction: {e}")
        stages["extraction"] = {"error": str(e)}

    # 3 retrieval
    try:
        retrieval = stage_retrieval(
            cases, cfg, run_dir, offline=offline, serving_binding=serving_before
        )
        stages["retrieval"] = {
            "modes": {k: v for k, v in retrieval["modes"].items()},
            "comparisons": retrieval.get("comparisons"),
            "scenario_comparisons": retrieval.get("scenario_comparisons"),
            "blocked_modes": retrieval.get("blocked_modes"),
            "l2_collection_audit": retrieval.get("l2_collection_audit"),
            "serving_snapshot": retrieval.get("serving_snapshot"),
        }
    except Exception as e:
        traceback.print_exc()
        errors.append(f"retrieval: {e}")
        retrieval = {"modes": {}, "comparisons": {}, "mode_scores": {}}
        stages["retrieval"] = {"error": str(e)}

    # 4 answer
    answer_enabled = full or (not retrieval_only)
    try:
        answer = stage_answer(
            cases,
            retrieval,
            run_dir,
            enabled=answer_enabled,
            offline=offline,
        )
        stages["answer"] = answer
    except Exception as e:
        errors.append(f"answer: {e}")
        answer = {"skipped": True, "error": str(e)}
        stages["answer"] = answer

    summary: dict[str, Any] = {
        "run_id": run_id,
        "generated_at": _utc(),
        "dataset_path": str(cases_path).replace("\\", "/"),
        "dataset_checksum": ds_ck,
        "config_checksum": cfg_ck,
        "scorer_version": SCORER_VERSION,
        "implementation_binding": cfg_for_hash["_runtime"]["implementation_binding"],
        "top_k": top_k,
        "modes": retrieval.get("modes") or {},
        "comparisons": retrieval.get("comparisons") or {},
        "scenario_comparisons": retrieval.get("scenario_comparisons") or {},
        "answer": answer,
        "stages": {k: ("ok" if "error" not in str(v) else "error") for k, v in stages.items()},
        "stage_details": {
            "dataset_audit": stages.get("dataset_audit"),
            "extraction_ok": (stages.get("extraction") or {}).get("extraction_quality", {}).get("ok")
            if isinstance(stages.get("extraction"), dict)
            else None,
            "lineage_ok": (stages.get("extraction") or {}).get("lineage", {}).get("ok")
            if isinstance(stages.get("extraction"), dict)
            else None,
            "lineage": (stages.get("extraction") or {}).get("lineage")
            if isinstance(stages.get("extraction"), dict)
            else None,
            "extraction_quality": (stages.get("extraction") or {}).get("extraction_quality")
            if isinstance(stages.get("extraction"), dict)
            else None,
            "l2_collection_audit": (stages.get("retrieval") or {}).get(
                "l2_collection_audit"
            )
            if isinstance(stages.get("retrieval"), dict)
            else None,
            "human_review": stages.get("human_review"),
        },
        "candidate_collection": (cfg.get("targets") or {}).get("candidate_collection")
        or (cfg.get("targets") or {}).get("l1_l2_collection")
        or active_before,
        "candidate_checksum": (cfg.get("targets") or {}).get("candidate_checksum") or "",
        "active_collection": active_before,
        "active_checksum_before": checksum_before,
        "serving_snapshot_before": serving_before,
        "active_serving_snapshot_before": active_serving_before,
        "errors": errors,
        "dry_run": dry_run,
        "offline": offline,
    }
    summary["primary_claims"] = primary_claims(summary)
    dump_json(run_dir / "summary.json", summary)

    # registry (immutable)
    try:
        reg = EvalRegistry(REGISTRY_DB)
        if not reg.has_run(run_id):
            reg.create_run(
                run_id,
                dataset_checksum=ds_ck,
                config_checksum=cfg_ck,
                scorer_version=SCORER_VERSION,
                top_k=top_k,
                modes=modes,
                notes="phase24_snapshot_bound",
            )
            for t in retrieval.get("targets") or []:
                try:
                    reg.add_target(run_id, t)
                except Exception:
                    pass
            for mode, payload in (retrieval.get("modes") or {}).items():
                try:
                    reg.add_metrics(run_id, mode, payload.get("aggregate") or payload)
                except Exception:
                    pass
            reg.add_artifact(run_id, "summary", str(run_dir / "summary.json"))
            reg.finalize(run_id, "completed" if not errors else "completed_with_errors")
        else:
            # immutable: keep existing; write side-by-side snapshot only
            dump_json(run_dir / "registry_exists.json", {"run_id": run_id, "exists": True})
    except Exception as e:
        errors.append(f"registry: {e}")

    # A caller-owned output directory (tests, sandbox, forensic replay) must
    # never mutate the global latest-run pointer.
    if out_dir is None:
        EVAL_ROOT.mkdir(parents=True, exist_ok=True)
        (EVAL_ROOT / "latest.txt").write_text(run_dir.name, encoding="utf-8")

    # render
    if render or full:
        try:
            from personal_knowledge.evaluation.render_knowledge_eval_report import render_run

            html = render_run(run_dir)
            summary["report_html"] = str(html)
            stages["render"] = {"path": str(html)}
        except Exception as e:
            errors.append(f"render: {e}")
            stages["render"] = {"error": str(e)}

    # gate
    gate_result = None
    if gate or full:
        try:
            from personal_knowledge.evaluation.gate_knowledge_candidate import evaluate_gate, load_policy

            policy_value = cfg.get("policy_path")
            policy_path = (
                ROOT / policy_value
                if policy_value and not Path(policy_value).is_absolute()
                else Path(policy_value)
                if policy_value
                else DEFAULT_POLICY
            )
            if not policy_path.exists():
                raise FileNotFoundError(f"evaluation policy not found: {policy_path}")
            policy = load_policy(policy_path)
            gate_result = evaluate_gate(
                summary,
                policy,
                candidate_collection=summary.get("candidate_collection") or "",
                candidate_checksum=summary.get("candidate_checksum") or "",
                require_answer=answer_enabled and not offline,
            )
            dump_json(run_dir / "gate.json", gate_result)
            summary["gate"] = gate_result
            dump_json(run_dir / "summary.json", summary)
            stages["gate"] = {"verdict": gate_result.get("verdict")}
        except Exception as e:
            errors.append(f"gate: {e}")
            stages["gate"] = {"error": str(e)}

    checksum_after = _active_checksum_proxy()
    active_after = _read_active()
    serving_after = capture_serving_binding(
        snapshot_id=requested_snapshot_id or None
    )
    active_serving_after = capture_serving_binding()
    summary["active_collection_after"] = active_after
    summary["active_checksum_after"] = checksum_after
    summary["active_unchanged"] = (
        active_before == active_after
        and checksum_before == checksum_after
        and active_serving_before == active_serving_after
    )
    summary["serving_snapshot_after"] = serving_after
    summary["active_serving_snapshot_after"] = active_serving_after
    if serving_before != serving_after:
        errors.append("evaluation_snapshot_changed_during_evaluation")
    if active_serving_before != active_serving_after:
        errors.append("active_serving_snapshot_changed_during_evaluation")
    summary["errors"] = errors
    dump_json(run_dir / "summary.json", summary)
    dump_json(
        run_dir / "run_manifest.json",
        {
            "run_id": run_id,
            "dataset_checksum": ds_ck,
            "config_checksum": cfg_ck,
            "scorer_version": SCORER_VERSION,
            "implementation_binding": cfg_for_hash["_runtime"]["implementation_binding"],
            "targets": retrieval.get("targets") or [],
            "serving_snapshot_before": serving_before,
            "serving_snapshot_after": serving_after,
            "active_unchanged": summary["active_unchanged"],
            "human_review_binding": stages.get("human_review"),
            "errors": errors,
        },
    )

    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run knowledge unit comprehensive eval")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--retrieval-only", action="store_true")
    p.add_argument("--full", action="store_true")
    p.add_argument("--render", action="store_true")
    p.add_argument("--gate", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--offline", action="store_true", help="no Chroma/embed; structural only")
    p.add_argument(
        "--no-require-answer",
        action="store_true",
        help="when gating, do not require answer eval stage",
    )
    p.add_argument("--out-dir", type=Path, default=None)
    args = p.parse_args(argv)

    try:
        # force answer skip for retrieval-only gate convenience
        if args.no_require_answer and args.retrieval_only:
            # monkeypatch via config is heavy; gate uses require_answer=answer_enabled
            pass
        summary = run_eval(
            args.config,
            retrieval_only=args.retrieval_only,
            full=args.full,
            render=args.render,
            gate=args.gate,
            dry_run=args.dry_run,
            offline=args.offline,
            out_dir=args.out_dir,
        )
    except (ContractError, FileNotFoundError) as e:
        print(f"[eval] FAIL: {e}")
        return 2

    print(
        f"[eval] run_id={summary['run_id'][:16]}… "
        f"claim={summary.get('primary_claims', {}).get('summary')} "
        f"active_unchanged={summary.get('active_unchanged')} "
        f"errors={len(summary.get('errors') or [])}"
    )
    if summary.get("gate"):
        print(f"[eval] gate={summary['gate'].get('verdict')}")
    # dry-run / offline still zero if no hard errors and active unchanged
    if not summary.get("active_unchanged"):
        return 3
    if summary.get("errors") and args.full:
        return 1
    if summary.get("gate") and not summary["gate"].get("passed") and args.gate:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
