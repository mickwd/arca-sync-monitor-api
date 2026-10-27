"""Punto de entrada de la API: `uvicorn app.main:app`."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select

from app.api.routers import auth, comprobantes, health, monitoring, sync, webhooks
from app.core.config import Settings, get_settings
from app.core.logging import RequestContextMiddleware, setup_logging
from app.core.security import hash_password
from app.db.base import build_engine, build_session_factory, init_db
from app.db.models import User
from app.monitoring.service import MonitoringService

logger = logging.getLogger(__name__)

ALL_SCOPES = "sync:write comprobantes:read monitoring:read"


def seed_admin(settings: Settings, session_factory) -> None:
    """Crea el usuario administrador inicial si todavía no existe."""
    with session_factory() as db:
        if db.scalar(select(User).where(User.username == settings.admin_username)):
            return
        db.add(
            User(
                username=settings.admin_username,
                hashed_password=hash_password(
                    settings.admin_password, settings.password_hash_iterations
                ),
                scopes=ALL_SCOPES,
            )
        )
        db.commit()
        logger.info("Usuario admin creado", extra={"username": settings.admin_username})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level, settings.log_json)

    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # En producción el esquema se gestionaría con Alembic; create_all alcanza para el demo
        init_db(engine)
        seed_admin(settings, session_factory)
        scheduler = None
        if settings.scheduler_enabled:
            from app.scheduler import start_scheduler

            scheduler = start_scheduler(app)
        yield
        if scheduler:
            scheduler.shutdown(wait=False)
        engine.dispose()

    app = FastAPI(
        title="ARCA Sync & Monitor API",
        version="1.0.0",
        description=(
            "Sincronización de comprobantes electrónicos de ARCA hacia un sistema receptor "
            "y monitoreo de servidores Linux / Windows Server."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.monitoring = MonitoringService.from_settings(settings, session_factory)

    app.add_middleware(RequestContextMiddleware)
    for router in (
        health.router,
        auth.router,
        sync.router,
        comprobantes.router,
        webhooks.router,
        monitoring.router,
    ):
        app.include_router(router)
    return app


app = create_app()
