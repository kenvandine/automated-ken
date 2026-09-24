"""Tests for Snapcraft Store credential propagation (github/secrets_sync.py)."""

from __future__ import annotations

import base64

from nacl import encoding, public

from snap_dashboard.github import secrets_sync


def _make_keypair_b64() -> tuple[public.PrivateKey, str]:
    """Return (private_key, base64_public_key) for round-trip decryption in tests."""
    private_key = public.PrivateKey.generate()
    public_key_b64 = private_key.public_key.encode(encoding.Base64Encoder).decode("utf-8")
    return private_key, public_key_b64


def test_encrypt_secret_round_trips():
    private_key, public_key_b64 = _make_keypair_b64()
    encrypted_b64 = secrets_sync.encrypt_secret(public_key_b64, "super-secret-macaroon")

    sealed_box = public.SealedBox(private_key)
    decrypted = sealed_box.decrypt(base64.b64decode(encrypted_b64))
    assert decrypted == b"super-secret-macaroon"


def test_set_repo_secret_encrypts_with_repo_public_key(monkeypatch):
    _, public_key_b64 = _make_keypair_b64()
    monkeypatch.setattr(
        secrets_sync,
        "_get_repo_public_key",
        lambda owner, repo, token: ("key-id-123", public_key_b64),
    )

    put_calls = []

    class _FakeResp:
        status_code = 201

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def put(self, url, json, headers):
            put_calls.append((url, json))
            return _FakeResp()

    monkeypatch.setattr(secrets_sync.httpx, "Client", lambda timeout=15: _FakeClient())

    ok = secrets_sync.set_repo_secret("kenvandine", "some-snap", "SNAPCRAFT_STORE_CREDENTIALS", "value", "tok")
    assert ok is True
    assert len(put_calls) == 1
    url, payload = put_calls[0]
    assert url.endswith("/repos/kenvandine/some-snap/actions/secrets/SNAPCRAFT_STORE_CREDENTIALS")
    assert payload["key_id"] == "key-id-123"
    assert "encrypted_value" in payload


def test_set_repo_secret_returns_false_when_no_public_key(monkeypatch):
    monkeypatch.setattr(secrets_sync, "_get_repo_public_key", lambda owner, repo, token: None)
    ok = secrets_sync.set_repo_secret("kenvandine", "some-snap", "SNAPCRAFT_STORE_CREDENTIALS", "value", "tok")
    assert ok is False


def test_sync_snapcraft_credentials_uses_detected_legacy_secret_name(monkeypatch):
    monkeypatch.setattr(
        secrets_sync,
        "_detect_secret_names",
        lambda owner, repo, token: {"STORE_LOGIN"} if repo == "legacy-snap" else set(),
    )
    set_calls = []
    monkeypatch.setattr(
        secrets_sync,
        "set_repo_secret",
        lambda owner, repo, name, value, token: set_calls.append((repo, name)) or True,
    )

    results = secrets_sync.sync_snapcraft_credentials(
        ["kenvandine/legacy-snap", "kenvandine/new-snap"], "creds", "tok"
    )

    assert {r["repo"]: r["secret_names"] for r in results} == {
        "kenvandine/legacy-snap": ["STORE_LOGIN"],
        "kenvandine/new-snap": ["SNAPCRAFT_STORE_CREDENTIALS"],
    }
    assert all(r["ok"] for r in results)
    assert ("legacy-snap", "STORE_LOGIN") in set_calls
    assert ("new-snap", "SNAPCRAFT_STORE_CREDENTIALS") in set_calls


def test_sync_snapcraft_credentials_reports_invalid_repo():
    results = secrets_sync.sync_snapcraft_credentials(["not-a-valid-repo-slug"], "creds", "tok")
    assert results == [{"repo": "not-a-valid-repo-slug", "secret_names": [], "ok": False}]
