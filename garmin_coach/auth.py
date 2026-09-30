"""Autenticación con Garmin Connect.

Credenciales desde .env; tokens de sesión persistidos en ~/.garminconnect
(garmin_tokens.json) para no repetir el login ni el MFA en cada ejecución.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TOKENSTORE = "~/.garminconnect"


class AuthError(RuntimeError):
    """Error de login con un mensaje ya pensado para el usuario."""


def _prompt_mfa() -> str:
    return input("Código MFA de Garmin (revisa tu email/app): ").strip()


def tokenstore_path() -> Path:
    load_dotenv(PROJECT_ROOT / ".env")
    return Path(os.getenv("GARMINTOKENS", DEFAULT_TOKENSTORE)).expanduser()


def get_client(interactive: bool = True) -> Garmin:
    """Devuelve un cliente autenticado.

    Primero intenta reutilizar los tokens guardados; si no hay o han caducado,
    hace login con email/contraseña del .env (pidiendo MFA por consola si
    `interactive`). En modo no interactivo (servidor MCP) nunca pide MFA.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    email = os.getenv("GARMIN_EMAIL") or None
    password = os.getenv("GARMIN_PASSWORD") or None
    store = tokenstore_path()
    store.mkdir(mode=0o700, parents=True, exist_ok=True)

    has_tokens = (store / "garmin_tokens.json").exists()
    if not has_tokens and not (email and password):
        raise AuthError(
            "No hay tokens guardados ni credenciales. Copia .env.example a .env, "
            "rellena GARMIN_EMAIL y GARMIN_PASSWORD y ejecuta "
            "`python -m garmin_coach login`."
        )
    if not has_tokens and not interactive:
        raise AuthError(
            "No hay sesión guardada. Ejecuta primero `python -m garmin_coach login` "
            "en una terminal (por si Garmin pide código MFA)."
        )

    client = Garmin(
        email=email,
        password=password,
        prompt_mfa=_prompt_mfa if interactive else None,
        retry_attempts=3,
        retry_min_wait=2.0,
        retry_max_wait=30.0,
    )
    try:
        client.login(str(store))
    except GarminConnectTooManyRequestsError as e:
        raise AuthError(
            "Garmin ha limitado los intentos de login (429). Espera 15-30 minutos "
            "antes de reintentar; insistir alarga el bloqueo."
        ) from e
    except Exception as e:
        if "MFA" in str(e) and not interactive:
            raise AuthError(
                "La sesión ha caducado y Garmin pide código MFA. Ejecuta "
                "`python -m garmin_coach login` en una terminal y vuelve a intentarlo."
            ) from e
        if isinstance(e, GarminConnectAuthenticationError):
            raise AuthError(f"Login rechazado por Garmin: {e}") from e
        raise

    # Garantiza que los tokens queden en disco aunque vinieran de otro sitio.
    try:
        client.client.dump(str(store))
        (store / "garmin_tokens.json").chmod(0o600)
    except Exception as e:  # noqa: BLE001 - no bloquea el uso de la sesión
        log.warning("No se pudieron guardar los tokens en %s: %s", store, e)
    return client
