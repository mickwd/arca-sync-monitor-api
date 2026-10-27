from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from app.monitoring.alerts import AlertEngine, AlertEvent, Threshold, Thresholds
from app.monitoring.models import DiskUsage, HostMetrics, ServiceStatus
from app.monitoring.notifiers import CompositeNotifier, WebhookNotifier


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def metrics(cpu=10.0, mem=10.0, disk=10.0, services=None, reachable=True) -> HostMetrics:
    return HostMetrics(
        host="srv-01",
        reachable=reachable,
        error=None if reachable else "timeout",
        cpu_percent=cpu,
        memory_percent=mem,
        disks=[DiskUsage(mount="/", total_bytes=100, used_bytes=int(disk), percent=disk)],
        services=services or [],
    )


def make_engine(clock=None, cooldown_min=15) -> AlertEngine:
    return AlertEngine(Thresholds(), timedelta(minutes=cooldown_min), clock=clock or FakeClock())


def test_no_alerts_when_everything_is_ok():
    assert make_engine().evaluate(metrics()) == []


def test_warning_and_critical_thresholds():
    events = make_engine().evaluate(metrics(cpu=85, mem=97, disk=91))
    by_metric = {e.metric: e for e in events}
    assert by_metric["cpu"].severity == "warning"
    assert by_metric["cpu"].threshold == 80
    assert by_metric["memory"].severity == "critical"
    assert by_metric["disk:/"].severity == "critical"
    assert "91.0%" in by_metric["disk:/"].message


def test_value_equal_to_threshold_triggers():
    events = make_engine().evaluate(metrics(cpu=80))
    assert [e.severity for e in events] == ["warning"]


def test_cooldown_suppresses_repeated_alerts():
    clock = FakeClock()
    engine = make_engine(clock)
    assert len(engine.evaluate(metrics(cpu=90))) == 1
    clock.advance(minutes=5)
    assert engine.evaluate(metrics(cpu=91)) == []
    clock.advance(minutes=10)  # 15 min desde la primera -> se vuelve a notificar
    assert [e.metric for e in engine.evaluate(metrics(cpu=92))] == ["cpu"]


def test_escalation_bypasses_cooldown():
    clock = FakeClock()
    engine = make_engine(clock)
    engine.evaluate(metrics(cpu=85))
    clock.advance(minutes=1)
    events = engine.evaluate(metrics(cpu=99))
    assert [(e.metric, e.severity) for e in events] == [("cpu", "critical")]
    # Bajar de critical a warning dentro del cooldown no genera ruido
    clock.advance(minutes=1)
    assert engine.evaluate(metrics(cpu=85)) == []


def test_recovery_emits_resolved_and_resets_state():
    clock = FakeClock()
    engine = make_engine(clock)
    engine.evaluate(metrics(disk=95))
    events = engine.evaluate(metrics(disk=40))
    assert [(e.metric, e.severity) for e in events] == [("disk:/", "resolved")]
    # Una nueva superación alerta inmediatamente aunque no haya pasado el cooldown
    clock.advance(minutes=1)
    assert [e.severity for e in engine.evaluate(metrics(disk=95))] == ["critical"]


def test_service_down_is_critical():
    svc = [
        ServiceStatus(name="nginx", running=True, status="active"),
        ServiceStatus(name="postgresql", running=False, status="failed"),
    ]
    events = make_engine().evaluate(metrics(services=svc))
    assert [(e.metric, e.severity) for e in events] == [("service:postgresql", "critical")]


def test_unreachable_host_only_reports_unreachable():
    events = make_engine().evaluate(metrics(cpu=None, reachable=False))
    assert [(e.metric, e.severity) for e in events] == [("host_unreachable", "critical")]


def test_per_host_threshold_overrides():
    engine = make_engine()
    events = engine.evaluate(metrics(disk=72), overrides={"disk": Threshold(70, 85)})
    assert [(e.metric, e.severity) for e in events] == [("disk:/", "warning")]


def test_webhook_notifier_posts_payload_and_composite_isolates_failures():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200)

    class Broken:
        def notify(self, event):
            raise RuntimeError("smtp caído")

    webhook = WebhookNotifier("http://hooks.test/alert", transport=httpx.MockTransport(handler))
    notifier = CompositeNotifier([Broken(), webhook])
    event = AlertEvent("srv-01", "cpu", "critical", "CPU en 99%", value=99, threshold=95)

    assert notifier.notify(event) is False  # un canal falló
    assert len(sent) == 1  # pero el webhook igual salió
    assert b"CRITICAL" in sent[0].content
