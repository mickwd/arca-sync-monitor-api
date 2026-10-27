"""Sincronización incremental ARCA -> base local -> receptor."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, date, datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Comprobante, EstadoEnvio, SyncRun
from app.services.arca_client import ArcaClient, ArcaComprobante
from app.services.receptor_client import ReceptorClient
from app.services.retry import RetryExhaustedError

logger = logging.getLogger(__name__)

# Evita dos sincronizaciones simultáneas dentro del mismo proceso (scheduler + manual).
# Con varias réplicas habría que usar un lock en la base (ej. pg_advisory_lock).
_sync_lock = threading.Lock()

MAX_PUSH_ATTEMPTS = 10


class SyncAlreadyRunningError(Exception):
    pass


class SyncService:
    def __init__(
        self,
        db: Session,
        arca: ArcaClient,
        receptor: ReceptorClient,
        *,
        initial_date: date,
    ):
        self.db = db
        self.arca = arca
        self.receptor = receptor
        self.initial_date = initial_date

    def compute_since(self) -> date:
        """Fecha desde la cual pedir a ARCA: la última fecha sincronizada (inclusive).

        Se repite el último día a propósito porque pueden haberse emitido más comprobantes
        después de la corrida anterior; los repetidos se descartan en el dedupe.
        """
        last = self.db.scalar(
            select(func.max(Comprobante.fecha_emision)).where(
                Comprobante.cuit_emisor == self.arca.cuit
            )
        )
        return last or self.initial_date

    def run(self, trigger: str = "manual") -> SyncRun:
        if not _sync_lock.acquire(blocking=False):
            raise SyncAlreadyRunningError("Ya hay una sincronización en curso")
        try:
            return self._run(trigger)
        finally:
            _sync_lock.release()

    def _run(self, trigger: str) -> SyncRun:
        run = SyncRun(trigger=trigger, status="running")
        self.db.add(run)
        self.db.commit()
        log_ctx = {"sync_run_id": run.id, "trigger": trigger}
        logger.info("Sincronización iniciada", extra=log_ctx)

        try:
            since = self.compute_since()
            run.since_date = since
            items = self.arca.fetch_comprobantes(desde=since)
            run.fetched = len(items)
            run.created, run.duplicates = self._store(items, run.id)
            self.db.commit()

            run.pushed, run.push_failed = self.push_pending()
            run.status = "success" if run.push_failed == 0 else "partial"
        except (RetryExhaustedError, httpx.HTTPError) as exc:
            self.db.rollback()
            run.status = "failed"
            run.error = str(exc)
            logger.error("Sincronización fallida", extra={**log_ctx, "error": str(exc)})
        finally:
            run.finished_at = datetime.now(UTC)
            self.db.add(run)
            self.db.commit()

        logger.info(
            "Sincronización finalizada",
            extra={
                **log_ctx,
                "status": run.status,
                "fetched": run.fetched,
                "inserted": run.created,
                "duplicates": run.duplicates,
                "pushed": run.pushed,
                "push_failed": run.push_failed,
            },
        )
        return run

    def _store(self, items: list[ArcaComprobante], run_id: int) -> tuple[int, int]:
        """Inserta los comprobantes nuevos. Devuelve (creados, duplicados)."""
        if not items:
            return 0, 0
        min_date = min(i.fecha_emision for i in items)
        existing = {
            tuple(row)
            for row in self.db.execute(
                select(
                    Comprobante.cuit_emisor,
                    Comprobante.tipo_comprobante,
                    Comprobante.punto_venta,
                    Comprobante.numero,
                ).where(
                    Comprobante.cuit_emisor.in_({i.cuit_emisor for i in items}),
                    Comprobante.fecha_emision >= min_date,
                )
            )
        }
        created = duplicates = 0
        for item in items:
            if item.clave in existing:
                duplicates += 1
                continue
            existing.add(item.clave)  # también deduplica dentro del mismo lote
            self.db.add(Comprobante(**item.model_dump(), sync_run_id=run_id))
            created += 1
        return created, duplicates

    def push_pending(self) -> tuple[int, int]:
        """Envía al receptor los pendientes y los fallidos que todavía tienen intentos."""
        pending = self.db.scalars(
            select(Comprobante)
            .where(
                Comprobante.estado_envio.in_([EstadoEnvio.PENDING, EstadoEnvio.FAILED]),
                Comprobante.intentos_envio < MAX_PUSH_ATTEMPTS,
            )
            .order_by(Comprobante.id)
        ).all()

        pushed = failed = 0
        for comp in pending:
            comp.intentos_envio += 1
            try:
                ack = self.receptor.push(comp)
            except (RetryExhaustedError, httpx.HTTPError) as exc:
                comp.estado_envio = EstadoEnvio.FAILED
                comp.ultimo_error = str(exc)[:1000]
                failed += 1
                logger.warning(
                    "No se pudo enviar el comprobante",
                    extra={"clave": comp.clave, "error": str(exc)},
                )
            else:
                comp.estado_envio = EstadoEnvio.SENT
                comp.receptor_ack_id = ack.ack_id
                comp.ultimo_error = None
                comp.sent_at = datetime.now(UTC)
                pushed += 1
            self.db.commit()
        return pushed, failed
