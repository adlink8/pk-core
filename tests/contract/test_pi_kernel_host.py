from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPErrorProcessor, ProxyHandler, urlopen


ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "apps" / "personal_intelligence_kernel" / "src" / "server.mjs"
DECISION_RUN_ID = "piq_f7896e839999ed2eac87ebd4"
OPENER = build_opener(ProxyHandler({}))


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event(number: int = 1, event_type: str = "task_started") -> dict[str, object]:
    event = {
        "event_id": "",
        "type": event_type,
        "source": "pi_kernel",
        "authority": "authority:python-test",
        "snapshot": "snapshot:python-test",
        "correlation_id": "corr:python-test",
        "causation_id": None,
        "idempotency_key": f"idem:python-test:{number}",
        "occurred_at": f"2026-08-04T09:00:{number:02d}.000Z",
        "payload_ref": {"kind": "none", "ref": None, "checksum": None},
        "privacy_class": "R1",
    }
    identity = {key: event[key] for key in list(event)[1:]}
    digest = hashlib.sha256(f"pi_kernel_event_v1:{_canonical(identity)}".encode()).hexdigest()
    event["event_id"] = f"pi_evt_{digest}"
    return event


def _write_decision(path: Path) -> Path:
    decision = {
        "schema": "pi-package-decision-v1",
        "run_id": DECISION_RUN_ID,
        "status": "accepted",
        "accepted": True,
        "expiry": "2099-01-01T00:00:00.000Z",
    }
    path.write_text(json.dumps(decision), encoding="utf-8")
    return path


class _Server:
    def __init__(self, tmp_path: Path, port: int = 0) -> None:
        self.tmp_path = tmp_path
        self.secret = "PI-CONTRACT-SECRET-DO-NOT-ECHO"
        self.project = tmp_path / "project"
        self.project.mkdir(exist_ok=True)
        self.decision = _write_decision(tmp_path / "decision.json")
        self.database = tmp_path / "events.sqlite"
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self._stdout_queue: queue.Queue[str] = queue.Queue()
        env = os.environ.copy()
        env.pop("PI_KERNEL_HOST", None)
        env["NO_PROXY"] = "127.0.0.1,localhost"
        self.process = subprocess.Popen(
            [
                "node", str(SERVER), "--host", "127.0.0.1", "--port", str(port),
                "--project-root", str(ROOT), "--decision-path", str(self.decision),
                "--database-path", str(self.database), "--cwd", str(self.project),
                "--agent-dir", str(tmp_path / "agent"),
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        line = self._stdout_queue.get(timeout=15)
        payload = json.loads(line)
        assert payload["event"] == "listening", self.output
        self.port = int(payload["port"])

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.stdout.append(line)
            self._stdout_queue.put(line)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        self.stderr.extend(self.process.stderr.readlines())

    @property
    def output(self) -> str:
        return "".join(self.stdout + self.stderr)

    def stop(self, force: bool = False) -> None:
        if self.process.poll() is None:
            (self.process.kill if force else self.process.terminate)()
        self.process.wait(timeout=10)


def _request(server: _Server, method: str, path: str, body: object | None = None, headers: dict[str, str] | None = None):
    data = None if body is None else json.dumps(body).encode()
    request = Request(
        f"http://127.0.0.1:{server.port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with OPENER.open(request, timeout=5) as response:
            return response.status, response.read().decode()
    except HTTPError as error:
        return error.code, error.read().decode()


def _fingerprints() -> dict[str, tuple[bool, int, str]]:
    paths = {
        "canonical_db": ROOT / "data" / "canonical" / "agent" / "structured" / "db" / "agent_conversations.sqlite",
        "active_pointer": ROOT / "var" / "db" / "knowledge_index_active.txt",
        "personal_db": ROOT / "var" / "db" / "personal_system.sqlite",
    }
    result = {}
    for name, path in paths.items():
        if not path.exists():
            result[name] = (False, 0, "")
            continue
        result[name] = (True, path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
    return result


def test_health_readiness_and_safe_route_contract(tmp_path: Path) -> None:
    server = _Server(tmp_path)
    before = _fingerprints()
    try:
        status, body = _request(server, "GET", "/health")
        assert status == 200
        assert json.loads(body)["ok"] is True
        status, body = _request(server, "GET", "/ready")
        ready = json.loads(body)
        assert status == 200 and ready["ready"] is True
        assert all(ready["checks"].values())

        status, body = _request(server, "GET", "/not-a-route")
        assert status == 404 and json.loads(body) == {"ok": False, "error": {"code": "route_not_found"}}
        status, body = _request(server, "POST", "/health", {"body": server.secret, "path": str(tmp_path)})
        assert status == 405 and json.loads(body) == {"ok": False, "error": {"code": "method_not_allowed"}}
        status, body = _request(server, "POST", "/v1/events", {"type": "unknown", "body": server.secret, "path": str(tmp_path)})
        assert status == 400 and json.loads(body) == {"ok": False, "error": {"code": "event_invalid"}}
        assert server.secret not in body
        assert str(tmp_path) not in body
    finally:
        server.stop()
    assert server.secret not in server.output
    assert str(tmp_path) not in server.output
    assert _fingerprints() == before


def test_non_loopback_bind_and_port_conflict_fail_closed(tmp_path: Path) -> None:
    decision = _write_decision(tmp_path / "decision.json")
    common = [
        "node", str(SERVER), "--project-root", str(ROOT), "--decision-path", str(decision),
        "--database-path", str(tmp_path / "events.sqlite"), "--cwd", str(tmp_path),
        "--agent-dir", str(tmp_path / "agent"),
    ]
    rejected = subprocess.run(common + ["--host", "0.0.0.0", "--port", "0"], cwd=ROOT, text=True, capture_output=True, timeout=15)
    assert rejected.returncode != 0
    assert "non_loopback_bind" in rejected.stderr
    assert str(tmp_path) not in rejected.stdout + rejected.stderr

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        conflict = subprocess.run(common + ["--host", "127.0.0.1", "--port", str(listener.getsockname()[1])], cwd=ROOT, text=True, capture_output=True, timeout=15)
        assert conflict.returncode != 0
        assert "host_bind_failed" in conflict.stderr
        listener.getsockname()
    finally:
        listener.close()
