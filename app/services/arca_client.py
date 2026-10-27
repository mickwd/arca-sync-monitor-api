"""Cliente HTTP (httpx) contra el servicio de comprobantes de ARCA.

La autenticación es pluggable mediante `ArcaAuthProvider`:

* `StaticTokenAuth`: token fijo (desarrollo / tests).
* `TokenEndpointAuth`: obtiene token+sign desde un endpoint HTTP y los cachea hasta su
  vencimiento. Es lo que expone el mock (`POST /auth/login`) y también sirve si delante de
  ARCA hay un gateway propio que ya resuelve WSAA.
* `WsaaAuthProvider`: punto de extensión para el flujo real de WSAA (ver docstring).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from app.services.retry import RetryPolicy, raise_for_retryable, retry_call

logger = logging.getLogger(__name__)


class ArcaComprobante(BaseModel):
    """Factura electrónica tal como la devuelve ARCA (formato del servicio de consulta)."""

    tipo_comprobante: int = Field(ge=1)
    punto_venta: int = Field(ge=1, le=99999)
    numero: int = Field(ge=1)
    cuit_emisor: str = Field(pattern=r"^\d{11}$")
    cuit_receptor: str = Field(pattern=r"^\d{11}$")
    fecha_emision: date
    importe_total: Decimal = Field(max_digits=15, decimal_places=2)
    moneda: str = "PES"
    cae: str = Field(pattern=r"^\d{14}$")
    cae_vencimiento: date | None = None

    @property
    def clave(self) -> tuple[str, int, int, int]:
        return (self.cuit_emisor, self.tipo_comprobante, self.punto_venta, self.numero)


@dataclass(frozen=True)
class ArcaCredentials:
    token: str
    sign: str = ""
    expires_at: datetime | None = None

    def is_valid(self, margin: timedelta = timedelta(minutes=5)) -> bool:
        return self.expires_at is None or datetime.now(UTC) + margin < self.expires_at


class ArcaAuthProvider(Protocol):
    def get_credentials(self, client: httpx.Client) -> ArcaCredentials: ...

    def invalidate(self) -> None: ...


class StaticTokenAuth:
    def __init__(self, token: str):
        self._creds = ArcaCredentials(token=token)

    def get_credentials(self, client: httpx.Client) -> ArcaCredentials:
        return self._creds

    def invalidate(self) -> None:  # un token estático no se puede renovar
        pass


class TokenEndpointAuth:
    """Pide token+sign a `POST {base_url}/auth/login` y los cachea (thread-safe)."""

    def __init__(self, cuit: str, service: str = "wsfe", path: str = "/auth/login"):
        self.cuit = cuit
        self.service = service
        self.path = path
        self._creds: ArcaCredentials | None = None
        self._lock = threading.Lock()

    def get_credentials(self, client: httpx.Client) -> ArcaCredentials:
        with self._lock:
            if self._creds is None or not self._creds.is_valid():
                resp = client.post(self.path, json={"cuit": self.cuit, "service": self.service})
                resp.raise_for_status()
                data = resp.json()
                self._creds = ArcaCredentials(
                    token=data["token"],
                    sign=data.get("sign", ""),
                    expires_at=datetime.fromisoformat(data["expiration"]),
                )
                logger.info("Token ARCA renovado", extra={"expires_at": data["expiration"]})
            return self._creds

    def invalidate(self) -> None:
        with self._lock:
            self._creds = None


class WsaaAuthProvider:
    """Punto de extensión para WSAA real (no implementado a propósito).

    El flujo productivo es:
      1. Armar un TRA (LoginTicketRequest XML) con `service`, `generationTime` y `expirationTime`.
      2. Firmarlo como CMS/PKCS#7 con el certificado X.509 y la clave privada del CUIT
         (por ejemplo con `cryptography.hazmat.primitives.serialization.pkcs7`).
      3. Llamar al método SOAP `loginCms` de WSAA con el CMS en base64.
      4. Parsear el `LoginTicketResponse` y extraer `token`, `sign` y `expirationTime`.
    El ticket dura ~12 h y WSAA rechaza pedir otro mientras el anterior siga vigente,
    por eso conviene persistirlo (archivo o tabla) y compartirlo entre procesos.
    """

    def __init__(self, cert_path: str, key_path: str, service: str = "wsfe"):
        self.cert_path = cert_path
        self.key_path = key_path
        self.service = service

    def get_credentials(self, client: httpx.Client) -> ArcaCredentials:
        raise NotImplementedError("Implementar loginCms de WSAA (ver docstring)")

    def invalidate(self) -> None:
        pass


class ArcaClient:
    """Consulta comprobantes emitidos con paginación, reintentos y renovación de token."""

    def __init__(
        self,
        base_url: str,
        cuit: str,
        auth: ArcaAuthProvider,
        *,
        retry_policy: RetryPolicy | None = None,
        timeout: float = 15.0,
        page_size: int = 100,
        transport: httpx.BaseTransport | None = None,
        sleep=None,
    ):
        self.cuit = cuit
        self.auth = auth
        self.retry_policy = retry_policy or RetryPolicy()
        self.page_size = page_size
        self._sleep = sleep
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ArcaClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, path: str, params: dict) -> dict:
        def do_request() -> dict:
            creds = self.auth.get_credentials(self._client)
            headers = {
                "Authorization": f"Bearer {creds.token}",
                "X-Arca-Sign": creds.sign,
                "X-Arca-Cuit": self.cuit,
            }
            resp = self._client.get(path, params=params, headers=headers)
            if resp.status_code == 401:
                # Token vencido o revocado: se invalida y se reintenta una sola vez
                self.auth.invalidate()
                creds = self.auth.get_credentials(self._client)
                headers["Authorization"] = f"Bearer {creds.token}"
                headers["X-Arca-Sign"] = creds.sign
                resp = self._client.get(path, params=params, headers=headers)
            raise_for_retryable(resp)
            return resp.json()

        kwargs = {"sleep": self._sleep} if self._sleep else {}
        return retry_call(do_request, self.retry_policy, operation=f"arca GET {path}", **kwargs)

    def iter_comprobantes(
        self, desde: date, hasta: date | None = None
    ) -> Iterator[ArcaComprobante]:
        """Recorre todas las páginas de comprobantes emitidos entre `desde` y `hasta`."""
        page = 1
        while True:
            params: dict = {
                "cuit": self.cuit,
                "desde": desde.isoformat(),
                "page": page,
                "page_size": self.page_size,
            }
            if hasta:
                params["hasta"] = hasta.isoformat()
            data = self._get("/api/v1/comprobantes", params)
            for item in data.get("items", []):
                yield ArcaComprobante.model_validate(item)
            if page >= data.get("total_pages", 1):
                break
            page += 1

    def fetch_comprobantes(self, desde: date, hasta: date | None = None) -> list[ArcaComprobante]:
        return list(self.iter_comprobantes(desde, hasta))
