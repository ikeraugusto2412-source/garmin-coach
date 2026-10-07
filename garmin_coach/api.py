"""Llamadas a Garmin con pausas, backoff ante 429 y tolerancia a endpoints que fallan.

La librería ya reintenta 5xx/errores de red, pero los 429 fallan al instante:
aquí se esperan con backoff exponencial y, si persisten, se aborta la
sincronización (seguir martilleando solo alarga el bloqueo).
"""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

log = logging.getLogger(__name__)


class RateLimited(RuntimeError):
    """Garmin sigue devolviendo 429 tras todos los reintentos."""


@dataclass
class CallResult:
    name: str
    ok: bool
    data: Any = None
    error: str | None = None
    empty: bool = False


@dataclass
class Caller:
    delay: float = field(default_factory=lambda: float(os.getenv("GARMIN_REQUEST_DELAY", "1.0")))
    max_429_retries: int = 4
    base_429_wait: float = 30.0
    sleep: Callable[[float], None] = time.sleep
    _last: float = 0.0
    failures: list[CallResult] = field(default_factory=list)

    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            self.sleep(wait)
        self._last = time.monotonic()

    def call(self, name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> CallResult:
        """Ejecuta `fn`; nunca lanza salvo por 429 persistente o sesión inválida."""
        for attempt in range(self.max_429_retries + 1):
            self._throttle()
            try:
                data = fn(*args, **kwargs)
                return CallResult(name, True, data, empty=_is_empty(data))
            except GarminConnectTooManyRequestsError:
                if attempt == self.max_429_retries:
                    raise RateLimited(
                        f"Garmin devolvió 429 de forma persistente en '{name}'. "
                        "Reanuda la sincronización más tarde: se continuará donde se quedó."
                    )
                wait = self.base_429_wait * (2**attempt)
                log.warning("429 en %s; esperando %.0fs (intento %d)", name, wait, attempt + 1)
                self.sleep(wait)
            except GarminConnectAuthenticationError:
                raise
            except GarminConnectNotFoundError as e:
                res = CallResult(name, False, error=f"404 no disponible: {e}")
                break
            except (GarminConnectConnectionError, ValueError, KeyError, TypeError) as e:
                res = CallResult(name, False, error=f"{type(e).__name__}: {e}")
                break
        log.info("Endpoint %s falló: %s", name, res.error)
        self.failures.append(res)
        return res


def _is_empty(data: Any) -> bool:
    if data is None:
        return True
    if isinstance(data, (list, dict, str)) and len(data) == 0:
        return True
    return False


def wait_for_network(hosts: tuple[str, ...] = ("connectapi.garmin.com", "github.com"), timeout: float = 600,
                     interval: float = 10, sleep: Callable[[float], None] = time.sleep,
                     resolve: Callable[..., Any] = socket.getaddrinfo) -> bool:
    """Espera a que haya conexión (DNS de Garmin y GitHub). Al despertar, el Mac lanza la tarea
    programada unos segundos antes de reconectar el Wi-Fi."""
    waited = 0.0
    while True:
        try:
            for h in hosts:
                resolve(h, 443)
            return True
        except OSError:
            if waited >= timeout:
                return False
            sleep(interval)
            waited += interval
