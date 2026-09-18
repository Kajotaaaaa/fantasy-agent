"""Login de LaLiga Fantasy: Azure AD B2C con el cliente OAuth de la app (PKCE).

Flujo en dos pasos (tú inicias sesión en tu navegador; el programa nunca ve tu contraseña):
  1. `fantasy auth url`   -> imprime la URL de login y guarda el verificador PKCE.
  2. Inicias sesión, copias la URL `authredirect://...?code=...` desde DevTools (Network).
  3. `fantasy auth code '<url>'` -> canjea el código por tokens (se guardan con permisos 0600).
Después el refresh token mantiene la sesión viva sin volver a hacer login.

Valores tomados de la ingeniería inversa de la comunidad; si LaLiga los cambia, edítalos aquí.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from pathlib import Path

from .config import Settings
from .http import request_json

TENANT = "laligadspprob2c.onmicrosoft.com"
POLICY = "B2C_1A_5ULAIP_PARAMETRIZED_SIGNIN"
CLIENT_ID = "af88bcff-1157-40a0-b579-030728aacf0b"
REDIRECT_URI = "authredirect://com.lfp.laligafantasy"
SCOPE = "openid offline_access"
AUTHORIZE_URL = f"https://login.laliga.es/{TENANT}/oauth2/v2.0/authorize"
TOKEN_URL = f"https://login.laliga.es/{TENANT}/oauth2/v2.0/token"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _write_private(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)


def build_login_url(settings: Settings) -> str:
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(16)
    params = {
        "p": POLICY,
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": secrets.token_urlsafe(16),
    }
    _write_private(
        settings.pending_auth_file,
        {"verifier": verifier, "state": state, "created": time.time()},
    )
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def parse_redirect(url: str) -> tuple[str, str | None]:
    query = urllib.parse.urlparse(url.strip()).query
    qs = urllib.parse.parse_qs(query)
    if "error" in qs:
        raise RuntimeError(f"B2C devolvió error: {qs.get('error_description', qs['error'])}")
    if "code" not in qs:
        raise RuntimeError("La URL no contiene ?code=. Copia la petición (canceled) authredirect://...")
    return qs["code"][0], qs.get("state", [None])[0]


def _store_tokens(settings: Settings, payload: dict) -> dict:
    now = time.time()
    expires_in = int(payload.get("expires_in") or payload.get("id_token_expires_in") or 3600)
    tokens = {
        "access_token": payload.get("access_token"),
        "id_token": payload.get("id_token"),
        "refresh_token": payload.get("refresh_token"),
        "expires_at": now + expires_in,
        "obtained_at": now,
    }
    old = load_tokens(settings)
    if not tokens["refresh_token"] and old:
        tokens["refresh_token"] = old.get("refresh_token")
    _write_private(settings.tokens_file, tokens)
    return tokens


def exchange_code(settings: Settings, redirect_url: str) -> dict:
    if not settings.pending_auth_file.exists():
        raise RuntimeError("No hay login pendiente. Ejecuta primero `fantasy auth url`.")
    pending = json.loads(settings.pending_auth_file.read_text(encoding="utf-8-sig"))
    if time.time() - pending["created"] > 15 * 60:
        raise RuntimeError("El login pendiente ha caducado (15 min). Repite `fantasy auth url`.")
    code, state = parse_redirect(redirect_url)
    if state and state != pending["state"]:
        raise RuntimeError("El 'state' no coincide: usa la URL generada por el último `auth url`.")
    payload = request_json(
        "POST",
        TOKEN_URL,
        params={"p": POLICY},
        form={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": pending["verifier"],
            "scope": SCOPE,
        },
    )
    settings.pending_auth_file.unlink(missing_ok=True)
    return _store_tokens(settings, payload)


def load_tokens(settings: Settings) -> dict | None:
    if not settings.tokens_file.exists():
        return None
    return json.loads(settings.tokens_file.read_text(encoding="utf-8-sig"))


def refresh(settings: Settings) -> dict:
    tokens = load_tokens(settings)
    if not tokens or not tokens.get("refresh_token"):
        raise RuntimeError("Sin sesión. Ejecuta `fantasy auth url` y `fantasy auth code`.")
    payload = request_json(
        "POST",
        TOKEN_URL,
        params={"p": POLICY},
        form={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": tokens["refresh_token"],
            "scope": SCOPE,
        },
    )
    return _store_tokens(settings, payload)


def bearer(settings: Settings) -> str:
    """Devuelve un token válido, refrescándolo si caduca en < 5 minutos."""
    tokens = load_tokens(settings)
    if not tokens:
        raise RuntimeError("Sin sesión. Ejecuta `fantasy auth url` y `fantasy auth code`.")
    if tokens["expires_at"] - time.time() < 300:
        tokens = refresh(settings)
    token = tokens.get("access_token") or tokens.get("id_token")
    if not token:
        raise RuntimeError("La respuesta de login no trae access_token ni id_token.")
    return token
