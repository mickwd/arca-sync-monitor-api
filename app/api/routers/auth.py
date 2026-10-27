"""Endpoint de tokens OAuth2 (grant_type=password y client_credentials)."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_principal, get_db, get_settings
from app.core.config import Settings
from app.core.security import Principal, create_access_token, verify_password
from app.db.models import User
from app.schemas.common import TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


def _oauth_error(code: str, description: str, http_status: int = 400) -> HTTPException:
    # Formato de error de RFC 6749 sección 5.2
    return HTTPException(
        status_code=http_status, detail={"error": code, "error_description": description}
    )


def _resolve_scopes(granted: list[str], requested: str | None) -> list[str]:
    if not requested:
        return granted
    wanted = requested.split()
    if not set(wanted) <= set(granted):
        raise _oauth_error("invalid_scope", "Se pidieron scopes no autorizados")
    return wanted


@router.post("/token", response_model=TokenResponse)
def issue_token(
    grant_type: Annotated[str, Form()],
    username: Annotated[str | None, Form()] = None,
    password: Annotated[str | None, Form()] = None,
    client_id: Annotated[str | None, Form()] = None,
    client_secret: Annotated[str | None, Form()] = None,
    scope: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    if grant_type == "password":
        if not username or not password:
            raise _oauth_error("invalid_request", "username y password son obligatorios")
        user = db.scalar(select(User).where(User.username == username))
        if not user or not user.is_active or not verify_password(password, user.hashed_password):
            raise _oauth_error(
                "invalid_grant", "Credenciales inválidas", status.HTTP_401_UNAUTHORIZED
            )
        subject, kind, granted = user.username, "user", user.scopes.split()

    elif grant_type == "client_credentials":
        valid = (
            client_id is not None
            and client_secret is not None
            and hmac.compare_digest(client_id, settings.oauth_client_id)
            and hmac.compare_digest(client_secret, settings.oauth_client_secret)
        )
        if not valid:
            raise _oauth_error("invalid_client", "Cliente inválido", status.HTTP_401_UNAUTHORIZED)
        subject, kind, granted = settings.oauth_client_id, "client", settings.client_scopes

    else:
        raise _oauth_error("unsupported_grant_type", f"grant_type no soportado: {grant_type}")

    scopes = _resolve_scopes(granted, scope)
    token, expires_in = create_access_token(settings, subject, kind, scopes)
    return TokenResponse(access_token=token, expires_in=expires_in, scope=" ".join(scopes))


@router.get("/me")
def me(principal: Principal = Depends(get_current_principal)) -> dict:
    return {"subject": principal.subject, "kind": principal.kind, "scopes": principal.scopes}
