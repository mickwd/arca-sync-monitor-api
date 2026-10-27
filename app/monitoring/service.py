"""Orquesta la recolección de métricas, evaluación de alertas, persistencia y notificación."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.models import Alert
from app.monitoring.alerts import AlertEngine, Threshold, Thresholds
from app.monitoring.hosts import HostConfig, build_collector, load_hosts
from app.monitoring.models import HostMetrics
from app.monitoring.notifiers import CompositeNotifier, build_notifier

logger = logging.getLogger(__name__)


class Collector(Protocol):
    name: str

    def collect(self) -> HostMetrics: ...


class MonitoringService:
    def __init__(
        self,
        hosts: list[HostConfig],
        engine: AlertEngine,
        notifier: CompositeNotifier,
        session_factory: sessionmaker[Session],
        collector_factory: Callable[[HostConfig], Collector] = build_collector,
    ):
        self.hosts = hosts
        self.engine = engine
        self.notifier = notifier
        self.session_factory = session_factory
        self.collectors = {h.name: collector_factory(h) for h in hosts}
        self._overrides = {
            h.name: {k: Threshold(v.warning, v.critical) for k, v in h.thresholds.items()}
            for h in hosts
        }
        self._latest: dict[str, HostMetrics] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(
        cls, settings: Settings, session_factory: sessionmaker[Session]
    ) -> MonitoringService:
        thresholds = Thresholds(
            cpu=Threshold(settings.cpu_warning, settings.cpu_critical),
            memory=Threshold(settings.memory_warning, settings.memory_critical),
            disk=Threshold(settings.disk_warning, settings.disk_critical),
        )
        return cls(
            hosts=load_hosts(settings.hosts_config_path),
            engine=AlertEngine(thresholds, timedelta(minutes=settings.alert_cooldown_minutes)),
            notifier=build_notifier(settings),
            session_factory=session_factory,
        )

    def latest(self) -> list[HostMetrics]:
        with self._lock:
            return list(self._latest.values())

    def collect_host(self, name: str) -> HostMetrics:
        return self.collectors[name].collect()

    def run_checks(self) -> list[HostMetrics]:
        """Recolecta todos los hosts en paralelo (SSH es lento) y procesa las alertas."""
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(self.collectors)))) as pool:
            results = list(pool.map(lambda c: c.collect(), self.collectors.values()))

        with self._lock:
            for metrics in results:
                self._latest[metrics.host] = metrics
            events = [
                e for m in results for e in self.engine.evaluate(m, self._overrides.get(m.host))
            ]

        if events:
            with self.session_factory() as db:
                for event in events:
                    notified = self.notifier.notify(event)
                    db.add(
                        Alert(
                            host=event.host,
                            metric=event.metric,
                            severity=event.severity,
                            value=event.value,
                            threshold=event.threshold,
                            message=event.message,
                            notified=notified,
                        )
                    )
                db.commit()
        logger.info(
            "Chequeo de monitoreo completo", extra={"hosts": len(results), "alerts": len(events)}
        )
        return results
