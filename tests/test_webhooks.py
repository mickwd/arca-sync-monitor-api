from __future__ import annotations

import json
import time

from sqlalchemy import func, select

from app.core.security import compute_webhook_signature
from app.db.models import Comprobante, EstadoEnvio, WebhookEvent
from tests.conftest import make_comprobante

CLAVE = "20123456789-001-00002-00000001"


def _event(event_id: str = "evt_1", event_type: str = "comprobante.recibido", **data) -> bytes:
    payload = {"id": event_id, "type": event_type, "data": {"clave": CLAVE, **data}}
    return json.dumps(payload).encode()


def _post(client, body: bytes, secret: str = "whsec_test", ts: int | None = None, sig=None):
    timestamp = str(ts if ts is not None else int(time.time()))
    signature = sig or compute_webhook_signature(secret, timestamp, body)
    return client.post(
        "/webhooks/receptor",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": timestamp,
            "X-Signature": signature,
        },
    )


def test_valid_signature_confirms_comprobante(client, db):
    db.add(make_comprobante(numero=1, estado_envio=EstadoEnvio.SENT))
    db.commit()

    resp = _post(client, _event(receptor_id="rcp_99"))
    assert resp.status_code == 200
    assert resp.json() == {"event_id": "evt_1", "status": "processed", "duplicate": False}

    db.expire_all()
    comp = db.scalar(select(Comprobante))
    assert comp.estado_envio == EstadoEnvio.CONFIRMED
    assert comp.receptor_ack_id == "rcp_99"


def test_rejected_event_stores_reason(client, db):
    db.add(make_comprobante(numero=1))
    db.commit()
    resp = _post(client, _event("evt_r", "comprobante.rechazado", motivo="CUIT receptor inválido"))
    assert resp.status_code == 200
    db.expire_all()
    comp = db.scalar(select(Comprobante))
    assert comp.estado_envio == EstadoEnvio.REJECTED
    assert comp.ultimo_error == "CUIT receptor inválido"


def test_invalid_signature_is_rejected(client, db):
    resp = _post(client, _event(), secret="otro-secreto")
    assert resp.status_code == 401
    assert db.scalar(select(func.count(WebhookEvent.id))) == 0


def test_body_tampering_invalidates_signature(client):
    body = _event()
    ts = str(int(time.time()))
    sig = compute_webhook_signature("whsec_test", ts, body)
    tampered = body.replace(b"recibido", b"rechazado")
    assert _post(client, tampered, ts=int(ts), sig=sig).status_code == 401


def test_old_timestamp_is_rejected(client):
    resp = _post(client, _event(), ts=int(time.time()) - 3600)
    assert resp.status_code == 401


def test_missing_signature_headers(client):
    resp = client.post("/webhooks/receptor", content=_event())
    assert resp.status_code == 422


def test_duplicate_event_is_idempotent(client, db):
    db.add(make_comprobante(numero=1))
    db.commit()

    first = _post(client, _event("evt_dup"))
    second = _post(client, _event("evt_dup"))
    assert first.status_code == second.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert db.scalar(select(func.count(WebhookEvent.id))) == 1


def test_unknown_event_type_is_ignored(client):
    resp = _post(client, _event("evt_x", "comprobante.otro"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_event_for_unknown_comprobante_is_recorded_as_error(client, db):
    resp = _post(client, _event("evt_missing"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"
    event = db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == "evt_missing"))
    assert "inexistente" in event.error


def test_invalid_payload_with_valid_signature(client):
    resp = _post(client, b'{"id": "evt_bad"}')
    assert resp.status_code == 422
