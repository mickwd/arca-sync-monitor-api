from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.api.deps import get_sync_service
from app.db.models import Comprobante, EstadoEnvio, SyncRun
from app.services.arca_client import ArcaClient, StaticTokenAuth, TokenEndpointAuth
from app.services.receptor_client import ReceptorClient
from app.services.retry import RetryExhaustedError, RetryPolicy, retry_call
from app.services.sync_service import SyncService
from mock_arca.app import create_app as create_mock_app

CUIT = "20123456789"
FAST = RetryPolicy(max_attempts=4, base_delay=0.01, max_delay=0.05)


def comprobante_json(numero: int, fecha: str = "2026-03-10", **extra) -> dict:
    return {
        "tipo_comprobante": 1,
        "punto_venta": 2,
        "numero": numero,
        "cuit_emisor": CUIT,
        "cuit_receptor": "30712345678",
        "fecha_emision": fecha,
        "importe_total": "1500.00",
        "moneda": "PES",
        "cae": "74123456789012",
        "cae_vencimiento": "2026-03-20",
        **extra,
    }


class FakeArca:
    """Handler para httpx.MockTransport que simula ARCA con fallas programables."""

    def __init__(self, pages: list[list[dict]], failures: int = 0, status: int = 503):
        self.pages = pages
        self.failures = failures
        self.status = status
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.failures > 0:
            self.failures -= 1
            return httpx.Response(self.status, json={"error": "temporal"})
        page = int(request.url.params.get("page", 1))
        return httpx.Response(
            200, json={"items": self.pages[page - 1], "page": page, "total_pages": len(self.pages)}
        )


class FakeReceptor:
    def __init__(self, failures: int = 0, status: int = 503):
        self.failures = failures
        self.status = status
        self.received: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.failures > 0:
            self.failures -= 1
            return httpx.Response(self.status)
        self.received.append(request)
        return httpx.Response(201, json={"id": f"rcp_{len(self.received)}", "status": "accepted"})


def no_sleep(_: float) -> None:
    pass


def build_service(db, arca_handler, receptor_handler, policy: RetryPolicy = FAST) -> SyncService:
    arca = ArcaClient(
        "http://arca.test",
        CUIT,
        StaticTokenAuth("t"),
        retry_policy=policy,
        transport=httpx.MockTransport(arca_handler),
        sleep=no_sleep,
    )
    receptor = ReceptorClient(
        "http://receptor.test",
        "key",
        retry_policy=policy,
        transport=httpx.MockTransport(receptor_handler),
        sleep=no_sleep,
    )
    return SyncService(db, arca, receptor, initial_date=date(2026, 1, 1))


# ---------------------------------------------------------------- retry helper


def test_backoff_delay_is_exponential_and_capped():
    policy = RetryPolicy(max_attempts=10, base_delay=1, max_delay=8)
    # Con rng=1 se obtiene el tope de cada intento
    assert [policy.compute_delay(n, rng=lambda: 1.0) for n in range(1, 6)] == [1, 2, 4, 8, 8]
    assert policy.compute_delay(3, rng=lambda: 0.5) == 2


def test_retry_call_does_not_retry_non_retryable_errors():
    calls = []

    def boom():
        calls.append(1)
        raise ValueError("bug")

    with pytest.raises(ValueError):
        retry_call(boom, FAST, sleep=no_sleep)
    assert len(calls) == 1


# ---------------------------------------------------------------- sync service


def test_sync_stores_and_pushes_comprobantes(db):
    arca = FakeArca([[comprobante_json(1), comprobante_json(2)], [comprobante_json(3)]])
    receptor = FakeReceptor()
    run = build_service(db, arca, receptor).run()

    assert run.status == "success"
    assert (run.fetched, run.created, run.duplicates, run.pushed) == (3, 3, 0, 3)
    assert len(arca.calls) == 2  # dos páginas
    assert arca.calls[0].headers["Authorization"] == "Bearer t"
    assert receptor.received[0].headers["Idempotency-Key"] == "20123456789-001-00002-00000001"
    estados = db.scalars(select(Comprobante.estado_envio)).all()
    assert estados == [EstadoEnvio.SENT] * 3


def test_sync_retries_on_5xx_and_then_succeeds(db):
    arca = FakeArca([[comprobante_json(1)]], failures=2, status=503)
    sleeps: list[float] = []
    service = build_service(db, arca, FakeReceptor())
    service.arca._sleep = sleeps.append

    run = service.run()
    assert run.status == "success"
    assert len(arca.calls) == 3
    assert len(sleeps) == 2
    assert all(0 <= s <= FAST.max_delay for s in sleeps)


def test_sync_fails_when_retries_are_exhausted(db):
    arca = FakeArca([[comprobante_json(1)]], failures=99, status=500)
    run = build_service(db, arca, FakeReceptor()).run()

    assert run.status == "failed"
    assert "Se agotaron 4 intentos" in run.error
    assert len(arca.calls) == FAST.max_attempts
    assert db.scalar(select(func.count(Comprobante.id))) == 0


def test_sync_does_not_retry_4xx(db):
    arca = FakeArca([[comprobante_json(1)]], failures=1, status=400)
    run = build_service(db, arca, FakeReceptor()).run()
    assert run.status == "failed"
    assert len(arca.calls) == 1


def test_dedupe_within_batch_and_across_runs(db):
    page = [comprobante_json(1), comprobante_json(1), comprobante_json(2)]
    first = build_service(db, FakeArca([page]), FakeReceptor()).run()
    assert (first.created, first.duplicates) == (2, 1)

    # Segunda corrida: ARCA devuelve lo mismo + uno nuevo
    second = build_service(db, FakeArca([[*page, comprobante_json(3)]]), FakeReceptor()).run()
    assert (second.created, second.duplicates, second.pushed) == (1, 3, 1)
    assert db.scalar(select(func.count(Comprobante.id))) == 3


def test_incremental_since_uses_last_synced_date(db):
    build_service(
        db,
        FakeArca([[comprobante_json(1, "2026-02-01"), comprobante_json(2, "2026-02-15")]]),
        FakeReceptor(),
    ).run()

    arca = FakeArca([[]])
    run = build_service(db, arca, FakeReceptor()).run()
    assert run.since_date == date(2026, 2, 15)
    assert arca.calls[0].url.params["desde"] == "2026-02-15"


def test_receptor_failure_marks_partial_and_is_retried_next_run(db):
    arca_page = [[comprobante_json(1), comprobante_json(2)]]
    # El receptor falla siempre durante la primera corrida
    run = build_service(db, FakeArca(arca_page), FakeReceptor(failures=99)).run()
    assert run.status == "partial"
    assert (run.pushed, run.push_failed) == (0, 2)
    failed = db.scalars(select(Comprobante)).all()
    assert all(c.estado_envio == EstadoEnvio.FAILED and c.intentos_envio == 1 for c in failed)

    # En la siguiente corrida el receptor responde y se reenvían los fallidos
    receptor = FakeReceptor()
    run2 = build_service(db, FakeArca(arca_page), receptor).run()
    assert run2.status == "success"
    assert run2.pushed == 2
    assert len(receptor.received) == 2


def test_arca_token_is_refreshed_on_401(db):
    issued = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            issued.append(f"tok{len(issued) + 1}")
            exp = (datetime.now(UTC) + timedelta(hours=12)).isoformat()
            return httpx.Response(200, json={"token": issued[-1], "sign": "s", "expiration": exp})
        if request.headers["Authorization"] == "Bearer tok1":
            return httpx.Response(401)  # el primer token fue revocado
        return httpx.Response(200, json={"items": [comprobante_json(1)], "total_pages": 1})

    client = ArcaClient(
        "http://arca.test", CUIT, TokenEndpointAuth(CUIT), transport=httpx.MockTransport(handler)
    )
    items = client.fetch_comprobantes(date(2026, 1, 1))
    assert len(items) == 1
    assert issued == ["tok1", "tok2"]


def test_retry_exhausted_error_keeps_last_error():
    def always_fail():
        raise httpx.ConnectError("connection refused")

    with pytest.raises(RetryExhaustedError) as info:
        retry_call(always_fail, FAST, sleep=no_sleep)
    assert info.value.attempts == FAST.max_attempts
    assert isinstance(info.value.last_error, httpx.ConnectError)


# ---------------------------------------------------------------- integración con el mock Flask


def test_end_to_end_against_flask_mock(db):
    mock = create_mock_app(
        {"DATA_START": date(2026, 3, 1), "DATA_END": date(2026, 3, 5), "PER_DAY": 4}
    )
    transport = httpx.WSGITransport(app=mock)
    arca = ArcaClient(
        "http://mock", CUIT, TokenEndpointAuth(CUIT), transport=transport, page_size=7
    )
    receptor = ReceptorClient("http://mock/receptor", "receptor-dev-key", transport=transport)
    service = SyncService(db, arca, receptor, initial_date=date(2026, 3, 1))

    run = service.run()
    assert run.status == "success"
    assert run.fetched == run.created == run.pushed == 20

    received = httpx.Client(transport=transport, base_url="http://mock").get(
        "/receptor/api/comprobantes"
    )
    assert received.json()["total"] == 20

    # Corrida incremental: re-pide desde el último día y descarta los repetidos
    run2 = service.run()
    assert (run2.fetched, run2.created, run2.duplicates) == (4, 0, 4)


# ---------------------------------------------------------------- endpoints


def test_sync_endpoints(client, app, auth_headers):
    def override():
        with app.state.session_factory() as session:
            yield build_service(session, FakeArca([[comprobante_json(1)]]), FakeReceptor())

    app.dependency_overrides[get_sync_service] = override
    resp = client.post("/sync/run", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    run = resp.json()
    assert run["status"] == "success"

    runs = client.get("/sync/runs", headers=auth_headers).json()
    assert runs["total"] == 1
    assert client.get(f"/sync/runs/{run['id']}", headers=auth_headers).status_code == 200
    assert client.get("/sync/runs/999", headers=auth_headers).status_code == 404

    comps = client.get("/comprobantes", params={"estado": "sent"}, headers=auth_headers).json()
    assert comps["total"] == 1
    assert comps["items"][0]["clave"] == "20123456789-001-00002-00000001"
    comp_id = comps["items"][0]["id"]
    assert client.get(f"/comprobantes/{comp_id}", headers=auth_headers).status_code == 200
    app.dependency_overrides.clear()


def test_sync_run_model_persisted(db):
    build_service(db, FakeArca([[comprobante_json(1)]]), FakeReceptor()).run(trigger="scheduled")
    run = db.scalar(select(SyncRun))
    assert run.trigger == "scheduled"
    assert run.finished_at is not None
