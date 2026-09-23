"""Rebaja de último segundo: si al final de la subasta eres el ÚNICO que ha pujado por un
jugador, no hace falta pagar más que el mínimo válido — se baja tu puja a ese mínimo.

Las pujas son ciegas, pero el anuncio dice cuántas hay (`numberOfBids`) y marca la tuya (`bid`).
Con `numberOfBids == 1` y tu puja, nadie más compite. Solo BAJA hasta el mínimo válido, así que
no puede hacerte gastar más; el riesgo es que alguien puje en el último segundo, después de la
lectura, y te lo lleve.

El cierre real del mercado NO es una hora fija: cada liga tiene su propio ciclo (una vez al día,
dos veces, el que sea) y este módulo no lo asume, lo LEE — `next_market_close()` calcula el
próximo cierre a partir del `expirationDate` de los anuncios que pone LaLiga (2026-09-23, arreglo
del problema real: el código anterior sí asumía 21:00 para todas las ligas). El `expirationDate`
de la API marca el cierre + 2 min (hora de proceso, ver CLAUDE.md — este desfase SÍ es fijo,
pero es un artefacto de la propia API, no una hora de cierre inventada), así que el cierre real
es `expirationDate - CLOSE_LEAD`. Quien dispara este trabajo (ver `cli._watch_once`) ya no
depende de un cron a una hora fija: arma el trabajo dinámicamente en cuanto detecta, en la
vigilancia normal de 30 min, que el próximo cierre está cerca.

`SNIPE_MODE`: `on` baja de verdad; cualquier otra cosa (por defecto `shadow`) solo cuenta lo que
haría. Además de decidir, toma lecturas a T-60s, T-30s, T-10s, T-3s, T+15s y T+90s para saber
cómo se comporta el contador de pujas y a qué hora cierra de verdad la subasta.

Mismo trabajo, mismo reloj: en T-60s también dispara la compra de flipeo (`flip.run`, petición
del usuario 2026-09-22). Antes `_buy` pujaba por la mañana, así que el anuncio enseñaba
`numberOfBids` todo el día y cualquier rival que lo mirase podía meterse a competir sabiendo
que había algo interesante ahí; pujando en el último minuto no da tiempo a que nadie reaccione.
El mundo con tendencias (`service.build_world(..., with_trends=True)`) se construye ANTES de
entrar en la espera de precisión, con margen de sobra (el trabajo se arma con antelación, ver
`cli._watch_once`) para no comerse ese margen con las llamadas de `market_value_history`."""
from __future__ import annotations

import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from . import analysis, flip, models, notify, service
from .analysis import b, esc, i
from .api import BASE, COMP, FantasyAPI
from .config import Settings
from .storage import Store

CLOSE_LEAD = timedelta(minutes=2)  # desfase de proceso de la API: expirationDate = cierre real + 2 min
FLIP_BUY_AT = -60  # segundos respecto al cierre: último minuto, sin tiempo a que nadie reaccione
ACT_AT = -10  # segundos respecto al cierre
SNAPSHOTS = (FLIP_BUY_AT, -30, ACT_AT, -3, 15, 90)
MAX_WAIT = timedelta(minutes=15)  # si el próximo cierre está más lejos, no hay nada que hacer aún


def close_time(item: models.MarketItem) -> datetime | None:
    return item.expires - CLOSE_LEAD if item.expires else None


def next_market_close(market: list[models.MarketItem], now: datetime) -> datetime | None:
    """Próximo cierre REAL del mercado de esta liga: el más próximo en el futuro entre los
    `close_time()` de los anuncios que pone LaLiga (no los de otros mánagers, que no marcan el
    ciclo del mercado). Es la única fuente de verdad sobre "cuándo cierra este mercado" — no
    asume ninguna hora fija ni que haya un solo cierre al día: cada liga puede tener su propio
    ciclo (uno diario, dos, el que sea), y esto se adapta solo porque lee el dato de la API en
    cada llamada. None si ahora mismo no hay ningún anuncio de LaLiga con cierre futuro visible
    (nadie puesto a la venta por el juego en este momento)."""
    closes = [c for it in market if it.seller == "LaLiga" and (c := close_time(it)) and c > now]
    return min(closes, default=None)


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
        next_close = next_market_close(first, now())
        if any(timedelta(0) < now() - c < timedelta(minutes=5) for c in all_closes) and not (
            next_close and next_close - now() <= MAX_WAIT
        ):
            return f"⚠️ {b('Rebaja de último segundo')}: el trabajo llegó tarde, el mercado ya había cerrado. No se hizo nada."
        if not next_close or next_close - now() > MAX_WAIT:
            return ""
        close = next_close

    # El mundo con tendencias para el flipeo se construye YA, con margen de sobra antes de
    # entrar en la espera de precisión: en el minuto final no hay tiempo para las llamadas de
    # `market_value_history` que hacen falta para puntuar candidatos.
    flip_world = None
    flip_store = None
    if s.flip_mode in ("on", "shadow"):
        try:
            flip_world = service.build_world(api, s, with_trends=True)
            flip_store = Store(s.db_file)
        except Exception as exc:
            print(f"[flip] error preparando el mundo para la compra del último minuto: {exc}")

    shots: dict[int, list[models.MarketItem]] = {}
    done: list[str] = []
    flip_notes: list[str] = []
    for off in SNAPSHOTS:
        target = close + timedelta(seconds=off)
        while now() < target:
            time.sleep(min(0.5, max(0.05, (target - now()).total_seconds())))
        items = snapshot()
        shots[off] = items
        if off == FLIP_BUY_AT and flip_world is not None:
            try:
                # `today` identifica el CIERRE que se está procesando, no el día de calendario
                # (bug real: antes era `datetime.now().isoformat()`, un valor distinto cada vez
                # que se llama, así que el "una vez por ciclo" de `flip.run` nunca se cumplía).
                # Usar `close` en vez de eso también deja procesar correctamente una liga con más
                # de un cierre el mismo día: cada cierre tiene su propio identificador.
                note = flip.run(api, flip_world, flip_store, s, buy_now=True, today=close.isoformat())
                if note:
                    flip_notes.append(note)
            except Exception as exc:
                flip_notes.append(f"❌ error en la compra del último minuto: {esc(str(exc))}")
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
    head = f"🎯 {b('Rebaja de último segundo')} ({tag}) · cierre {analysis.to_madrid(close).strftime('%H:%M:%S')}"
    body = done or [i("Nada que bajar: ninguna puja tuya estaba sola por encima del mínimo.")]
    result = "\n".join([head, "", *body, "", *_summary(shots)])
    if flip_notes:
        # El flipeo ya manda sus propios avisos (puja enviada, sin candidatos...); esto es solo
        # para que quede constancia en el resumen de que corrió en el último minuto.
        result += "\n\n" + f"🤖 {b('Flipeo (último minuto)')}: " + " · ".join(flip_notes)
    return result


def send(api: FantasyAPI, s: Settings, mode: str, close_in: float | None = None) -> None:
    text = run(api, s, mode, close_in)
    if text:
        notify.send_telegram(s, text)
