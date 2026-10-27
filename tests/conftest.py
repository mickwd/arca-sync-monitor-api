from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Comprobante
from app.main import create_app


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite://",  # SQLite en memoria (StaticPool)
        jwt_secret="test-secret-key-0123456789abcdef",
        password_hash_iterations=1_000,  # rápido para tests
        admin_username="admin",
        admin_password="s3cret",
        oauth_client_id="test-client",
        oauth_client_secret="test-client-secret",
        webhook_secret="whsec_test",
        hosts_config_path=tmp_path / "hosts.yaml",  # no existe -> host local
        scheduler_enabled=False,
        log_json=False,
        log_level="INFO",  # INFO para detectar errores en los extra= de los logs
        retry_base_delay_seconds=0.0,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:  # el context manager ejecuta el lifespan (crea tablas y admin)
        yield c


@pytest.fixture
def db(client: TestClient, app: FastAPI) -> Iterator[Session]:
    with app.state.session_factory() as session:
        yield session


def get_token(client: TestClient, scope: str | None = None) -> str:
    data = {"grant_type": "password", "username": "admin", "password": "s3cret"}
    if scope:
        data["scope"] = scope
    resp = client.post("/auth/token", data=data)
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.fixture
def auth_headers(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {get_token(client)}"}


def make_comprobante(numero: int = 1, **overrides) -> Comprobante:
    data = {
        "cuit_emisor": "20123456789",
        "cuit_receptor": "30712345678",
        "tipo_comprobante": 1,
        "punto_venta": 2,
        "numero": numero,
        "fecha_emision": date(2026, 3, 10),
        "importe_total": Decimal("12100.50"),
        "moneda": "PES",
        "cae": "74123456789012",
        "cae_vencimiento": date(2026, 3, 20),
    }
    data.update(overrides)
    return Comprobante(**data)
