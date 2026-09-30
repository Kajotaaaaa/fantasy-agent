"""Panel web, Fase 1: módulo de Mercado (2026-09-27, ver CLAUDE.md).

Servidor HTTP con SOLO librería estándar (regla del proyecto, ver CLAUDE.md): sirve el panel
estático (`webapp_static/`) y un endpoint JSON de solo lectura (`/app/api/market`) protegido con
un token firmado de larga duración (`dashboard_token.py`).

**No hay ninguna máquina propia siempre encendida** (no hay Raspberry Pi ni nada parecido: la
vigilancia real vive en `tick`, ejecuciones sueltas de GitHub Actions cada ~30 min) — así que este
servidor se despliega como servicio propio siempre encendido en Render (plan free), corriendo
`python -m fantasy_agent serve` (2026-09-27, decisión del usuario: "desplegar un servicio
gratuito siempre encendido"). Eso deja DOS procesos vivos por separado (el tick de GitHub Actions
y este servicio en Render) que podrían competir por refrescar la sesión de LaLiga a la vez — para
evitarlo, este servidor NUNCA refresca el token (`api.FantasyAPI(s, refresh_session=False)` /
`auth.bearer_readonly`): recibe la sesión ya refrescada por el vigilante en cada tick, empujada a
`/internal/sync-tokens` (protegido con `WEBAPP_SYNC_SECRET`, compartido con el workflow de
GitHub Actions). El disco de Render (plan free) es efímero — se pierde al reiniciar/reescalar a
cero — por eso hace falta este empuje periódico en vez de fiarse de que el archivo siga ahí.

Sin CORS ni multi-usuario: una sola cuenta.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import dashboard_token, service
from .api import FantasyAPI
from .auth import _write_private

STATIC_DIR = Path(__file__).resolve().parent / "webapp_static"

_STATIC_FILES = {
    "/app/": ("index.html", "text/html; charset=utf-8"),
    "/app/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app/manifest.json": ("manifest.json", "application/manifest+json"),
}


def _make_handler(s):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # el log de systemd ya se llena con la vigilancia; silencioso salvo error real

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (nombre fijo de BaseHTTPRequestHandler)
            parsed = urlsplit(self.path)
            path, query = parsed.path, parse_qs(parsed.query)

            static = _STATIC_FILES.get(path)
            if static:
                name, content_type = static
                data = (STATIC_DIR / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if path == "/healthz":
                # Ping del Worker cada 10 min para que Render no duerma el servicio (y borre la
                # sesión con el disco). Sin token: solo dice si ya hay sesión, nunca la enseña.
                self._json(200, {"ok": True, "session": s.tokens_file.exists()})
                return

            if path == "/app/api/market":
                token = (query.get("token") or [""])[0]
                if not dashboard_token.verify(token):
                    self._json(401, {"detail": "Sesión caducada o inválida. Pide un enlace nuevo al bot (/menu → Abrir panel)."})
                    return
                try:
                    api = FantasyAPI(s, refresh_session=False)
                    world = service.build_world(api, s, with_trends=True)
                    data = service.market_data(world)
                except Exception as exc:
                    self._json(502, {"detail": f"No se pudo consultar LaLiga ahora mismo: {exc}"})
                    return
                self._json(200, data)
                return

            self.send_response(404)
            self.end_headers()

        def do_POST(self):  # noqa: N802
            if self.path != "/internal/sync-tokens":
                self.send_response(404)
                self.end_headers()
                return
            secret = os.environ.get("WEBAPP_SYNC_SECRET")
            if not secret or self.headers.get("X-Sync-Secret") != secret:
                self._json(403, {"detail": "Secreto de sincronización inválido."})
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            try:
                tokens = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {"detail": "Cuerpo no es JSON válido."})
                return
            s.tokens_file.parent.mkdir(parents=True, exist_ok=True)
            _write_private(s.tokens_file, tokens)
            self._json(200, {"ok": True})

    return Handler


def _port(s) -> int:
    """Render asigna el puerto vía la variable de entorno estándar `PORT`; en local manda
    `WEB_PORT` del .env."""
    return int(os.environ.get("PORT") or s.web_port)


def start_background(s) -> ThreadingHTTPServer | None:
    """Arranca el servidor en un hilo de fondo, para quien prefiera correr `watch` en su propio
    ordenador en vez de desplegar un servicio aparte. `WEB_PORT=0` lo desactiva."""
    if not s.web_port:
        return None
    httpd = ThreadingHTTPServer(("0.0.0.0", _port(s)), _make_handler(s))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="fantasy-webapp")
    thread.start()
    return httpd


def serve_forever(s) -> None:
    """Primer plano: `python -m fantasy_agent serve` (el comando que corre el servicio siempre
    encendido en Render, ver CLAUDE.md)."""
    port = _port(s)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(s))
    print(f"🌐 Panel web en http://0.0.0.0:{port}/app/ (Ctrl+C para salir)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
