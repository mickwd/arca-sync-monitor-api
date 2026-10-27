from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class DiskUsage(BaseModel):
    mount: str
    total_bytes: int
    used_bytes: int
    percent: float


class ServiceStatus(BaseModel):
    name: str
    running: bool
    status: str  # running / stopped / inactive / failed / not_found / unsupported


class HostMetrics(BaseModel):
    host: str
    platform: str = "unknown"
    reachable: bool = True
    error: str | None = None
    cpu_percent: float | None = None
    memory_percent: float | None = None
    disks: list[DiskUsage] = Field(default_factory=list)
    services: list[ServiceStatus] = Field(default_factory=list)
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
