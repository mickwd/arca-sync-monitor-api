"""Modelos ORM: usuarios, comprobantes, corridas de sync, eventos de webhook y alertas."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    scopes: Mapped[str] = mapped_column(String(255), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EstadoEnvio:
    """Estados del comprobante respecto del receptor."""

    PENDING = "pending"  # sincronizado desde ARCA, falta enviar
    SENT = "sent"  # el receptor respondió 2xx
    FAILED = "failed"  # se agotaron los reintentos
    CONFIRMED = "confirmed"  # el receptor confirmó por webhook
    REJECTED = "rejected"  # el receptor lo rechazó por webhook


class Comprobante(Base):
    __tablename__ = "comprobantes"
    __table_args__ = (
        UniqueConstraint(
            "cuit_emisor", "tipo_comprobante", "punto_venta", "numero", name="uq_comprobante"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cuit_emisor: Mapped[str] = mapped_column(String(11), index=True)
    cuit_receptor: Mapped[str] = mapped_column(String(11))
    tipo_comprobante: Mapped[int] = mapped_column(Integer)
    punto_venta: Mapped[int] = mapped_column(Integer)
    numero: Mapped[int] = mapped_column(Integer)
    fecha_emision: Mapped[date] = mapped_column(Date, index=True)
    importe_total: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    moneda: Mapped[str] = mapped_column(String(3), default="PES")
    cae: Mapped[str] = mapped_column(String(14))
    cae_vencimiento: Mapped[date | None] = mapped_column(Date, nullable=True)

    estado_envio: Mapped[str] = mapped_column(String(16), default=EstadoEnvio.PENDING, index=True)
    intentos_envio: Mapped[int] = mapped_column(Integer, default=0)
    receptor_ack_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ultimo_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    @property
    def clave(self) -> str:
        """Clave natural: CUIT-TIPO-PV-NUMERO (ej. 20123456789-001-00002-00000123)."""
        return (
            f"{self.cuit_emisor}-{self.tipo_comprobante:03d}-"
            f"{self.punto_venta:05d}-{self.numero:08d}"
        )


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    since_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    pushed: Mapped[int] = mapped_column(Integer, default=0)
    push_failed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="received")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    host: Mapped[str] = mapped_column(String(128), index=True)
    metric: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str] = mapped_column(Text)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
