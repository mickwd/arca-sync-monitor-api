"""Recepción de webhooks del receptor con verificación HMAC e idempotencia."""

from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_settings
from app.core.config import Settings
from app.core.security import verify_webhook_signature
from app.schemas.webhooks import ReceptorWebhook, WebhookAck
from app.services.webhook_service import WebhookService

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


@router.post("/receptor", response_model=WebhookAck)
async def receptor_webhook(
    request: Request,
    x_signature: Annotated[str, Header(description="sha256=<hex HMAC de '{timestamp}.{body}'>")],
    x_timestamp: Annotated[str, Header(description="Epoch en segundos")],
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> WebhookAck:
    # La firma se calcula sobre el body crudo, antes de parsear el JSON
    body = await request.body()

    try:
        ts = int(x_timestamp)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-Timestamp inválido") from exc
    if abs(time.time() - ts) > settings.webhook_tolerance_seconds:
        # Protege contra replay de requests viejos capturados
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Timestamp fuera de tolerancia")

    if not verify_webhook_signature(settings.webhook_secret, x_timestamp, body, x_signature):
        logger.warning("Firma de webhook inválida")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Firma inválida")

    try:
        event = ReceptorWebhook.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False)) from exc

    record, duplicate = WebhookService(db).handle(event)
    logger.info(
        "Webhook recibido",
        extra={"event_id": event.id, "event_type": event.type, "duplicate": duplicate},
    )
    return WebhookAck(event_id=record.event_id, status=record.status, duplicate=duplicate)
