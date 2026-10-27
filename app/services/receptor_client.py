"""Cliente del sistema receptor al que se empujan los comprobantes sincronizados."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.db.models import Comprobante
from app.services.retry import RetryPolicy, raise_for_retryable, retry_call


@dataclass(frozen=True)
class ReceptorAck:
    ack_id: str
    status: str


def comprobante_to_payload(c: Comprobante) -> dict:
    return {
        "clave": c.clave,
        "tipo_comprobante": c.tipo_comprobante,
        "punto_venta": c.punto_venta,
        "numero": c.numero,
        "cuit_emisor": c.cuit_emisor,
        "cuit_receptor": c.cuit_receptor,
        "fecha_emision": c.fecha_emision.isoformat(),
        "importe_total": str(c.importe_total),
        "moneda": c.moneda,
        "cae": c.cae,
        "cae_vencimiento": c.cae_vencimiento.isoformat() if c.cae_vencimiento else None,
    }


class ReceptorClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        retry_policy: RetryPolicy | None = None,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        sleep=None,
    ):
        self.retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"X-API-Key": api_key},
        )

    def close(self) -> None:
        self._client.close()

    def push(self, comprobante: Comprobante) -> ReceptorAck:
        """Envía un comprobante. El Idempotency-Key evita duplicados si hay reintentos."""
        payload = comprobante_to_payload(comprobante)

        def do_request() -> ReceptorAck:
            resp = self._client.post(
                "/api/comprobantes",
                json=payload,
                headers={"Idempotency-Key": comprobante.clave},
            )
            raise_for_retryable(resp)
            data = resp.json()
            return ReceptorAck(ack_id=str(data.get("id", "")), status=data.get("status", "ok"))

        kwargs = {"sleep": self._sleep} if self._sleep else {}
        return retry_call(
            do_request, self.retry_policy, operation="receptor POST comprobante", **kwargs
        )
