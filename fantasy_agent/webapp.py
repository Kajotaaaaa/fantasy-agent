"""Panel web, Fase 1: módulo de Mercado (2026-09-27, ver CLAUDE.md).

Servidor HTTP con SOLO librería estándar (regla del proyecto, ver CLAUDE.md): sirve el panel
estático (`webapp_static/`) y un endpoint JSON de solo lectura (`/app/api/market`) protegido con
un token firmado de larga duración (`dashboard_token.py`). Pensado para correr como hilo de
fondo dentro de `cli.cmd_watch` (el proceso que ya vive siempre encendido en la Raspberry Pi,
ver `deploy/fantasy-watch.service`) — así no hace falta un segundo proceso ni tocar el systemd.

Sin CORS ni multi-usuario: una sola cuenta, pensado para abrirse desde el móvil en la misma red
que la Pi (o detrás de un túnel que el propio usuario decida más adelante si quiere acceso desde
fuera de casa) — decisión deliberada para no montar infraestructura de más antes de saber si el
panel compensa (2026-09-27, "quiero ver las posibilidades que tiene esto").
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import dashboard_token, service
from .api import FantasyAPI

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

            if path == "/app/api/market":
                token = (query.get("token") or [""])[0]
                if not dashboard_token.verify(token):
                    self._json(401, {"detail": "Sesión caducada o inválida. Pide un enlace nuevo al bot (/menu → Abrir panel)."})
                    return
                try:
                    api = FantasyAPI(s)
                    world = service.build_world(api, s, with_trends=True)
                    data = service.market_data(world)
                except Exception as exc:
                    self._json(502, {"detail": f"No se pudo consultar LaLiga ahora mismo: {exc}"})
                    return
                self._json(200, data)
                return

            self.send_response(404)
            self.end_headers()

    return Handler


def start_background(s) -> ThreadingHTTPServer | None:
    """Arranca el servidor en un hilo de fondo (para `cmd_watch`, que ya vive siempre encendido).
    `WEB_PORT=0` desactiva el panel por completo (por si el usuario no quiere exponer nada)."""
    if not s.web_port:
        return None
    httpd = ThreadingHTTPServer(("0.0.0.0", s.web_port), _make_handler(s))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="fantasy-webapp")
    thread.start()
    return httpd


def serve_forever(s) -> None:
    """Primer plano (para `python -m fantasy_agent serve`, probar el panel sin arrancar toda la
    vigilancia)."""
    if not s.web_port:
        raise SystemExit("WEB_PORT está a 0: pon un puerto en el .env para servir el panel.")
    httpd = ThreadingHTTPServer(("0.0.0.0", s.web_port), _make_handler(s))
    print(f"🌐 Panel web en http://0.0.0.0:{s.web_port}/app/ (Ctrl+C para salir)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
