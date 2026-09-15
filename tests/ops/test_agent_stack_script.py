from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops" / "runtime" / "start-agent-stack.ps1"


def _run(*args: str, env: dict[str, str] | None = None, timeout: int = 30):
    return subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(SCRIPT), *args],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=timeout,
    )


def _temp_project(path: Path) -> Path:
    (path / "apps" / "personal_data_chatgpt").mkdir(parents=True)
    (path / "var" / "db").mkdir(parents=True)
    return path


def test_parser_and_zero_write_check(tmp_path: Path) -> None:
    project = _temp_project(tmp_path / "project")
    result = _run("-Mode", "Check", "-SkipTunnel", "-ProjectRoot", str(project))
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"event":"preflight_passed"' in result.stdout
    assert not (project / "ops").exists()


def test_tunnel_has_bounded_extended_readiness_and_early_ownership() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "New-LocalUrl $TunnelHealthPort '/readyz'" in source
    assert "New-LocalUrl $TunnelHealthPort '/healthz'" not in source
    assert "[int]$TunnelStartTimeoutSeconds = 90" in source
    assert "$entries += $entry" in source
    assert source.index("$entries += $entry") < source.index("$entry.Process = New-ManagedProcess $spec")
    assert "Wait-Ready $entry $readinessTimeout" in source
    assert "$start.RedirectStandardOutput = $false" in source
    assert "$start.RedirectStandardError = $false" in source


def test_missing_secret_and_profile_fail_before_runtime() -> None:
    env = os.environ.copy()
    env.pop("CONTROL_PLANE_API_KEY", None)
    result = _run("-Mode", "Check", "-ProjectRoot", str(ROOT), env=env)
    assert result.returncode == 2
    assert "CONTROL_PLANE_API_KEY_missing" in result.stdout
    result = _run(
        "-Mode", "Check", "-ProjectRoot", str(ROOT),
        "-TunnelProfile", "definitely-missing-profile",
    )
    assert result.returncode == 2
    assert "tunnel_profile_missing" in result.stdout


def test_duplicate_ports_fail_without_writes(tmp_path: Path) -> None:
    project = _temp_project(tmp_path / "project")
    result = _run(
        "-Mode", "Check", "-SkipTunnel", "-ProjectRoot", str(project),
        "-RestPort", "28111", "-McpPort", "28111",
    )
    assert result.returncode == 2
    assert "ports_must_be_unique" in result.stdout
    assert not (project / "ops").exists()


def test_unhealthy_port_owner_is_not_terminated(tmp_path: Path) -> None:
    project = _temp_project(tmp_path / "project")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        result = _run(
            "-Mode", "Run", "-SkipTunnel", "-ProjectRoot", str(project),
            "-RestPort", str(port), "-McpPort", str(port + 1),
            "-StartTimeoutSeconds", "2", "-RunForSeconds", "1",
            timeout=20,
        )
        assert result.returncode == 2
        assert "unhealthy_port_conflict:rest" in result.stdout
        listener.getsockname()
    finally:
        listener.close()
