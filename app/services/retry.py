"""Reintentos con backoff exponencial y jitter ("full jitter", ver AWS Architecture Blog).

Implementación propia y chica para no depender de tenacity y poder inyectar `sleep`
y `random` en los tests.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx

T = TypeVar("T")
logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 30.0

    def compute_delay(self, attempt: int, rng: Callable[[], float] = random.random) -> float:
        """Delay para el intento `attempt` (1-based): random(0, min(max, base * 2^(n-1)))."""
        cap = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return cap * rng()


class RetryableHTTPError(Exception):
    """Respuesta HTTP que conviene reintentar (5xx, 429, etc.)."""

    def __init__(self, response: httpx.Response):
        self.response = response
        super().__init__(f"HTTP {response.status_code} en {response.request.url}")


class RetryExhaustedError(Exception):
    def __init__(self, attempts: int, last_error: BaseException):
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"Se agotaron {attempts} intentos: {last_error}")


def is_retryable_exception(exc: BaseException) -> bool:
    return isinstance(exc, RetryableHTTPError | httpx.TransportError)


def raise_for_retryable(response: httpx.Response) -> httpx.Response:
    """Convierte respuestas reintentables en excepción; el resto de errores HTTP se propaga."""
    if response.status_code in RETRYABLE_STATUS:
        raise RetryableHTTPError(response)
    response.raise_for_status()
    return response


def retry_call(
    func: Callable[[], T],
    policy: RetryPolicy,
    *,
    is_retryable: Callable[[BaseException], bool] = is_retryable_exception,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
    operation: str = "operation",
) -> T:
    """Ejecuta `func` reintentando ante errores transitorios."""
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return func()
        except Exception as exc:
            if not is_retryable(exc):
                raise
            if attempt == policy.max_attempts:
                raise RetryExhaustedError(attempt, exc) from exc
            delay = policy.compute_delay(attempt, rng)
            logger.warning(
                "Reintentando operación",
                extra={
                    "operation": operation,
                    "attempt": attempt,
                    "delay_s": round(delay, 3),
                    "error": str(exc),
                },
            )
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
