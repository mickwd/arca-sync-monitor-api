from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_monitoring_service, require_scopes
from app.db.models import Alert
from app.monitoring.models import HostMetrics
from app.monitoring.service import MonitoringService
from app.schemas.common import AlertOut, Page

router = APIRouter(
    prefix="/monitoring",
    tags=["monitoring"],
    dependencies=[Depends(require_scopes("monitoring:read"))],
)


@router.get("/hosts", response_model=list[HostMetrics], summary="Último snapshot de cada host")
async def list_hosts(
    refresh: bool = Query(False, description="Fuerza una recolección antes de responder"),
    service: MonitoringService = Depends(get_monitoring_service),
) -> list[HostMetrics]:
    if refresh or not service.latest():
        return await run_in_threadpool(service.run_checks)
    return service.latest()


@router.get("/hosts/{name}/metrics", response_model=HostMetrics, summary="Métricas en vivo")
async def host_metrics(
    name: str, service: MonitoringService = Depends(get_monitoring_service)
) -> HostMetrics:
    if name not in service.collectors:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Host desconocido: {name}")
    return await run_in_threadpool(service.collect_host, name)


@router.post("/check", response_model=list[HostMetrics], summary="Ejecuta chequeos y alertas")
async def run_checks(service: MonitoringService = Depends(get_monitoring_service)):
    return await run_in_threadpool(service.run_checks)


@router.get("/alerts", response_model=Page[AlertOut])
def list_alerts(
    host: str | None = None,
    severity: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    query = select(Alert)
    if host:
        query = query.where(Alert.host == host)
    if severity:
        query = query.where(Alert.severity == severity)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = db.scalars(query.order_by(Alert.id.desc()).limit(limit).offset(offset)).all()
    return {"items": items, "total": total, "limit": limit, "offset": offset}
