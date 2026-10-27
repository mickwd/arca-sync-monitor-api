"""Motor de alertas por umbrales con cooldown y aviso de recuperación."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.monitoring.models import HostMetrics

SEVERITY_RANK = {"warning": 1, "critical": 2}


@dataclass(frozen=True)
class Threshold:
    warning: float
    critical: float

    def severity_for(self, value: float) -> str | None:
        if value >= self.critical:
            return "critical"
        if value >= self.warning:
            return "warning"
        return None


@dataclass(frozen=True)
class Thresholds:
    cpu: Threshold = field(default_factory=lambda: Threshold(80, 95))
    memory: Threshold = field(default_factory=lambda: Threshold(80, 95))
    disk: Threshold = field(default_factory=lambda: Threshold(80, 90))

    def with_overrides(self, overrides: dict[str, Threshold]) -> Thresholds:
        return Thresholds(
            cpu=overrides.get("cpu", self.cpu),
            memory=overrides.get("memory", self.memory),
            disk=overrides.get("disk", self.disk),
        )


@dataclass(frozen=True)
class AlertEvent:
    host: str
    metric: str  # cpu, memory, disk:/var, service:nginx, host_unreachable
    severity: str  # warning | critical | resolved
    message: str
    value: float | None = None
    threshold: float | None = None


@dataclass
class _ActiveAlert:
    severity: str
    last_sent: datetime


class AlertEngine:
    """Evalúa métricas contra umbrales.

    Reglas de deduplicación:
      * Una alerta activa no se repite hasta que pase el cooldown.
      * Si la severidad escala (warning -> critical) se notifica de inmediato.
      * Cuando la métrica vuelve a la normalidad se emite un evento `resolved`.
    """

    def __init__(
        self,
        thresholds: Thresholds | None = None,
        cooldown: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.thresholds = thresholds or Thresholds()
        self.cooldown = cooldown
        self.clock = clock
        self._active: dict[tuple[str, str], _ActiveAlert] = {}

    def evaluate(
        self, metrics: HostMetrics, overrides: dict[str, Threshold] | None = None
    ) -> list[AlertEvent]:
        candidates = self._check(metrics, self.thresholds.with_overrides(overrides or {}))
        return self._apply_cooldown(metrics.host, candidates)

    def _check(self, m: HostMetrics, th: Thresholds) -> dict[str, AlertEvent | None]:
        """Devuelve {métrica: evento o None si está OK} para todas las métricas evaluadas."""
        result: dict[str, AlertEvent | None] = {}
        result["host_unreachable"] = (
            None
            if m.reachable
            else AlertEvent(m.host, "host_unreachable", "critical", f"Host inaccesible: {m.error}")
        )
        if not m.reachable:
            return result

        if m.cpu_percent is not None:
            result["cpu"] = self._threshold_event(m.host, "cpu", "CPU", m.cpu_percent, th.cpu)
        if m.memory_percent is not None:
            result["memory"] = self._threshold_event(
                m.host, "memory", "Memoria", m.memory_percent, th.memory
            )
        for disk in m.disks:
            result[f"disk:{disk.mount}"] = self._threshold_event(
                m.host, f"disk:{disk.mount}", f"Disco {disk.mount}", disk.percent, th.disk
            )
        for svc in m.services:
            result[f"service:{svc.name}"] = (
                None
                if svc.running
                else AlertEvent(
                    m.host,
                    f"service:{svc.name}",
                    "critical",
                    f"Servicio {svc.name} no está corriendo (estado: {svc.status})",
                )
            )
        return result

    @staticmethod
    def _threshold_event(
        host: str, metric: str, label: str, value: float, threshold: Threshold
    ) -> AlertEvent | None:
        severity = threshold.severity_for(value)
        if severity is None:
            return None
        limit = threshold.critical if severity == "critical" else threshold.warning
        return AlertEvent(
            host,
            metric,
            severity,
            f"{label} en {value:.1f}% (umbral {severity}: {limit:.0f}%)",
            value=value,
            threshold=limit,
        )

    def _apply_cooldown(
        self, host: str, candidates: dict[str, AlertEvent | None]
    ) -> list[AlertEvent]:
        now = self.clock()
        out: list[AlertEvent] = []
        for metric, event in candidates.items():
            key = (host, metric)
            active = self._active.get(key)
            if event is None:
                if active is not None:
                    del self._active[key]
                    out.append(
                        AlertEvent(host, metric, "resolved", f"{metric} volvió a la normalidad")
                    )
                continue
            escalated = active is not None and (
                SEVERITY_RANK[event.severity] > SEVERITY_RANK[active.severity]
            )
            if active is None or escalated or now - active.last_sent >= self.cooldown:
                self._active[key] = _ActiveAlert(event.severity, now)
                out.append(event)
        return out
