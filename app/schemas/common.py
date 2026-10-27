from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    scope: str


class ComprobanteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    clave: str
    tipo_comprobante: int
    punto_venta: int
    numero: int
    cuit_emisor: str
    cuit_receptor: str
    fecha_emision: date
    importe_total: Decimal
    moneda: str
    cae: str
    cae_vencimiento: date | None
    estado_envio: str
    intentos_envio: int
    receptor_ack_id: str | None
    ultimo_error: str | None
    sent_at: datetime | None


class SyncRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trigger: str
    status: str
    since_date: date | None
    fetched: int
    created: int
    duplicates: int
    pushed: int
    push_failed: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    host: str
    metric: str
    severity: str
    value: float | None
    threshold: float | None
    message: str
    notified: bool
    created_at: datetime
