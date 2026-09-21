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

    # --- escritura: gasta saldo real, SIEMPRE con confirmación previa del usuario ----------
    # Rutas sin documentar oficialmente; verificadas por coincidencia entre dos proyectos
    # independientes de la comunidad (mismo prefijo /v1/competition/1 que usamos nosotros),
    # pero no probadas todavía contra una cuenta real. Sin reintentos (retries=0): reintentar
    # una escritura financiera tras un timeout podría duplicar la operación.
    def _write(self, method: str, path: str, body: dict | None) -> Any:
        wait = self.s.request_delay_s - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        headers = {"x-lang": "es", "x-app": "Fantasy", "Authorization": f"Bearer {auth.bearer(self.s)}"}
        result = request_json(method, BASE + path, headers=headers, json_body=body, retries=0)
        self._last_call = time.time()
        return result

    def bid(self, league_id: str, market_id: str, amount: int) -> Any:
        """Puja por un anuncio del mercado de LaLiga. IRREVERSIBLE. Verificado: queda con
        estado "pending", el dinero no baja al instante (se resuelve más tarde)."""
        return self._write("POST", f"{COMP}/league/{league_id}/market/{market_id}/bid", {"money": amount})

    def update_bid(self, league_id: str, market_id: str, bid_id: str, amount: int) -> Any:
        """Cambia la cantidad de una puja PENDIENTE tuya. Hallado con OPTIONS (`Allow: PUT`);
        el cuerpo `{"money": cantidad}` es una suposición por analogía con la puja original,
        SIN verificar todavía. Un segundo POST no vale: da 400 030.01.09 "Team has pending
        bid in this player"."""
        return self._write("PUT", f"{COMP}/league/{league_id}/market/{market_id}/bid/{bid_id}", {"money": amount})

    def pay_clause(self, league_id: str, player_team_id: str, amount: int) -> Any:
        """Paga la cláusula de un jugador de otro manager. IRREVERSIBLE, instantáneo.
        Verificado: usa el id del hueco de plantilla (`playerTeamId`), no el id del jugador."""
        return self._write("POST", f"{COMP}/league/{league_id}/buyout/{player_team_id}/pay", {"buyoutClauseToPay": amount})

    def player_offers(self, league_id: str, player_team_id: str) -> Any:
        """Ofertas pendientes sobre un jugador tuyo (solo aparecen si lo has puesto a la
        venta). Solo lectura. Forma exacta sin verificar todavía — no hay ninguno listado
        para probarlo en real."""
        return self.get(f"{COMP}/league/{league_id}/playerTeam/{player_team_id}/offer")

    def list_for_sale(self, league_id: str, player_team_id: str, price: int) -> Any:
        """Pone un jugador tuyo a la venta. IRREVERSIBLE en el sentido de que empieza a
        recibir ofertas del juego en cada ciclo de mercado (21:00) hasta que aceptes,
        rechaces, o lo retires. No verificado todavía contra una cuenta real."""
        return self._write("POST", f"{COMP}/league/{league_id}/market/sell", {"playerId": player_team_id, "salePrice": price})

    def accept_offer(self, league_id: str, market_id: str, offer_id: str, amount: int) -> Any:
        """Acepta una oferta recibida por un jugador puesto a la venta. IRREVERSIBLE, gasta
        saldo cero pero CIERRA la venta. No verificado todavía."""
        return self._write("POST", f"{COMP}/league/{league_id}/market/{market_id}/offer/{offer_id}/accept", {"offerMoney": amount})

    def reject_offer(self, league_id: str, market_id: str, offer_id: str) -> Any:
        """Rechaza una oferta. No verificado todavía."""
        return self._write("POST", f"{COMP}/league/{league_id}/market/{market_id}/offer/{offer_id}/reject", None)

    def withdraw_from_market(self, league_id: str, market_id: str) -> Any:
        """Retira un anuncio tuyo del mercado (deja de estar a la venta). No verificado
        todavía contra una cuenta real."""
        return self._write("DELETE", f"{COMP}/league/{league_id}/market/{market_id}/delete", None)
