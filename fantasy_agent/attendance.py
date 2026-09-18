"""Estimación de titularidad SIN servicios externos: gratis, sin API keys ni límites.

Mira cuántas de las últimas jornadas completadas ha sumado puntos cada jugador (weekPoints,
de la API pública de jugadores) como proxy de si suele jugar. No sabe el motivo si no juega
(lesión, sanción, suplente...); para eso ya usamos el campo `playerStatus` real de la API,
que no depende de esto.
"""
from __future__ import annotations

from . import futbolfantasy
from .api import FantasyAPI
from .models import Player, to_int, week_points_by_id

WINDOW = 5  # cuántas últimas jornadas completadas se miran
MIN_PROB, MAX_PROB = 0.10, 0.95
DEFAULT_PROB = 0.70  # sin histórico suficiente (fichaje nuevo, etc.)


def estimate_start_probability(api: FantasyAPI, players: list[Player]) -> dict[str, dict]:
    current_week = to_int((api.current_week() or {}).get("weekNumber"), default=0)
    by_id = week_points_by_id(api.players())
    results: dict[str, dict] = {}
    for p in players:
        weeks = [pts for wn, pts in by_id.get(p.id, []) if 0 < wn < current_week]
        weeks = weeks[-WINDOW:]
        if not weeks:
            results[p.id] = {"start_probability": round(DEFAULT_PROB * 100), "note": "sin histórico reciente"}
            continue
        played = sum(1 for pts in weeks if pts > 0)
        prob = max(MIN_PROB, min(MAX_PROB, played / len(weeks)))
        results[p.id] = {
            "start_probability": round(prob * 100),
            "note": f"jugó {played}/{len(weeks)} de las últimas jornadas",
        }
    return results


def estimate_titularidad(api: FantasyAPI, players: list[Player]) -> dict[str, dict]:
    """Noticias reales (futbolfantasy.com) primero; si no encontramos a alguien —cambiaron
    su web, no está en el campo probable, lo que sea— cae al histórico de jornadas."""
    try:
        real = futbolfantasy.fetch_probable_lineups(players)
    except Exception as exc:
        print(f"[titularidad] futbolfantasy no disponible, uso histórico: {exc}")
        real = {}
    missing = [p for p in players if p.id not in real]
    if missing:
        real.update(estimate_start_probability(api, missing))
    return real
