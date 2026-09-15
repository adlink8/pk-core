"""Live localhost MCP smoke and descriptor snapshot verification."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import urllib.request


def _json(url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="GET" if data is None else "POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=20) as response:
        return json.loads(response.read().decode())


def _call(mcp_url: str, request_id: int, method: str, params: dict | None = None) -> dict:
    body = _json(mcp_url, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
    if "error" in body:
        raise RuntimeError(f"mcp_error:{body['error'].get('code')}:{body['error'].get('message')}")
    return body["result"]


def _core(tool: dict) -> dict:
    return {key: tool[key] for key in ("name", "inputSchema", "outputSchema", "annotations", "securitySchemes")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rest-url", default="http://127.0.0.1:8000")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8789/mcp")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    reviewed = json.loads(args.snapshot.read_text(encoding="utf-8"))
    rest_health = _json(args.rest_url.rstrip("/") + "/health")
    initialize = _call(args.mcp_url, 1, "initialize")
    listed = _call(args.mcp_url, 2, "tools/list")
    live_tools = sorted((_core(tool) for tool in listed["tools"]), key=lambda item: item["name"])
    canonical = json.dumps(live_tools, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    live_hash = hashlib.sha256(canonical.encode()).hexdigest()
    # JS snapshot hash uses recursively sorted keys; sort_keys produces the same canonical order.
    if live_tools != reviewed["tools"] or live_hash != reviewed["descriptor_sha256"]:
        raise RuntimeError(f"descriptor_snapshot_drift:{live_hash}")

    read = _call(args.mcp_url, 3, "tools/call", {
        "name": "decision_analysis_list", "arguments": {"limit": 1},
    })["structuredContent"]
    if not read.get("ok") or read.get("schema_version") != "agent_compact_envelope_v1":
        raise RuntimeError("analysis_list_compact_contract_failed")
    run_id = next((item for item in read.get("ids", []) if str(item).startswith("dar_")), None)
    if not run_id:
        raise RuntimeError("analysis_run_missing")
    explained = _call(args.mcp_url, 4, "tools/call", {
        "name": "decision_analysis_explain", "arguments": {"run_id": run_id},
    })["structuredContent"]
    if not explained.get("ok"):
        raise RuntimeError("analysis_explain_failed")

    evidence = {
        "schema_version": "agent_stack_smoke_v1",
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "rest_ready": rest_health.get("ok") is True,
        "mcp_protocol_version": initialize.get("protocolVersion"),
        "tool_count": len(live_tools),
        "descriptor_sha256": live_hash,
        "read": {"operation": read["operation"], "ids": read["ids"], "bytes": len(json.dumps(read).encode())},
        "explain": {"operation": explained["operation"], "ids": explained["ids"], "bytes": len(json.dumps(explained).encode())},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
