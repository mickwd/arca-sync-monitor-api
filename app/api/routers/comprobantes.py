from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_scopes
from app.db.models import Comprobante
from app.schemas.common import ComprobanteOut, Page

router = APIRouter(
    prefix="/comprobantes",
    tags=["comprobantes"],
    dependencies=[Depends(require_scopes("comprobantes:read"))],
)


@router.get("", response_model=Page[ComprobanteOut])
def list_comprobantes(
    estado: str | None = Query(None, description="pending, sent, failed, confirmed, rejected"),
    cuit_receptor: str | None = Query(None, pattern=r"^\d{11}$"),
    punto_venta: int | None = None,
    desde: date | None = None,
    hasta: date | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    query = select(Comprobante)
    if estado:
        query = query.where(Comprobante.estado_envio == estado)
    if cuit_receptor:
        query = query.where(Comprobante.cuit_receptor == cuit_receptor)
    if punto_venta:
        query = query.where(Comprobante.punto_venta == punto_venta)
    if desde:
        query = query.where(Comprobante.fecha_emision >= desde)
    if hasta:
        query = query.where(Comprobante.fecha_emision <= hasta)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = db.scalars(
        query.order_by(Comprobante.fecha_emision.desc(), Comprobante.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{comprobante_id}", response_model=ComprobanteOut)
def get_comprobante(comprobante_id: int, db: Session = Depends(get_db)) -> Comprobante:
    comp = db.get(Comprobante, comprobante_id)
    if comp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Comprobante no encontrado")
    return comp
