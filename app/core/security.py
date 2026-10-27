"""Hashing de contraseñas (PBKDF2-SHA256, stdlib) y emisión/validación de JWT."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import Settings

_HASH_ALGO = "pbkdf2_sha256"


def hash_password(password: str, iterations: int = 210_000) -> str:
    """Devuelve un hash con formato `pbkdf2_sha256$iteraciones$salt$hash` (base64)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join(
        [
            _HASH_ALGO,
            str(iterations),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = encoded.split("$")
    except ValueError:
        return False
    if algo != _HASH_ALGO:
        return False
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(digest_b64)
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
    return hmac.compare_digest(candidate, expected)


class TokenError(Exception):
    """Token inválido, vencido o con firma incorrecta."""


@dataclass(frozen=True)
class Principal:
    """Identidad autenticada: un usuario humano o un cliente de integración."""

    subject: str
    kind: str  # "user" | "client"
    scopes: list[str] = field(default_factory=list)

    def has_scopes(self, required: list[str]) -> bool:
        return all(s in self.scopes for s in required)


def create_access_token(
    settings: Settings, subject: str, kind: str, scopes: list[str]
) -> tuple[str, int]:
    """Genera un JWT firmado. Devuelve (token, segundos_de_validez)."""
    now = datetime.now(UTC)
    expires_in = settings.access_token_expire_minutes * 60
    payload = {
        "sub": subject,
        "typ": kind,
        "scope": " ".join(scopes),
        "iss": settings.jwt_issuer,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(seconds=expires_in),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_in


def decode_access_token(settings: Settings, token: str) -> Principal:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "sub", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    return Principal(
        subject=payload["sub"],
        kind=payload.get("typ", "user"),
        scopes=payload.get("scope", "").split(),
    )


def compute_webhook_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Firma HMAC-SHA256 sobre `{timestamp}.{body}` (mismo esquema que usa el receptor)."""
    message = timestamp.encode() + b"." + body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_webhook_signature(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    expected = compute_webhook_signature(secret, timestamp, body)
    return hmac.compare_digest(expected, signature)
