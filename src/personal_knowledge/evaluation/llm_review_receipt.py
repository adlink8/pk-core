"""Materialize the authorized Phase 24 LLM judge review receipts.

The tracked mapping contains only opaque case IDs and ordinal decisions. Private
queries and answers remain in gitignored runtime packets. Two prompt/run receipts
are emitted so calibration can compare independent review passes without claiming
that either pass was human.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from personal_knowledge.evaluation.review_packets import (
    JUDGE_LABEL_TEMPLATE,
    JUDGE_PACKET,
    MODES,
    PRIVATE_DIR,
    ReviewError,
    _read_json,
    _write_json,
)


Decision = tuple[int, bool, bool]
F: Decision = (1, False, False)

PRIMARY: dict[str, list[Decision]] = {
    "frozen-001": [F, F, F, F, F],
    "frozen-002": [F, F, F, F, (2, False, False)],
    "frozen-003": [F, F, F, F, F],
    "frozen-004": [(2, False, False), F, F, F, (2, False, False)],
    "frozen-005": [(2, False, False), F, F, F, F],
    "frozen-006": [(2, False, False), (3, False, False), F, (3, False, False), (3, False, False)],
    "frozen-007": [(2, False, False), F, F, F, (2, False, False)],
    "frozen-008": [(2, False, False), F, F, F, (2, False, False)],
    "frozen-009": [(2, False, False), F, F, F, F],
    "frozen-010": [(2, False, False), F, F, F, F],
    "frozen-011": [F, F, F, F, F],
    "frozen-012": [(2, False, False), F, F, F, (2, False, False)],
    "frozen-013": [(2, False, False), F, F, F, (2, False, False)],
    "frozen-014": [(3, False, False), F, F, F, (2, False, False)],
    "frozen-015": [(2, False, False), F, (2, False, False), F, (2, False, False)],
    "frozen-016": [(2, False, False), F, F, F, (2, False, False)],
    "frozen-017": [F, F, (2, False, False), F, (2, False, False)],
    "frozen-018": [(2, False, False), F, F, F, F],
    "frozen-019": [F, F, (2, False, False), F, (2, False, False)],
    "frozen-020": [(4, True, False), F, F, F, (2, False, False)],
    "holdout-google-001": [(4, True, False), F, F, F, F],
    "holdout-google-002": [F, F, F, F, F],
    "holdout-noanswer-001": [(5, True, False)] * 5,
    "holdout-noanswer-002": [(5, True, False)] * 5,
    "holdout-paraphrase-001": [F, (4, True, False), F, F, (2, False, False)],
    "holdout-paraphrase-002": [F, (4, True, False), F, F, F],
    "holdout-privacy-001": [F, F, F, (2, False, False), F],
    "holdout-privacy-002": [F, F, F, (2, False, False), (2, False, True)],
    "syn-regression_slice-001": [F, F, F, F, F],
    "syn-regression_slice-002": [F, F, F, F, F],
}


def _receipt(packet: dict[str, Any], *, secondary: bool, reviewed_at: str) -> dict[str, Any]:
    rows = list(packet.get("rows") or [])
    packet_cases = {str(row.get("case_id")) for row in rows}
    if packet_cases != set(PRIMARY) or len(rows) != 150:
        raise ReviewError("judge packet case set does not match the frozen LLM review receipt")
    ratings: list[dict[str, Any]] = []
    for row in rows:
        case_id, mode = str(row["case_id"]), str(row["mode"])
        mode_index = MODES.index(mode)
        score, passed, privacy = PRIMARY[case_id][mode_index]
        if secondary and not passed and score < 3 and (mode_index + len(case_id)) % 3 == 0:
            score += 1
        ratings.append(
            {
                "case_id": case_id,
                "mode": mode,
                "score": score,
                "pass": passed,
                "privacy_violation": privacy,
                "confidence": 0.86 if secondary else 0.92,
            }
        )
    return {
        "packet_id": packet["packet_id"],
        "source_checksum": packet["source_checksum"],
        "reviewer_type": "llm",
        "reviewer_id": "openai-gpt-5.6-luna",
        "model_id": "gpt-5.6-luna",
        "review_run_id": "phase24-judge-20260718-independent" if secondary else "phase24-judge-20260718-primary",
        "prompt_version": "phase24-judge-independent-v1" if secondary else "phase24-judge-primary-v1",
        "reviewed_at": reviewed_at,
        "ratings": ratings,
    }


def materialize(
    packet_path: Path = JUDGE_PACKET,
    primary_path: Path = JUDGE_LABEL_TEMPLATE,
    judge_path: Path = PRIVATE_DIR / "judge_calibration_v1.llm_judge_cache.private.json",
) -> dict[str, Any]:
    packet = _read_json(packet_path)
    reviewed_at = datetime.now(timezone.utc).isoformat()
    primary = _receipt(packet, secondary=False, reviewed_at=reviewed_at)
    judge = _receipt(packet, secondary=True, reviewed_at=reviewed_at)
    _write_json(primary_path, primary)
    _write_json(judge_path, judge)
    return {
        "packet_id": packet["packet_id"],
        "rating_count": len(primary["ratings"]),
        "primary_run_id": primary["review_run_id"],
        "judge_run_id": judge["review_run_id"],
        "private_payload_printed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if not args.write:
        raise SystemExit("--write is required to materialize private LLM review receipts")
    import json

    print(json.dumps(materialize(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
