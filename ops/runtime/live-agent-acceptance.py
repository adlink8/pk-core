"""Real MCP read/explain plus explicitly confirmed orchestration replay acceptance."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
AUTHORITIES = {
    "personal": ROOT / "var/db/personal_system.sqlite",
    "external": ROOT / "var/db/external_context.sqlite",
    "analysis": ROOT / "var/db/decision_analysis.sqlite",
    "pilot": ROOT / "var/db/project_pilot.sqlite",
    "calibration": ROOT / "var/db/recommendation_calibration.sqlite",
}
ORCHESTRATION = ROOT / "var/db/decision_orchestration.sqlite"


def _fingerprints() -> dict[str, str]:
    return {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in AUTHORITIES.items()}


def _counts() -> dict[str, int]:
    con = sqlite3.connect(ORCHESTRATION)
    try:
        return {
            table: int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("orchestration_sessions", "orchestration_events", "orchestration_confirmations", "orchestration_invocations")
        }
    finally:
        con.close()


def _rpc(url: str, request_id: int, method: str, params: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST",
    )
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=30) as response:
        body = json.loads(response.read().decode())
    if body.get("error"):
        raise RuntimeError(f"rpc_error:{body['error']}")
    return body["result"]


def _tool(url: str, request_id: int, name: str, arguments: dict) -> dict:
    result = _rpc(url, request_id, "tools/call", {"name": name, "arguments": arguments})
    if result.get("isError"):
        raise RuntimeError(f"tool_error:{name}:{result.get('structuredContent')}")
    compact = result["structuredContent"]
    if compact.get("schema_version") != "agent_compact_envelope_v1" or not compact.get("ok"):
        raise RuntimeError(f"compact_contract_failed:{name}")
    return compact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8789/mcp")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    before_hashes = _fingerprints()
    before_counts = _counts()

    listed = _tool(args.mcp_url, 1, "decision_analysis_list", {"limit": 1})
    run_id = next(item for item in listed["ids"] if str(item).startswith("dar_"))
    explained = _tool(args.mcp_url, 2, "decision_analysis_explain", {"run_id": run_id})

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    actor = hashlib.sha256(b"phase35-live-user-confirmation").hexdigest()
    prepared = _tool(args.mcp_url, 3, "agent_session_prepare", {
        "goal": "Choose the next bounded local validation step",
        "constraints": ["local inspection only", "manual operation only", "maximum 30 minutes"],
        "weights": {"safety": 1.0, "reversibility": 0.8},
        "actor_identity_hash": actor,
        "domain": "project", "risk_budget": "low",
        "max_external_age_seconds": 172800, "now": now,
    })
    preview = prepared["data"]
    idempotency_key = "phase35-live-confirm-" + preview["session_id"]
    confirm_args = {
        "preview": preview, "confirmed": True,
        "idempotency_key": idempotency_key, "now": now,
    }
    confirmed = _tool(args.mcp_url, 4, "agent_session_confirm", confirm_args)
    replayed = _tool(args.mcp_url, 5, "agent_session_confirm", confirm_args)
    first = confirmed["data"]
    replay = replayed["data"]
    if first["event_id"] != replay["event_id"] or replay["replayed"] is not True:
        raise RuntimeError("confirmation_replay_not_exact")

    after_hashes = _fingerprints()
    after_counts = _counts()
    if before_hashes != after_hashes:
        raise RuntimeError("authority_fingerprint_changed")
    expected_delta = {
        "orchestration_sessions": 1, "orchestration_events": 1,
        "orchestration_confirmations": 1, "orchestration_invocations": 0,
    }
    delta = {key: after_counts[key] - before_counts[key] for key in after_counts}
    if delta != expected_delta:
        raise RuntimeError(f"unexpected_orchestration_delta:{delta}")

    evidence = {
        "schema_version": "live_agent_acceptance_v1",
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "read_explain": {
            "run_id": run_id, "list_bytes": len(json.dumps(listed).encode()),
            "explain_bytes": len(json.dumps(explained).encode()),
        },
        "confirmed_replay": {
            "session_id": preview["session_id"], "event_id": first["event_id"],
            "state": first["state"], "sequence": first["sequence"],
            "exact_replay": True, "provider_calls": 0,
        },
        "authority_fingerprints_unchanged": True,
        "orchestration_delta": delta,
        "external_actions": 0,
        "automatic_promotions": 0,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
