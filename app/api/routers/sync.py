from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_sync_service, require_scopes
from app.db.models import SyncRun
from app.schemas.common import Page, SyncRunOut
from app.services.sync_service import SyncAlreadyRunningError, SyncService

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post(
    "/run",
    response_model=SyncRunOut,
    dependencies=[Depends(require_scopes("sync:write"))],
    summary="Dispara una sincronización incremental ARCA -> receptor",
)
def trigger_sync(service: SyncService = Depends(get_sync_service)) -> SyncRun:
    try:
        return service.run(trigger="manual")
    except SyncAlreadyRunningError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get(
    "/runs",
    response_model=Page[SyncRunOut],
    dependencies=[Depends(require_scopes("comprobantes:read"))],
)
def list_runs(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    query = select(SyncRun)
    if status_filter:
        query = query.where(SyncRun.status == status_filter)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = db.scalars(query.order_by(SyncRun.id.desc()).limit(limit).offset(offset)).all()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get(
    "/runs/{run_id}",
    response_model=SyncRunOut,
    dependencies=[Depends(require_scopes("comprobantes:read"))],
)
def get_run(run_id: int, db: Session = Depends(get_db)) -> SyncRun:
    run = db.get(SyncRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Corrida no encontrada")
    return run
