"""Token firmado de larga duración para el panel web (Fase 1, 2026-09-27, ver CLAUDE.md).

Sin usuarios ni tenants (una sola cuenta, a diferencia de sniperfantasy): el token solo prueba
"esto lo emitió alguien que conoce DASHBOARD_TOKEN_SECRET", nada más. Mismo esquema HMAC-SHA256
que ya usa `auth.py` para la sesión de LaLiga, pero con secreto y propósito propios (nunca debe
poder generarse ni verificarse con ningún otro secreto del proyecto).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

from . import config  # noqa: F401  (fuerza la carga del .env antes de leer el secreto)

TOKEN_TTL_SECONDS = 90 * 24 * 3600  # pensado para vivir en el móvil sin pedir el enlace de nuevo
_PURPOSE = "dash"


def _secret() -> bytes:
    key = os.environ.get("DASHBOARD_TOKEN_SECRET")
    if not key:
        raise RuntimeError("Falta DASHBOARD_TOKEN_SECRET en el .env.")
    return key.encode()


def issue() -> str:
    expires_at = int(time.time()) + TOKEN_TTL_SECONDS
    signature = hmac.new(_secret(), f"{_PURPOSE}.{expires_at}".encode(), hashlib.sha256).hexdigest()
    return f"{expires_at}.{signature}"


def verify(token: str) -> bool:
    try:
        expires_at_str, signature = token.split(".", 1)
        expires_at = int(expires_at_str)
    except (ValueError, AttributeError):
        return False
    if time.time() > expires_at:
        return False
    expected = hmac.new(_secret(), f"{_PURPOSE}.{expires_at}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
