"""Cliente de la API interna de LaLiga Fantasy. SOLO LECTURA: aquí no hay ni un POST de juego.

Rutas documentadas por la comunidad para la temporada 26/27. Ojo con la inconsistencia
de la propia app: clasificación y plantillas cuelgan de /leagues/{id}/..., mercado de /league/{id}/...
Si algo cambia, usa `fantasy probe <ruta>` para ver el JSON crudo y ajusta este archivo.
"""
from __future__ import annotations

import time
from typing import Any

from . import auth
from .config import Settings
from .http import request_json

BASE = "https://fantasy-api.llt-services.com/api"
COMP = "/v1/competition/1"


class FantasyAPI:
    def __init__(self, settings: Settings):
        self.s = settings
        self._last_call = 0.0
        self._cache: dict[str, Any] = {}

    # --- infraestructura -------------------------------------------------
    def get(self, path: str, *, authed: bool = True, params: dict | None = None, cache: bool = False) -> Any:
        key = f"{path}?{params}"
        if cache and key in self._cache:
            return self._cache[key]
        wait = self.s.request_delay_s - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)  # ritmo humano: no machacar la API
        headers = {"x-lang": "es", "x-app": "Fantasy"}
        if authed:
            headers["Authorization"] = f"Bearer {auth.bearer(self.s)}"
        data = request_json("GET", BASE + path, headers=headers, params=params)
        self._last_call = time.time()
        if cache:
            self._cache[key] = data
        return data

    # --- públicas ---------------------------------------------------------
    def players(self) -> list[dict]:
        return self.get(f"{COMP}/players", authed=False, cache=True)

    def player(self, player_id: str | int) -> dict:
        return self.get(f"{COMP}/player/{player_id}", authed=False, cache=True)

    def market_value_history(self, player_id: str | int) -> list[dict]:
        return self.get(f"{COMP}/player/{player_id}/market-value", authed=False, cache=True)

    def current_week(self) -> dict:
        return self.get(f"{COMP}/week/current", authed=False, cache=True)

    def calendar(self, week: int) -> Any:
        return self.get(f"{COMP}/calendar", authed=False, params={"weekNumber": week}, cache=True)

    # --- con sesión ---------------------------------------------------------
    def me(self) -> dict:
        return self.get("/v4/user/me", cache=True)

    def leagues(self) -> list[dict]:
        return self.get(f"{COMP}/leagues", cache=True)

    def standing(self, league_id: str) -> list[dict]:
        return self.get(f"{COMP}/leagues/{league_id}/standing", cache=True)

    def team(self, league_id: str, team_id: str) -> dict:
        return self.get(f"{COMP}/leagues/{league_id}/teams/{team_id}", cache=True)

    def market(self, league_id: str) -> list[dict]:
        return self.get(f"{COMP}/league/{league_id}/market", cache=True)

    def activity(self, league_id: str, index: int = 0) -> Any:
        return self.get(f"{COMP}/leagues/{league_id}/activity/{index}", cache=True)

    def clear_cache(self) -> None:
        self._cache.clear()
