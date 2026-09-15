"""Unit tests for retrieval-exit privacy_guard sealing (v2 expanded scope)."""

from __future__ import annotations

import pytest

from personal_knowledge.core.privacy_guard import (
    POLICY_ID,
    guard_jsonable,
    guard_mcp_payload,
    guard_text,
    iter_rule_names,
    seal_value,
)


@pytest.fixture(autouse=True)
def _seal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONAL_DATA_PRIVACY_GUARD", "1")
    monkeypatch.setenv("PERSONAL_DATA_PRIVACY_MODE", "redact")
    monkeypatch.setenv("PERSONAL_DATA_PRIVACY_SEAL_KEY", "unit-test-seal-key")
    monkeypatch.setenv("PERSONAL_DATA_PRIVACY_SCOPE", "credentials,pii,fields")


def test_openai_key_sealed() -> None:
    raw = "my key is sk-" + ("a" * 32)
    r = guard_text(raw)
    assert "sk-aaaaaaaa" not in r.text
    assert r.hit_count == 1
    assert "openai-key" in r.kinds
    assert "[PRIVACY:openai-key:fp:" in r.text


def test_anthropic_not_stolen_by_openai() -> None:
    raw = "sk-ant-api03-" + ("b" * 40)
    r = guard_text(raw)
    assert "sk-ant-" not in r.text or "[PRIVACY:" in r.text
    assert "anthropic-key" in r.kinds
    assert "openai-key" not in r.kinds


def test_assignment_and_env_sealed() -> None:
    raw = "api_key=supersecretvalue99\nOPENAI_API_KEY=anothersecret99"
    r = guard_text(raw)
    assert "supersecretvalue99" not in r.text
    assert "anothersecret99" not in r.text
    assert r.hit_count >= 2


def test_bearer_and_github() -> None:
    raw = "Authorization: Bearer " + ("B" * 24) + " token=ghp_" + ("c" * 36)
    r = guard_text(raw)
    assert "B" * 24 not in r.text
    assert "ghp_" not in r.text
    assert r.hit_count >= 2


def test_google_and_aws() -> None:
    g = "AIza" + ("d" * 35)
    aws = "AKIA" + ("E" * 16)
    r = guard_text(f"g={g} aws={aws}")
    assert g not in r.text
    assert aws not in r.text


def test_expanded_provider_tokens() -> None:
    samples = [
        ("stripe-key", "sk_live_" + "x" * 24),
        ("huggingface-token", "hf_" + "y" * 30),
        ("npm-token", "npm_" + "z" * 30),
        ("gitlab-token", "glpat-" + "a" * 24),
        ("xai-key", "xai-" + "b" * 30),
        ("sendgrid-key", "SG." + ("c" * 22) + "." + ("d" * 22)),
        ("linear-token", "lin_api_" + "e" * 24),
        ("telegram-bot", "123456789:" + ("f" * 35)),
    ]
    for kind, secret in samples:
        r = guard_text(f"leak {secret} end")
        assert secret not in r.text, kind
        assert r.hit_count >= 1, kind


def test_private_key_block() -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"  # governance: synthetic-secret-fixture
        "MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF6PZGFw6\n"
        "-----END RSA PRIVATE KEY-----"
    )
    r = guard_text(f"here:\n{pem}\nok")
    assert "BEGIN RSA PRIVATE KEY" not in r.text
    assert "private-key" in r.kinds


def test_connection_and_url_password() -> None:
    raw = "postgres://user:SuperPass99@localhost:5432/db https://u:SecretPass1@api.example.com/v1"
    r = guard_text(raw)
    assert "SuperPass99" not in r.text
    assert "SecretPass1" not in r.text


def test_zh_secret_label() -> None:
    raw = "密码：mysecretpass 密钥=anotherkey99"
    r = guard_text(raw)
    assert "mysecretpass" not in r.text
    assert "anotherkey99" not in r.text
    assert r.hit_count >= 2


def test_pii_email_phone_id() -> None:
    email = "alice.demo@example.com"
    phone = "13812345678"
    # 合法结构身份证样例（非真实）
    idcard = "110105199003074477"
    raw = f"联系 {email} 手机 {phone} 证 {idcard}"
    r = guard_text(raw)
    assert email not in r.text
    assert phone not in r.text
    assert idcard not in r.text
    assert "email" in r.kinds
    assert "phone-cn" in r.kinds
    assert "id-card-cn" in r.kinds


def test_bank_card_luhn_gate() -> None:
    # Visa test number (Luhn ok)
    good = "4111111111111111"
    bad = "4111111111111112"  # Luhn fail
    r_good = guard_text(f"card {good}")
    r_bad = guard_text(f"card {bad}")
    assert good not in r_good.text
    assert "bank-card" in r_good.kinds
    # 不通过 Luhn 的不封存
    assert bad in r_bad.text


def test_no_false_positive_on_normal_text() -> None:
    raw = "用户偏好使用 Python 做数据分析，记忆 id=mem_12345，event sha 不扫裸 hex"
    r = guard_text(raw)
    assert r.hit_count == 0
    assert r.text == raw


def test_event_id_hex_not_sealed() -> None:
    # 64 hex 是系统常见 event_id / sha256，禁止无标签裸扫
    eid = "a" * 64
    r = guard_text(f"event_id={eid}")
    assert eid in r.text
    assert r.hit_count == 0


def test_labeled_hex_is_sealed() -> None:
    secret = "ab" * 32
    r = guard_text(f"secret={secret}")
    assert secret not in r.text
    assert r.hit_count >= 1


def test_stable_fingerprint() -> None:
    secret = "sk-" + ("z" * 40)
    a = seal_value("openai-key", secret)
    b = seal_value("openai-key", secret)
    assert a == b
    assert a.startswith("[PRIVACY:openai-key:fp:")


def test_jsonable_recursive_and_field_keys() -> None:
    payload = {
        "rows": [{"content": "token ghp_" + ("x" * 40)}],
        "note": "safe text",
        "password": "hunter2hunter2",
        "memory_type": "preference",
        "api_key": "should-seal-whole-field-value",
    }
    sealed, meta = guard_jsonable(payload)
    assert meta.hit_count >= 3
    assert "ghp_" not in sealed["rows"][0]["content"]
    assert sealed["note"] == "safe text"
    assert "hunter2" not in sealed["password"]
    assert "should-seal" not in sealed["api_key"]
    # deny list 业务字段不整值封存
    assert sealed["memory_type"] == "preference"


def test_jsonable_preserves_typed_ids_and_integrity_hashes() -> None:
    phone_shaped_digest = "a13800138000" + ("b" * 52)
    payload = {
        "ids": ["ors_" + phone_shaped_digest],
        "session_id": "ors_" + phone_shaped_digest,
        "actor_identity_hash": phone_shaped_digest,
        "preview_checksum": phone_shaped_digest,
        "binding": {"external_snapshot_id": "exs_" + phone_shaped_digest},
        "note": "call 13800138000",
    }
    sealed, meta = guard_jsonable(payload)
    assert sealed["ids"] == payload["ids"]
    assert sealed["session_id"] == payload["session_id"]
    assert sealed["actor_identity_hash"] == payload["actor_identity_hash"]
    assert sealed["preview_checksum"] == payload["preview_checksum"]
    assert sealed["binding"] == payload["binding"]
    assert sealed["note"] != payload["note"]
    assert meta.hit_count == 1


def test_scope_credentials_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONAL_DATA_PRIVACY_SCOPE", "credentials")
    email = "bob@example.com"
    key = "sk-" + ("n" * 40)
    r = guard_text(f"{email} {key}")
    assert key not in r.text
    assert email in r.text  # pii 关闭
    assert "email" not in r.kinds


def test_mcp_payload_footnote() -> None:
    raw = "leak sk-" + ("q" * 40)
    out = guard_mcp_payload(raw)
    assert "sk-q" not in out
    assert POLICY_ID in out
    assert "plaintext credentials/PII are not returned" in out


def test_disable_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONAL_DATA_PRIVACY_GUARD", "0")
    raw = "sk-" + ("w" * 40)
    r = guard_text(raw)
    assert r.hit_count == 0
    assert r.text == raw


def test_no_double_seal() -> None:
    secret = "sk-" + ("m" * 40)
    once = guard_text(secret).text
    twice = guard_text(once).text
    assert twice.count("[PRIVACY:") == once.count("[PRIVACY:")


def test_rule_catalog_expanded() -> None:
    names = set(iter_rule_names())
    for expected in (
        "openai-key",
        "stripe-key",
        "huggingface-token",
        "email",
        "phone-cn",
        "id-card-cn",
        "zh-secret-label",
        "connection-password",
    ):
        assert expected in names
    assert len(names) >= 25


def test_mcp_server_exit_uses_guard() -> None:
    import personal_knowledge.services.mcp_server as mcp

    assert mcp.guard_mcp_payload is guard_mcp_payload
    sealed = mcp.guard_mcp_payload("api_key=plainsecretvalue123")
    assert "plainsecretvalue123" not in sealed
