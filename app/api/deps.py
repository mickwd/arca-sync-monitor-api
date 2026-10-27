"""Dependencias de FastAPI: settings, sesión de base, autenticación y servicios."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import Principal, TokenError, decode_access_token
from app.monitoring.service import MonitoringService
from app.services.arca_client import ArcaClient, StaticTokenAuth, TokenEndpointAuth
from app.services.receptor_client import ReceptorClient
from app.services.retry import RetryPolicy
from app.services.sync_service import SyncService

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_current_principal(
    token: str = Depends(oauth2_scheme), settings: Settings = Depends(get_settings)
) -> Principal:
    try:
        return decode_access_token(settings, token)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o vencido",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def require_scopes(*scopes: str) -> Callable[..., Principal]:
    def checker(principal: Principal = Depends(get_current_principal)) -> Principal:
        if not principal.has_scopes(list(scopes)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Faltan permisos: {' '.join(scopes)}",
            )
        return principal

    return checker


def build_arca_client(settings: Settings) -> ArcaClient:
    auth = (
        TokenEndpointAuth(settings.arca_cuit)
        if settings.arca_auth_mode == "token"
        else StaticTokenAuth(settings.arca_static_token)
    )
    return ArcaClient(
        settings.arca_base_url,
        settings.arca_cuit,
        auth,
        retry_policy=_retry_policy(settings),
        timeout=settings.arca_timeout_seconds,
        page_size=settings.arca_page_size,
    )


def build_receptor_client(settings: Settings) -> ReceptorClient:
    return ReceptorClient(
        settings.receptor_base_url,
        settings.receptor_api_key,
        retry_policy=_retry_policy(settings),
        timeout=settings.receptor_timeout_seconds,
    )


def _retry_policy(settings: Settings) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=settings.retry_max_attempts,
        base_delay=settings.retry_base_delay_seconds,
        max_delay=settings.retry_max_delay_seconds,
    )


def get_sync_service(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> Iterator[SyncService]:
    arca = build_arca_client(settings)
    receptor = build_receptor_client(settings)
    try:
        yield SyncService(db, arca, receptor, initial_date=settings.sync_initial_date)
    finally:
        arca.close()
        receptor.close()


def get_monitoring_service(request: Request) -> MonitoringService:
    return request.app.state.monitoring
