"""Collector local basado en psutil (Linux y Windows Server)."""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable
from types import ModuleType
from typing import Any

import psutil

from app.monitoring.models import DiskUsage, HostMetrics, ServiceStatus

logger = logging.getLogger(__name__)

# Sistemas de archivos que no tiene sentido monitorear
_IGNORED_FSTYPES = {"squashfs", "tmpfs", "devtmpfs", "overlay", "iso9660", ""}


class LocalCollector:
    """Toma CPU, memoria, disco y estado de servicios del host donde corre la API.

    `ps`, `platform` y `runner` son inyectables para poder testear sin tocar el sistema real.
    """

    def __init__(
        self,
        name: str,
        services: list[str] | None = None,
        disk_paths: list[str] | None = None,
        *,
        cpu_interval: float = 0.5,
        ps: ModuleType | Any = psutil,
        platform: str = sys.platform,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self.name = name
        self.services = services or []
        self.disk_paths = disk_paths
        self.cpu_interval = cpu_interval
        self.ps = ps
        self.platform = platform
        self.runner = runner

    def collect(self) -> HostMetrics:
        return HostMetrics(
            host=self.name,
            platform="windows" if self.is_windows else self.platform,
            cpu_percent=float(self.ps.cpu_percent(interval=self.cpu_interval)),
            memory_percent=float(self.ps.virtual_memory().percent),
            disks=self._disks(),
            services=self.check_services(),
        )

    @property
    def is_windows(self) -> bool:
        return self.platform.startswith("win")

    def _disk_mounts(self) -> list[str]:
        if self.disk_paths:
            return self.disk_paths
        return [
            p.mountpoint
            for p in self.ps.disk_partitions(all=False)
            if p.fstype.lower() not in _IGNORED_FSTYPES and "cdrom" not in p.opts
        ]

    def _disks(self) -> list[DiskUsage]:
        disks = []
        for mount in self._disk_mounts():
            try:
                usage = self.ps.disk_usage(mount)
            except (PermissionError, FileNotFoundError, OSError) as exc:
                logger.warning(
                    "No se pudo leer el disco", extra={"mount": mount, "error": str(exc)}
                )
                continue
            disks.append(
                DiskUsage(
                    mount=mount,
                    total_bytes=usage.total,
                    used_bytes=usage.used,
                    percent=float(usage.percent),
                )
            )
        return disks

    def check_services(self) -> list[ServiceStatus]:
        if not self.services:
            return []
        if self.is_windows:
            return self._windows_services()
        if self.platform.startswith("linux"):
            return [self._systemd_service(name) for name in self.services]
        return [ServiceStatus(name=s, running=False, status="unsupported") for s in self.services]

    def _windows_services(self) -> list[ServiceStatus]:
        # win_service_iter devuelve todos los servicios; se indexan por nombre (case-insensitive)
        installed = {svc.name().lower(): svc for svc in self.ps.win_service_iter()}
        result = []
        for name in self.services:
            svc = installed.get(name.lower())
            if svc is None:
                result.append(ServiceStatus(name=name, running=False, status="not_found"))
                continue
            status = svc.status()
            result.append(ServiceStatus(name=name, running=status == "running", status=status))
        return result

    def _systemd_service(self, name: str) -> ServiceStatus:
        try:
            proc = self.runner(
                ["systemctl", "is-active", name], capture_output=True, text=True, timeout=5
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return ServiceStatus(name=name, running=False, status=f"error: {exc}")
        status = (proc.stdout or "").strip() or "unknown"
        return ServiceStatus(name=name, running=status == "active", status=status)
