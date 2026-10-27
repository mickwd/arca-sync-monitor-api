"""Collector remoto para hosts Linux vía SSH (paramiko).

Se ejecuta un único comando que junta /proc/stat (dos muestras), /proc/meminfo, df y el
estado de los servicios systemd; la salida se parsea con funciones puras fáciles de testear.
No requiere instalar ningún agente en el servidor remoto.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable
from pathlib import Path

import paramiko

from app.monitoring.models import DiskUsage, HostMetrics, ServiceStatus

logger = logging.getLogger(__name__)

SECTION_MEM = "---MEM"
SECTION_DF = "---DF"
SECTION_SVC = "---SVC"


def build_remote_script(disks: list[str], services: list[str]) -> str:
    disk_args = " ".join(shlex.quote(d) for d in disks) or "/"
    parts = [
        "head -1 /proc/stat",
        "sleep 1",
        "head -1 /proc/stat",
        f"echo '{SECTION_MEM}'",
        "cat /proc/meminfo",
        f"echo '{SECTION_DF}'",
        f"df -P -B1 {disk_args}",
        f"echo '{SECTION_SVC}'",
    ]
    for svc in services:
        q = shlex.quote(svc)
        parts.append(f'echo "{svc} $(systemctl is-active {q} 2>/dev/null || true)"')
    return "; ".join(parts)


def parse_cpu(first: str, second: str) -> float:
    """Calcula el % de CPU usado entre dos líneas `cpu ...` de /proc/stat."""

    def totals(line: str) -> tuple[int, int]:
        values = [int(v) for v in line.split()[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
        return idle, sum(values)

    idle1, total1 = totals(first)
    idle2, total2 = totals(second)
    delta_total = total2 - total1
    if delta_total <= 0:
        return 0.0
    return round(100.0 * (1 - (idle2 - idle1) / delta_total), 2)


def parse_meminfo(text: str) -> float:
    info: dict[str, int] = {}
    for line in text.splitlines():
        if ":" in line:
            key, rest = line.split(":", 1)
            info[key.strip()] = int(rest.split()[0])
    total = info["MemTotal"]
    available = info.get("MemAvailable", info.get("MemFree", 0))
    return round(100.0 * (total - available) / total, 2)


def parse_df(text: str) -> list[DiskUsage]:
    disks = []
    for line in text.splitlines()[1:]:  # la primera línea es el encabezado
        cols = line.split()
        if len(cols) < 6:
            continue
        total, used, available = int(cols[1]), int(cols[2]), int(cols[3])
        # Igual que df: usado / (usado + disponible), sin contar lo reservado para root
        denom = used + available
        percent = round(100.0 * used / denom, 2) if denom else 0.0
        disks.append(DiskUsage(mount=cols[5], total_bytes=total, used_bytes=used, percent=percent))
    return disks


def parse_services(text: str) -> list[ServiceStatus]:
    result = []
    for line in text.splitlines():
        if not line.strip():
            continue
        name, _, status = line.strip().partition(" ")
        status = status.strip() or "unknown"
        result.append(ServiceStatus(name=name, running=status == "active", status=status))
    return result


def parse_remote_output(host: str, output: str) -> HostMetrics:
    head, _, rest = output.partition(SECTION_MEM)
    mem_text, _, rest = rest.partition(SECTION_DF)
    df_text, _, svc_text = rest.partition(SECTION_SVC)
    cpu_lines = [line for line in head.splitlines() if line.startswith("cpu ")]
    if len(cpu_lines) < 2:
        raise ValueError("Salida inesperada: faltan muestras de /proc/stat")
    return HostMetrics(
        host=host,
        platform="linux",
        cpu_percent=parse_cpu(cpu_lines[0], cpu_lines[1]),
        memory_percent=parse_meminfo(mem_text),
        disks=parse_df(df_text.strip()),
        services=parse_services(svc_text),
    )


class SshCollector:
    def __init__(
        self,
        name: str,
        address: str,
        username: str,
        *,
        port: int = 22,
        key_file: str | None = None,
        services: list[str] | None = None,
        disks: list[str] | None = None,
        timeout: float = 10.0,
        strict_host_keys: bool = True,
        client_factory: Callable[[], paramiko.SSHClient] = paramiko.SSHClient,
    ):
        self.name = name
        self.address = address
        self.username = username
        self.port = port
        self.key_file = str(Path(key_file).expanduser()) if key_file else None
        self.services = services or []
        self.disks = disks or ["/"]
        self.timeout = timeout
        self.strict_host_keys = strict_host_keys
        self.client_factory = client_factory

    def collect(self) -> HostMetrics:
        client = self.client_factory()
        try:
            client.load_system_host_keys()
            # En producción se exige que el host esté en known_hosts (evita MITM)
            policy = paramiko.RejectPolicy() if self.strict_host_keys else paramiko.WarningPolicy()
            client.set_missing_host_key_policy(policy)
            client.connect(
                self.address,
                port=self.port,
                username=self.username,
                key_filename=self.key_file,
                timeout=self.timeout,
                banner_timeout=self.timeout,
                auth_timeout=self.timeout,
            )
            script = build_remote_script(self.disks, self.services)
            _, stdout, _ = client.exec_command(script, timeout=self.timeout + 5)
            output = stdout.read().decode("utf-8", errors="replace")
            return parse_remote_output(self.name, output)
        except (paramiko.SSHException, OSError, ValueError, KeyError) as exc:
            logger.error(
                "Fallo la recolección remota", extra={"host": self.name, "error": str(exc)}
            )
            return HostMetrics(host=self.name, platform="linux", reachable=False, error=str(exc))
        finally:
            client.close()
