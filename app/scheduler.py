"""Tareas programadas dentro del proceso de la API (APScheduler).

Alternativa: dejar SCHEDULER_ENABLED=false y disparar `POST /sync/run` desde cron
(ver scripts/crontab.example). Con varias réplicas conviene esa opción o habilitar el
scheduler en una sola instancia para no duplicar ejecuciones.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI

from app.api.deps import build_arca_client, build_receptor_client
from app.services.sync_service import SyncAlreadyRunningError, SyncService

logger = logging.getLogger(__name__)


def scheduled_sync(app: FastAPI) -> None:
    settings = app.state.settings
    arca = build_arca_client(settings)
    receptor = build_receptor_client(settings)
    try:
        with app.state.session_factory() as db:
            SyncService(db, arca, receptor, initial_date=settings.sync_initial_date).run(
                trigger="scheduled"
            )
    except SyncAlreadyRunningError:
        logger.info("Sync programada omitida: ya hay una en curso")
    except Exception:
        logger.exception("Error en la sync programada")
    finally:
        arca.close()
        receptor.close()


def scheduled_monitoring(app: FastAPI) -> None:
    try:
        app.state.monitoring.run_checks()
    except Exception:
        logger.exception("Error en el chequeo de monitoreo programado")


def start_scheduler(app: FastAPI) -> BackgroundScheduler:
    settings = app.state.settings
    scheduler = BackgroundScheduler(timezone="America/Argentina/Buenos_Aires")
    scheduler.add_job(
        scheduled_sync,
        "interval",
        minutes=settings.sync_interval_minutes,
        args=[app],
        id="arca_sync",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        scheduled_monitoring,
        "interval",
        seconds=settings.monitoring_interval_seconds,
        args=[app],
        id="monitoring",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Scheduler iniciado", extra={"jobs": [j.id for j in scheduler.get_jobs()]})
    return scheduler
