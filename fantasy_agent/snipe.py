"""Rebaja de último segundo: si al final de la subasta eres el ÚNICO que ha pujado por un
jugador, no hace falta pagar más que el mínimo válido — se baja tu puja a ese mínimo.

Las pujas son ciegas, pero el anuncio dice cuántas hay (`numberOfBids`) y marca la tuya (`bid`).
Con `numberOfBids == 1` y tu puja, nadie más compite. Solo BAJA hasta el mínimo válido, así que
no puede hacerte gastar más; el riesgo es que alguien puje en el último segundo, después de la
lectura, y te lo lleve.

Las pujas cierran a las 21:00:00 (dato del usuario); el `expirationDate` de la API marca 21:02
(hora de proceso, ver CLAUDE.md), así que el cierre es `expirationDate - 2 min`. El trabajo lo
lanza el Worker de Cloudflare (puntual al minuto; el cron de GitHub se retrasa minutos) a las
20:50, espera hasta 10 s antes del cierre, lee el mercado y actúa.

`SNIPE_MODE`: `on` baja de verdad; cualquier otra cosa (por defecto `shadow`) solo cuenta lo que
haría. Además de decidir, toma lecturas a T-30s, T-10s, T-3s, T+15s y T+90s para saber cómo se
comporta el contador de pujas y a qué hora cierra de verdad la subasta."""
from __future__ import annotations

import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from . import models, notify, service
from .analysis import b, esc, i
from .api import BASE, COMP, FantasyAPI
from .config import Settings

CLOSE_LEAD = timedelta(minutes=2)  # expirationDate (21:02) - 2 min = cierre real (21:00)
ACT_AT = -10  # segundos respecto al cierre
SNAPSHOTS = (-30, ACT_AT, -3, 15, 90)
MAX_WAIT = timedelta(minutes=15)  # si el próximo cierre está más lejos, no hay nada que hacer aún


def close_time(item: models.MarketItem) -> datetime | None:
    return item.expires - CLOSE_LEAD if item.expires else None


def is_protected(item: models.MarketItem, top_ids: set[str], skip: set[str]) -> bool:
    """Jugadores a los que NO se les rebaja la puja aunque estés solo: los TOP de LaLiga en su
    posición (un Lamine Yamal: si un rival puja en los últimos 10 s por encima del mínimo, con la
    puja rebajada lo pierdes; con la tuya alta, no) y los de la lista manual `SNIPE_SKIP`
    (nombres o ids separados por comas)."""
    p = item.player
    return p.id in top_ids or p.id in skip or p.name.strip().lower() in skip


def plan_reductions(
    items: list[models.MarketItem], top_ids: set[str] | None = None, skip: set[str] | None = None,
) -> list[tuple[models.MarketItem, int]]:
    """Anuncios donde solo has pujado tú y tu puja está por encima del mínimo válido de ahora:
    (anuncio, cantidad a la que bajarla). Se saltan los protegidos (`is_protected`)."""
    out = []
    for it in items:
        if it.seller != "LaLiga" or not it.my_bid_id or it.bids != 1:
            continue
        if is_protected(it, top_ids or set(), skip or set()):
            continue
        minimum = service.bid_amount(it)
        if it.my_bid > minimum:
            out.append((it, minimum))
    return out


def _server_offset() -> timedelta:
    """Diferencia entre el reloj del servidor de LaLiga y el local (cabecera `Date`, precisión
    de 1 s): la subasta se decide con SU reloj, no con el del runner."""
    try:
        req = urllib.request.Request(BASE + f"{COMP}/week/current", headers={"User-Agent": "okhttp/4.12.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
            server = parsedate_to_datetime(resp.headers["Date"])
        return server - datetime.now(timezone.utc)
    except Exception:
        return timedelta(0)


def _summary(shots: dict[int, list[models.MarketItem]]) -> list[str]:
    lines = []
    for offset, items in sorted(shots.items()):
        label = f"T{offset:+d}s"
        contested = [
            f"{it.player.name} ×{it.bids}{' (mía)' if it.my_bid_id else ''}"
            for it in items if it.seller == "LaLiga" and (it.bids or it.my_bid_id)
        ]
        mine = [f"{it.player.name} {it.my_bid_id and service.m(it.my_bid)}" for it in items if it.my_bid_id]
        lines.append(
            f"{b(label)}: {len(items)} anuncios · con pujas: {esc(', '.join(contested) or 'ninguno')}"
            + (f" · mi puja: {esc(', '.join(mine))}" if mine else "")
        )
    return lines


def run(api: FantasyAPI, s: Settings, mode: str, close_in: float | None = None) -> str:
    """Ejecuta la rebaja para el próximo cierre. `close_in`: solo para probar (segundos hasta
    un cierre ficticio). Devuelve el resumen que se manda por Telegram ("" si no había cierre
    cercano)."""
    live = mode == "on"
    league_id, _, _ = service.resolve_league(api, s)
    top_ids = service.league_top_ids(api)
    skip = {x.strip().lower() for x in (os.environ.get("SNIPE_SKIP") or "").split(",") if x.strip()}
    offset = _server_offset()
    now = lambda: datetime.now(timezone.utc) + offset  # noqa: E731

    def snapshot() -> list[models.MarketItem]:
        api.clear_cache()
        return models.parse_market(api.market(league_id))

    first = snapshot()
    if close_in is not None:
        close = now() + timedelta(seconds=close_in)
    else:
        all_closes = [c for it in first if it.seller == "LaLiga" and (c := close_time(it))]
        closes = sorted(c for c in all_closes if c > now())
        if any(timedelta(0) < now() - c < timedelta(minutes=5) for c in all_closes) and not (
            closes and closes[0] - now() <= MAX_WAIT
        ):
            return f"⚠️ {b('Rebaja de último segundo')}: el trabajo llegó tarde, el mercado ya había cerrado. No se hizo nada."
        if not closes or closes[0] - now() > MAX_WAIT:
            return ""
        close = closes[0]

    shots: dict[int, list[models.MarketItem]] = {}
    done: list[str] = []
    for off in SNAPSHOTS:
        target = close + timedelta(seconds=off)
        while now() < target:
            time.sleep(min(0.5, max(0.05, (target - now()).total_seconds())))
        items = snapshot()
        shots[off] = items
        if off == ACT_AT:
            for it in items:
                if it.my_bid_id and it.bids == 1 and is_protected(it, top_ids, skip):
                    done.append(f"🛡️ {it.player.name}: se queda en {service.m(it.my_bid)} (protegido: TOP de liga o en SNIPE_SKIP)")
            for it, amount in plan_reductions(items, top_ids, skip):
                if live:
                    try:
                        api.update_bid(league_id, it.listing_id, it.my_bid_id, amount)
                        done.append(f"⬇️ {it.player.name}: {service.m(it.my_bid)} → {service.m(amount)} (nadie más pujaba)")
                    except Exception as exc:
                        done.append(f"❌ {it.player.name}: no pude bajar la puja ({esc(str(exc))})")
                else:
                    done.append(f"🕶️ Habría bajado {it.player.name}: {service.m(it.my_bid)} → {service.m(amount)} (nadie más pujaba)")

    tag = "real" if live else "sombra"
    # `close` hereda la zona del expirationDate (+02:00 hora de España); en pruebas va en UTC.
    head = f"🎯 {b('Rebaja de último segundo')} ({tag}) · cierre {close.strftime('%H:%M:%S')}"
    body = done or [i("Nada que bajar: ninguna puja tuya estaba sola por encima del mínimo.")]
    return "\n".join([head, "", *body, "", *_summary(shots)])


def send(api: FantasyAPI, s: Settings, mode: str, close_in: float | None = None) -> None:
    text = run(api, s, mode, close_in)
    if text:
        notify.send_telegram(s, text)
