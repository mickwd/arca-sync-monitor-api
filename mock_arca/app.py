"""Mock de ARCA y del sistema receptor para desarrollo local y tests (Flask).

ARCA (simulado):
  POST /auth/login                 -> token + sign con vencimiento (simula WSAA)
  GET  /api/v1/comprobantes        -> facturas emitidas paginadas (requiere Bearer)

Receptor (simulado):
  POST /receptor/api/comprobantes  -> recibe un comprobante (X-API-Key + Idempotency-Key)
  GET  /receptor/api/comprobantes  -> lista lo recibido

Variables de entorno:
  MOCK_FAILURE_RATE   probabilidad (0-1) de responder 503, para ver los reintentos en acción
  MOCK_WEBHOOK_URL    si está definida, el receptor manda un webhook firmado al recibir
  WEBHOOK_SECRET      secreto HMAC compartido con la API
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import secrets
import threading
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
from flask import Flask, Response, abort, jsonify, request

TIPOS = [1, 6, 11]  # Factura A, B y C
CUITS_CLIENTES = ["30712345678", "20304050607", "27222333444", "30698765432"]


def generate_comprobantes(cuit: str, start: date, end: date, per_day: int = 3) -> list[dict]:
    """Genera un set determinístico de comprobantes (misma semilla = mismos datos)."""
    rng = random.Random(f"{cuit}-{start.isoformat()}")
    numeros = {(t, pv): 0 for t in TIPOS for pv in (1, 2)}
    items = []
    day = start
    while day <= end:
        for _ in range(per_day):
            tipo, pv = rng.choice(TIPOS), rng.choice([1, 2])
            numeros[(tipo, pv)] += 1
            items.append(
                {
                    "tipo_comprobante": tipo,
                    "punto_venta": pv,
                    "numero": numeros[(tipo, pv)],
                    "cuit_emisor": cuit,
                    "cuit_receptor": rng.choice(CUITS_CLIENTES),
                    "fecha_emision": day.isoformat(),
                    "importe_total": str(Decimal(rng.randint(1_000_00, 2_500_000_00)) / 100),
                    "moneda": "PES",
                    "cae": "".join(str(rng.randint(0, 9)) for _ in range(14)),
                    "cae_vencimiento": (day + timedelta(days=10)).isoformat(),
                }
            )
        day += timedelta(days=1)
    return items


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    message = timestamp.encode() + b"." + body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        CUIT=os.getenv("ARCA_CUIT", "20123456789"),
        DATA_START=date(2026, 1, 1),
        DATA_END=date.today(),
        PER_DAY=3,
        FAILURE_RATE=float(os.getenv("MOCK_FAILURE_RATE", "0")),
        TOKEN_TTL_SECONDS=12 * 3600,
        RECEPTOR_API_KEY=os.getenv("RECEPTOR_API_KEY", "receptor-dev-key"),
        WEBHOOK_URL=os.getenv("MOCK_WEBHOOK_URL"),
        WEBHOOK_SECRET=os.getenv("WEBHOOK_SECRET", "webhook-dev-secret"),
    )
    if config:
        app.config.update(config)

    tokens: dict[str, datetime] = {}
    received: dict[str, dict] = {}
    dataset = generate_comprobantes(
        app.config["CUIT"], app.config["DATA_START"], app.config["DATA_END"], app.config["PER_DAY"]
    )

    def maybe_fail() -> None:
        if random.random() < app.config["FAILURE_RATE"]:
            abort(Response(json.dumps({"error": "servicio no disponible"}), status=503))

    @app.get("/health")
    def health():
        return {"status": "ok"}

    # ---------------- ARCA ----------------

    @app.post("/auth/login")
    def login():
        body = request.get_json(silent=True) or {}
        if body.get("cuit") != app.config["CUIT"]:
            return jsonify(error="CUIT no autorizado"), 403
        token = secrets.token_urlsafe(24)
        expiration = datetime.now(UTC) + timedelta(seconds=app.config["TOKEN_TTL_SECONDS"])
        tokens[token] = expiration
        return {"token": token, "sign": secrets.token_hex(16), "expiration": expiration.isoformat()}

    @app.get("/api/v1/comprobantes")
    def list_comprobantes():
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if token not in tokens or tokens[token] < datetime.now(UTC):
            return jsonify(error="token inválido o vencido"), 401
        maybe_fail()

        if request.args.get("cuit") != app.config["CUIT"]:
            return jsonify(error="CUIT no coincide con el token"), 403
        desde = date.fromisoformat(request.args.get("desde", "2000-01-01"))
        hasta = date.fromisoformat(request.args.get("hasta", date.max.isoformat()))
        page = max(1, int(request.args.get("page", 1)))
        page_size = min(500, max(1, int(request.args.get("page_size", 100))))

        filtered = [c for c in dataset if desde <= date.fromisoformat(c["fecha_emision"]) <= hasta]
        total_pages = max(1, -(-len(filtered) // page_size))
        start = (page - 1) * page_size
        return {
            "items": filtered[start : start + page_size],
            "page": page,
            "page_size": page_size,
            "total": len(filtered),
            "total_pages": total_pages,
        }

    # ---------------- Receptor ----------------

    def send_webhook(clave: str, receptor_id: str) -> None:
        body = json.dumps(
            {
                "id": f"evt_{uuid.uuid4().hex}",
                "type": "comprobante.recibido",
                "created_at": datetime.now(UTC).isoformat(),
                "data": {"clave": clave, "receptor_id": receptor_id},
            }
        ).encode()
        ts = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Timestamp": ts,
            "X-Signature": sign_webhook(app.config["WEBHOOK_SECRET"], ts, body),
        }
        try:
            httpx.post(app.config["WEBHOOK_URL"], content=body, headers=headers, timeout=5)
        except httpx.HTTPError as exc:
            app.logger.warning("No se pudo enviar el webhook: %s", exc)

    @app.post("/receptor/api/comprobantes")
    def receive():
        if request.headers.get("X-API-Key") != app.config["RECEPTOR_API_KEY"]:
            return jsonify(error="API key inválida"), 401
        maybe_fail()
        key = request.headers.get("Idempotency-Key")
        payload = request.get_json(silent=True)
        if not key or not payload:
            return jsonify(error="Falta Idempotency-Key o body"), 400
        if key in received:
            return jsonify(received[key]), 200
        record = {"id": f"rcp_{uuid.uuid4().hex[:12]}", "status": "accepted", "clave": key}
        received[key] = {**record, "payload": payload}
        if app.config["WEBHOOK_URL"]:
            # Simula la confirmación asincrónica que mandaría el sistema real
            threading.Timer(1.0, send_webhook, args=(key, record["id"])).start()
        return jsonify(record), 201

    @app.get("/receptor/api/comprobantes")
    def list_received():
        return {"total": len(received), "items": list(received.values())}

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.getenv("PORT", "5001")))
