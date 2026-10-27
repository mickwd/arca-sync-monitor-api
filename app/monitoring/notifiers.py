"""Canales de notificación de alertas: webhook (Slack/Teams/genérico), email SMTP y log."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

import httpx

from app.core.config import Settings
from app.monitoring.alerts import AlertEvent

logger = logging.getLogger(__name__)

_EMOJI = {"warning": "⚠️", "critical": "🔴", "resolved": "✅"}


def format_alert(event: AlertEvent) -> str:
    return (
        f"{_EMOJI.get(event.severity, '')} [{event.severity.upper()}] {event.host}: {event.message}"
    )


class Notifier(Protocol):
    def notify(self, event: AlertEvent) -> None: ...


class LogNotifier:
    def notify(self, event: AlertEvent) -> None:
        level = logging.INFO if event.severity == "resolved" else logging.WARNING
        logger.log(level, format_alert(event), extra={"host": event.host, "metric": event.metric})


class WebhookNotifier:
    """POST JSON con un campo `text` (compatible con Slack incoming webhooks) + datos crudos."""

    def __init__(self, url: str, transport: httpx.BaseTransport | None = None, timeout: float = 5):
        self.url = url
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def notify(self, event: AlertEvent) -> None:
        payload = {
            "text": format_alert(event),
            "host": event.host,
            "metric": event.metric,
            "severity": event.severity,
            "value": event.value,
            "threshold": event.threshold,
        }
        self._client.post(self.url, json=payload).raise_for_status()


class EmailNotifier:
    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        recipients: list[str],
        user: str | None = None,
        password: str | None = None,
        use_tls: bool = True,
    ):
        self.host = host
        self.port = port
        self.sender = sender
        self.recipients = recipients
        self.user = user
        self.password = password
        self.use_tls = use_tls

    def notify(self, event: AlertEvent) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[{event.severity.upper()}] {event.host} - {event.metric}"
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg.set_content(format_alert(event))
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.user and self.password:
                smtp.login(self.user, self.password)
            smtp.send_message(msg)


class CompositeNotifier:
    """Envía por todos los canales; un canal caído no impide que funcionen los demás."""

    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers

    def notify(self, event: AlertEvent) -> bool:
        ok = True
        for notifier in self.notifiers:
            try:
                notifier.notify(event)
            except Exception as exc:
                ok = False
                logger.error(
                    "Fallo el envío de la alerta",
                    extra={"notifier": type(notifier).__name__, "error": str(exc)},
                )
        return ok


def build_notifier(settings: Settings) -> CompositeNotifier:
    notifiers: list[Notifier] = [LogNotifier()]
    if settings.alert_webhook_url:
        notifiers.append(WebhookNotifier(settings.alert_webhook_url))
    if settings.smtp_host and settings.alert_email_to:
        notifiers.append(
            EmailNotifier(
                host=settings.smtp_host,
                port=settings.smtp_port,
                sender=settings.alert_email_from,
                recipients=[r.strip() for r in settings.alert_email_to.split(",")],
                user=settings.smtp_user,
                password=settings.smtp_password,
                use_tls=settings.smtp_use_tls,
            )
        )
    return CompositeNotifier(notifiers)
