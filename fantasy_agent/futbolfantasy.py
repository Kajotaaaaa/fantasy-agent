"""Onces probables reales desde futbolfantasy.com: gratis, sin API key.

A diferencia de `attendance.py` (que solo mira si jugó en jornadas pasadas), esto usa su
valoración editorial diaria — lesiones, ruedas de prensa, rotaciones — que es justo lo que
hace que sus números no se parezcan a un simple "jugó X/5". Es scraping de un tercero: si
cambian su HTML esto deja de encontrar datos con normalidad, así que cada paso falla en
silencio (o con un aviso) y el llamador debe rellenar los huecos con `attendance.py`.
"""
from __future__ import annotations

import re
import unicodedata

from .http import request_text
from .models import Player

BASE = "https://www.futbolfantasy.com"
INDEX_URL = f"{BASE}/laliga/posibles-alineaciones"

# Alias únicos primero (para no confundir "Deportivo Alavés" con "RC Deportivo", o
# "Atlético" con "Athletic"): se usa el primer alias que aparezca en el nombre normalizado.
TEAM_ALIASES = [
    "alaves", "rayo", "sociedad", "racing", "betis", "celta", "elche", "espanyol",
    "getafe", "levante", "malaga", "osasuna", "villarreal", "valencia", "atletico",
    "athletic", "barcelona", "sevilla", "madrid", "deportivo",
]


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def _team_alias(team_name: str) -> str | None:
    norm = _normalize(team_name)
    for alias in TEAM_ALIASES:
        if alias in norm:
            return alias
    return None


def _find_match_urls(index_html: str, wanted_aliases: set[str]) -> dict[str, str]:
    """alias de equipo -> URL del partido, para los equipos que nos interesan."""
    out: dict[str, str] = {}
    for m in re.finditer(r'href="(https://www\.futbolfantasy\.com/partidos/[^"]+)"[^>]*data-tooltip="([^"]+)"', index_html):
        url, tooltip = m.group(1), m.group(2)
        for side in re.split(r"\s*-\s*", tooltip):
            alias = _team_alias(side)
            if alias in wanted_aliases and alias not in out:
                out[alias] = url
    return out


def _modal_names(html: str) -> dict[str, str]:
    """id de modal -> nombre completo del jugador."""
    out = {}
    for m in re.finditer(r'id="opcion-jugador-(\d+)"[^>]*>.*?<h5[^>]*>([^<]+)</h5>', html, re.S):
        out[m.group(1)] = m.group(2).strip()
    return out


def _player_rows(html: str) -> list[dict]:
    """Cada jugador que aparece en el campo (probable titular/suplente) con sus datos.
    OJO: la página tiene otros widgets (jugadores de otras ligas, "destacados"...) que
    reutilizan el mismo patrón `jugador_<id> campo`. Hay que acotar la búsqueda a la zona
    real de las alineaciones (entre los contenedores `campo-wrapper` local/visitante/
    suplentes) o se cuela gente que no tiene nada que ver con este partido."""
    wrappers = [m.start() for m in re.finditer(r"campo-wrapper", html)]
    if not wrappers:
        return []
    zone_start, zone_end = wrappers[0], wrappers[-1] + 300_000
    starts = [m.start() for m in re.finditer(r'jugador_\d+\s+campo', html) if zone_start <= m.start() <= zone_end]
    rows = []
    for i, start in enumerate(starts):
        # Cada bloque de jugador es enorme (repite sus datos por cada modo de fantasy
        # soportado: comunio, biwenger, futmondo...), fácilmente 20-40k caracteres — un
        # límite corto (p.ej. 4000) corta antes de llegar a data-target y se pierde al jugador.
        end = starts[i + 1] if i + 1 < len(starts) else start + 60_000
        chunk = html[start:min(end, start + 60_000)]
        once = re.search(r'data-onceFF="([^"]*)"', chunk)
        prob = re.search(r'data-probabilidad="(\d+)%"', chunk)
        lesion = re.search(r'data-lesion="([^"]*)"', chunk)
        modal = re.search(r'data-target="#opcion-jugador-(\d+)"', chunk)
        if not modal:
            continue
        rows.append({
            "once": once.group(1) if once else "",
            "prob": int(prob.group(1)) if prob else None,
            "lesion": lesion.group(1) if lesion else "-1",
            "modal_id": modal.group(1),
        })
    return rows


def _match_player(ff_name: str, candidates: list[Player]) -> Player | None:
    norm_ff = _normalize(ff_name)
    for p in candidates:
        norm_p = _normalize(p.name)
        if norm_p == norm_ff or norm_p in norm_ff or norm_ff in norm_p:
            return p
    # último recurso: mismo apellido (última palabra)
    last = norm_ff.split()[-1] if norm_ff else ""
    for p in candidates:
        if last and last in _normalize(p.name):
            return p
    return None


def fetch_probable_lineups(players: list[Player]) -> dict[str, dict]:
    """id de jugador -> {"start_probability", "status", "note"}. Solo incluye a quienes
    encontramos y pudimos casar por nombre; el resto lo debe rellenar el llamador."""
    by_team: dict[str, list[Player]] = {}
    for p in players:
        by_team.setdefault(p.team, []).append(p)
    wanted = {a for t in by_team for a in [_team_alias(t)] if a}
    if not wanted:
        return {}

    index_html = request_text(INDEX_URL)
    match_urls = _find_match_urls(index_html, wanted)

    results: dict[str, dict] = {}
    seen_urls: set[str] = set()
    for team_name, team_players in by_team.items():
        alias = _team_alias(team_name)
        url = match_urls.get(alias) if alias else None
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            match_html = request_text(url)
        except Exception:
            continue
        names = _modal_names(match_html)
        # Un partido tiene jugadores de DOS equipos; cada _player_rows() cubre a los que
        # aparecen en el campo (de ambos lados), así que se casan contra TODOS los
        # jugadores de los equipos que juegan este partido, no solo `team_players`.
        rival_alias = next((a for a, u in match_urls.items() if u == url and a != alias), None)
        pool = list(team_players)
        if rival_alias:
            for t, ps in by_team.items():
                if _team_alias(t) == rival_alias:
                    pool.extend(ps)
        for row in _player_rows(match_html):
            name = names.get(row["modal_id"])
            if not name:
                continue
            player = _match_player(name, pool)
            if not player or player.id in results:
                continue
            if row["prob"] is not None:
                prob = row["prob"]
            elif row["once"] == "titular":
                prob = 85
            elif row["once"] == "suplente":
                prob = 25
            else:
                prob = 50
            lesionado = row["lesion"] not in ("-1", "", None)
            status = "lesionado" if lesionado else ("duda" if prob < 50 else "ok")
            note = f"futbolfantasy: {row['once'] or 'sin datos'} ({prob}%)"
            results[player.id] = {"start_probability": prob, "status": status, "note": note}
    return results
