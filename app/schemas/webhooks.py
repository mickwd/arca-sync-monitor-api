from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ReceptorWebhookData(BaseModel):
    clave: str = Field(examples=["20123456789-001-00002-00000123"])
    receptor_id: str | None = None
    motivo: str | None = None


class ReceptorWebhook(BaseModel):
    id: str = Field(min_length=1, max_length=64, description="Id único del evento (idempotencia)")
    type: str = Field(examples=["comprobante.recibido", "comprobante.rechazado"])
    created_at: datetime | None = None
    data: ReceptorWebhookData


class WebhookAck(BaseModel):
    event_id: str
    status: str
    duplicate: bool
