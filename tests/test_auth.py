from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt

from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from tests.conftest import get_token


def test_password_hash_roundtrip():
    hashed = hash_password("clave-segura", iterations=1_000)
    assert hashed.startswith("pbkdf2_sha256$1000$")
    assert verify_password("clave-segura", hashed)
    assert not verify_password("otra", hashed)
    assert not verify_password("clave-segura", "formato-roto")


def test_password_grant_issues_valid_jwt(client, settings):
    resp = client.post(
        "/auth/token", data={"grant_type": "password", "username": "admin", "password": "s3cret"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.access_token_expire_minutes * 60

    principal = decode_access_token(settings, body["access_token"])
    assert principal.subject == "admin"
    assert principal.kind == "user"
    assert "sync:write" in principal.scopes


def test_password_grant_wrong_password(client):
    resp = client.post(
        "/auth/token", data={"grant_type": "password", "username": "admin", "password": "nope"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_grant"


def test_client_credentials_grant(client):
    resp = client.post(
        "/auth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "test-client",
            "client_secret": "test-client-secret",
        },
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["kind"] == "client"
    assert me.json()["subject"] == "test-client"


def test_client_credentials_invalid_secret(client):
    resp = client.post(
        "/auth/token",
        data={"grant_type": "client_credentials", "client_id": "test-client", "client_secret": "x"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_client"


def test_unsupported_grant_type(client):
    resp = client.post("/auth/token", data={"grant_type": "implicit"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unsupported_grant_type"


def test_requesting_unknown_scope_is_rejected(client):
    resp = client.post(
        "/auth/token",
        data={
            "grant_type": "password",
            "username": "admin",
            "password": "s3cret",
            "scope": "admin:all",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_scope"


def test_protected_endpoint_requires_token(client):
    assert client.get("/comprobantes").status_code == 401


def test_tampered_token_is_rejected(client):
    token = get_token(client)
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    resp = client.get("/comprobantes", headers={"Authorization": f"Bearer {tampered}"})
    assert resp.status_code == 401


def test_expired_token_is_rejected(client, settings):
    now = datetime.now(UTC)
    expired = jwt.encode(
        {
            "sub": "admin",
            "scope": "comprobantes:read",
            "iss": settings.jwt_issuer,
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    resp = client.get("/comprobantes", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401


def test_insufficient_scope_returns_403(client):
    token = get_token(client, scope="monitoring:read")
    resp = client.post("/sync/run", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_token_signed_with_other_secret_is_rejected(client, settings):
    other = settings.model_copy(update={"jwt_secret": "another-secret-0123456789abcdef"})
    token, _ = create_access_token(other, "admin", "user", ["comprobantes:read"])
    resp = client.get("/comprobantes", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_request_id_header_is_propagated(client):
    resp = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert resp.headers["X-Request-ID"] == "abc-123"
    assert client.get("/health").headers["X-Request-ID"]
