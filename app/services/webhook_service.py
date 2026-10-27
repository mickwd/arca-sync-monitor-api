"""Procesamiento de webhooks enviados por el receptor (confirmaciones y rechazos)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Comprobante, EstadoEnvio, WebhookEvent
from app.schemas.webhooks import ReceptorWebhook

logger = logging.getLogger(__name__)

EVENT_STATUS = {
    "comprobante.recibido": EstadoEnvio.CONFIRMED,
    "comprobante.rechazado": EstadoEnvio.REJECTED,
}


def parse_clave(clave: str) -> tuple[str, int, int, int]:
    """'20123456789-001-00002-00000123' -> ('20123456789', 1, 2, 123)."""
    cuit, tipo, pv, numero = clave.split("-")
    return cuit, int(tipo), int(pv), int(numero)


class WebhookService:
    def __init__(self, db: Session):
        self.db = db

    def handle(self, event: ReceptorWebhook) -> tuple[WebhookEvent, bool]:
        """Registra y procesa el evento. Devuelve (evento, es_duplicado).

        La idempotencia se apoya en el UNIQUE de `event_id`: si el receptor reenvía el mismo
        evento (reintento de su lado), se responde OK sin volver a procesarlo.
        """
        existing = self.db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == event.id))
        if existing:
            return existing, True

        record = WebhookEvent(
            event_id=event.id,
            event_type=event.type,
            payload=event.model_dump(mode="json"),
        )
        self.db.add(record)
        try:
            self.db.flush()
        except IntegrityError:
            # Carrera entre dos entregas simultáneas del mismo evento
            self.db.rollback()
            existing = self.db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == event.id))
            assert existing is not None
            return existing, True

        self._process(record, event)
        record.processed_at = datetime.now(UTC)
        self.db.commit()
        return record, False

    def _process(self, record: WebhookEvent, event: ReceptorWebhook) -> None:
        new_status = EVENT_STATUS.get(event.type)
        if new_status is None:
            record.status = "ignored"
            logger.info("Tipo de evento ignorado", extra={"event_type": event.type})
            return

        try:
            cuit, tipo, pv, numero = parse_clave(event.data.clave)
        except ValueError:
            record.status = "error"
            record.error = f"Clave inválida: {event.data.clave}"
            return

        comp = self.db.scalar(
            select(Comprobante).where(
                Comprobante.cuit_emisor == cuit,
                Comprobante.tipo_comprobante == tipo,
                Comprobante.punto_venta == pv,
                Comprobante.numero == numero,
            )
        )
        if comp is None:
            record.status = "error"
            record.error = f"Comprobante inexistente: {event.data.clave}"
            logger.warning("Webhook de comprobante inexistente", extra={"clave": event.data.clave})
            return

        comp.estado_envio = new_status
        if new_status == EstadoEnvio.REJECTED:
            comp.ultimo_error = event.data.motivo or "Rechazado por el receptor"
        if event.data.receptor_id:
            comp.receptor_ack_id = event.data.receptor_id
        record.status = "processed"
