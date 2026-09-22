"""Flipeo autónomo: comprar jugadores de LaLiga en subida y revenderlos en pocos días.

Es la única parte del bot que mueve dinero SIN confirmación previa (decisión del usuario, con
tope y reglas fijas). Interruptor: `FLIP_MODE` = `on` (ejecuta), `shadow` (solo cuenta lo que
haría, no toca nada) o cualquier otra cosa / vacío (apagado, el valor por defecto).

Ciclo de un flip, con el estado en el `Store` (sobrevive entre vigilancias):
  1. `_buy` (una vez al día): puja por los mejores candidatos de inversión -> `flip_pending:<anuncio>`.
  2. `_resolve_pending` (cada vigilancia): tras el cierre del mercado (21:00) la puja se ha
     ganado (el jugador aparece en tu plantilla -> `flip_held:<jugador>`) o se ha perdido.
  3. `_list_held` (cada vigilancia): pone a la venta lo ganado a valor de mercado.
  4. Aceptar ofertas: PENDIENTE. Nunca se ha visto una oferta real, y aceptar es irreversible;
     ver CLAUDE.md antes de tocarlo. Mientras tanto `watch_offers` (solo lectura) te manda cada
     oferta nueva con su JSON crudo y lo que diría la regla, y las decides tú.
  5. Libro de resultados y freno (`flip_result:*`, `breaker_reason`): cada flip que sale de tu
     plantilla se anota con lo que costó y lo que se cobró; si hay 3 seguidos en pérdida o
     más de `FLIP_MAX_LOSS_M` (5M por defecto) perdidos en 14 días, el flipeo deja de COMPRAR
     hasta que cambies el valor de la variable `FLIP_RESUME`.

Solo se toca lo que el bot compró: nunca vende un jugador de tu once por su cuenta."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

from . import analysis, models, notify, service
from .analysis import b, esc, i
from .api import FantasyAPI
from .config import Settings

CAP_PCT = 0.25  # dinero comprometido en flipeos a la vez, sobre (saldo + coste de lo ya comprado)
MAX_OPEN = 4  # pujas pendientes + jugadores sin vender a la vez
MAX_NEW_PER_RUN = 3
MAX_OVERPAY = 1.03  # nunca pujar más de un 3% por encima del valor de mercado
LIST_RETRIES = 5
EXPIRY_GRACE = timedelta(minutes=20)  # margen tras el cierre antes de dar una puja por perdida
MAX_LOSS_DEFAULT_M = 5.0  # millones perdidos en 14 días a partir de los que se frena la compra
LOSS_WINDOW = timedelta(days=14)
STREAK = 3  # flips seguidos en pérdida que frenan la compra


def breaker_reason(results: list[dict], now: datetime, max_loss: int) -> str | None:
    """Por qué frenar la compra, o None. `results`: flips cerrados (`profit`, `at`) en orden
    cronológico, ya sin los anteriores al último "reanudar". Se frena con `STREAK` seguidos en
    pérdida o con más de `max_loss` perdido en los últimos 14 días."""
    if len(results) >= STREAK and all(r["profit"] < 0 for r in results[-STREAK:]):
        return f"{STREAK} flips seguidos en pérdida"
    recent = sum(r["profit"] for r in results if now - datetime.fromisoformat(r["at"]) <= LOSS_WINDOW)
    if recent < -max_loss:
        return f"{service.m(-recent)} perdidos en 14 días (tope {service.m(max_loss)})"
    return None


def flip_amount(item: models.MarketItem, trend: analysis.Trend) -> int | None:
    """Cuánto pujar por este anuncio, o None si no merece un flip: sigue subiendo AHORA (hoy
    al menos +0.5% y no menos de la mitad del ritmo diario de los últimos 3 días: una subida
    que se frena no vale) y la racha no se está enfriando; se puja el mínimo válido o, si la
    subida lo justifica, la puja con margen (`analysis.bid_plan`) — pero nunca por encima del
    103% de su valor de mercado: se revende a ofertas del juego de ±5% sobre ese valor, pagar
    más se come el margen. (Pablo García, 2026-09-21: subió +41% en 9 días pero hoy +0.18% con
    una media de 1.5%/día: con "d1 > 0" a secas pasó el filtro y se pujó un 2.2% por encima de
    su valor sobre una proyección inflada.)"""
    if trend.d1 < 0.5 or trend.d1 < trend.d3 / 6 or trend.cooling:
        return None
    mv = item.player.market_value
    plan = analysis.bid_plan(service.bid_amount(item), mv, trend, item.player.avg_points, 0.0, False)
    amount = plan.margin or plan.minimum
    if mv and amount > mv * MAX_OVERPAY:
        return None
    return amount


def plan_bids(
    picks: list[tuple[models.MarketItem, analysis.Trend]],
    cash: int,
    held_cost: int,
    pending_amounts: list[int],
    busy_players: set[str],
    open_count: int,
) -> list[tuple[models.MarketItem, int, analysis.Trend]]:
    """Qué pujar hoy, en orden de preferencia, respetando el dinero: lo comprometido (pujas
    pendientes + coste de lo ya comprado) nunca pasa del 25% de (saldo + coste de lo comprado);
    ninguna puja pasa de la mitad de ese tope (así caben al menos dos flips a la vez); y lo
    pujado en total nunca supera tu saldo (regla del usuario: prohibido quedarse en negativo,
    las pujas no bajan el saldo hasta que se resuelven, pero podrían ganarse todas)."""
    pending_cost = sum(pending_amounts)
    cap = (cash + held_cost) * CAP_PCT
    room = cap - held_cost - pending_cost
    cash_free = cash - pending_cost
    plan = []
    for item, trend in picks:
        if len(plan) >= MAX_NEW_PER_RUN or open_count + len(plan) >= MAX_OPEN:
            break
        if item.player.id in busy_players or not item.listing_id or item.my_bid_id:
            continue  # ya hay una puja tuya (la API la marca en el anuncio): no se toca
        amount = flip_amount(item, trend)
        if amount is None or amount > room or amount > cap / 2 or amount > cash_free:
            continue
        plan.append((item, amount, trend))
        room -= amount
        cash_free -= amount
    return plan


def _load(store, prefix: str) -> dict[str, dict]:
    return {k: json.loads(v) for k, v in store.prefixed(prefix).items()}


def _resolve_pending(world: service.World, store, s: Settings, now: datetime) -> list[str]:
    my_ids = {sl.player.id for sl in world.my_slots}
    live = {it.listing_id for it in world.market}
    out = []
    for lid, p in _load(store, "flip_pending:").items():
        name = b(p["name"])
        if p["player_id"] in my_ids:
            price = int(store.get(f"buy_price:{p['player_id']}") or p["amount"])
            held = {"name": p["name"], "buy_price": price, "bought_at": now.isoformat(), "listed": False, "tries": 0}
            store.set(f"flip_held:{p['player_id']}", json.dumps(held))
            store.set(f"flip_pending:{lid}", "")
            notify.send_telegram(s, f"🤖 {b('Flip ganado')}\n{name} por {service.m(price)}")
            out.append(f"ganado {p['name']}")
        elif lid not in live and now > datetime.fromisoformat(p["expires"]) + EXPIRY_GRACE:
            store.set(f"flip_pending:{lid}", "")
            notify.send_telegram(s, f"🤖 {b('Flip perdido')}\n{name}: la puja de {service.m(p['amount'])} no ganó.")
            out.append(f"perdido {p['name']}")
    return out


def _closed_result(api: FantasyAPI, world: service.World, pid: str, h: dict, now: datetime) -> dict | None:
    """Lo que se cobró por un flip que ya no está en tu plantilla: el último movimiento de
    venta a LaLiga (tipo 33) o de cláusula cobrada (tipo 1, tú como vendedor) de ese jugador."""
    my_id = next((r.manager_id for r in world.standing if r.team_id == world.my_team_id), None)
    try:
        events = models.parse_activity(api.activity(world.league_id, 0))
    except Exception:
        return None
    exits = [
        e for e in events
        if e.player_id == pid and (
            (e.type_id == models.ACTIVITY_SELL and e.user1_id == my_id)
            or (e.type_id == models.ACTIVITY_CLAUSE and e.user2_id == my_id)
        )
    ]
    if not exits:
        return None
    last = max(exits, key=lambda e: models.to_int(e.id))
    return {
        "name": h["name"], "buy": h["buy_price"], "sell": last.amount, "profit": last.amount - h["buy_price"],
        "days": (now - datetime.fromisoformat(h["bought_at"])).days, "at": now.isoformat(),
        "via": "te lo clausularon por" if last.type_id == models.ACTIVITY_CLAUSE else "vendido por",
    }


def _results(store) -> list[dict]:
    """Flips cerrados desde el último "reanudar", en orden cronológico."""
    baseline = store.get("flip_baseline") or ""
    rows = [json.loads(v) for v in store.prefixed("flip_result:").values()]
    return sorted((r for r in rows if r["at"] > baseline), key=lambda r: r["at"])


def _list_held(api: FantasyAPI, world: service.World, store, s: Settings, now: datetime) -> list[str]:
    listed_now = {it.player.id for it in world.market}
    today = now.strftime("%Y-%m-%d")
    out = []
    for pid, h in _load(store, "flip_held:").items():
        slot = next((sl for sl in world.my_slots if sl.player.id == pid), None)
        if slot is None:
            store.set(f"flip_held:{pid}", "")
            result = _closed_result(api, world, pid, h, now)
            if result:
                store.set(f"flip_result:{now.isoformat()}:{pid}", json.dumps(result))
                pct = result["profit"] / result["buy"] * 100 if result["buy"] else 0
                msg = (
                    f"🤖 {b('Flip cerrado')}\n{b(h['name'])}: comprado {service.m(result['buy'])}, "
                    f"{result['via']} {service.m(result['sell'])} en {result['days']} días\n"
                    f"{b('Resultado: ' + service.m(result['profit']))} ({pct:+.1f}%)"
                )
            else:
                msg = (
                    f"🤖 {b('Flip cerrado')}\n{b(h['name'])} ya no está en tu plantilla; no encuentro el "
                    f"movimiento en el historial reciente para calcular el resultado."
                )
            notify.send_telegram(s, msg)
            out.append(f"cerrado {h['name']}")
            continue
        if h["listed"]:
            continue
        if pid in listed_now:
            h["listed"] = True
            store.set(f"flip_held:{pid}", json.dumps(h))
            continue
        price = slot.player.market_value
        if not price or not slot.player_team_id or h.get("last_try") == today or h["tries"] >= LIST_RETRIES:
            continue
        h["tries"] += 1
        h["last_try"] = today
        try:
            api.list_for_sale(world.league_id, slot.player_team_id, price)
            h["listed"] = True
            msg = f"🤖 {b('Flip a la venta')}\n{b(h['name'])} por {service.m(price)} (lo pagaste {service.m(h['buy_price'])})"
            out.append(f"a la venta {h['name']}")
        except Exception as exc:
            msg = f"🤖 {b('No pude poner a la venta')} a {b(h['name'])}\n{esc(str(exc))}"
            if h["tries"] >= LIST_RETRIES:
                msg += "\nDejo de intentarlo: ponlo tú a la venta."
            out.append(f"fallo venta {h['name']}")
        store.set(f"flip_held:{pid}", json.dumps(h))
        notify.send_telegram(s, msg)
    return out


def _buy(api: FantasyAPI, world: service.World, store, s: Settings, now: datetime) -> list[str]:
    if world.my_cash is None:
        return []
    held, pending = _load(store, "flip_held:"), _load(store, "flip_pending:")
    held_cost = sum(h["buy_price"] for h in held.values())
    # Lo que se ofrece como fichaje para tu once lo decides tú a mano (y no hay forma de ver
    # las pujas propias en la API): el flipeo no puja por esos, para no comprar y luego
    # revender por su cuenta a alguien que querías conservar. El filtro vive en
    # `service._investment_picks` (así el flipeo real y los informes de "Inversión" siempre
    # coinciden), aquí solo se piden los picks ya filtrados.
    plan = plan_bids(
        service._investment_picks(world, top=10),
        world.my_cash,
        held_cost,
        [p["amount"] for p in pending.values()],
        set(held) | {p["player_id"] for p in pending.values()},
        len(held) + len(pending),
    )
    shadow = s.flip_mode == "shadow"
    cap = (world.my_cash + held_cost) * CAP_PCT
    committed = held_cost + sum(p["amount"] for p in pending.values())
    out = []
    if not plan:
        # Sin esto no se distingue "el bot no funciona" de "hoy no había nada que comprar".
        tag = "🕶️ (sombra) " if shadow else ""
        notify.send_telegram(
            s, f"🤖 {tag}{b('Flipeo')}\nHoy no hay candidatos que cumplan las reglas: que suban hoy, no se enfríen, "
               f"no cuesten más del 103% de su valor y quepan en el tope ({service.m(round(cap))}).",
        )
        return ["sin candidatos"]
    for item, amount, trend in plan:
        p = item.player
        detail = f"{b(p.name)} por {service.m(amount)}\n{i(analysis.trend_words(trend))}"
        if shadow:
            notify.send_telegram(s, f"🕶️ {b('Flip (sombra)')}\nPujaría por {detail}")
            out.append(f"sombra {p.name}")
            continue
        try:
            response = api.bid(world.league_id, item.listing_id, amount)
        except Exception as exc:
            notify.send_telegram(s, f"🤖 {b('No pude pujar')} por {b(p.name)}\n{esc(str(exc))}")
            out.append(f"fallo puja {p.name}")
            continue
        record = {
            "player_id": p.id, "name": p.name, "amount": amount, "placed_at": now.isoformat(),
            "expires": (item.expires or now).isoformat(),
            # Sin el id de la puja no se puede modificar después (PUT .../bid/{id}); la API no
            # tiene ninguna ruta para listar las pujas propias.
            "bid_id": str(response.get("id", "")) if isinstance(response, dict) else "",
        }
        store.set(f"flip_pending:{item.listing_id}", json.dumps(record))
        committed += amount
        notify.send_telegram(
            s, f"🤖 {b('Flip: puja enviada')}\n{detail}\n"
               f"{i(f'Comprometido en flipeos: {service.m(committed)} de un tope de {service.m(round(cap))}')}",
        )
        out.append(f"puja {p.name}")
    return out


def run(
    api: FantasyAPI, world: service.World, store, s: Settings, buy_now: bool, today: str, force: bool = False,
) -> str:
    """Un paso del flipeo dentro de la vigilancia. Devuelve un resumen corto para el log ("" si
    no hay nada que hacer o está apagado). `buy_now`: solo una vez al día, cuando ya se han
    calculado las tendencias del mercado. `force`: salta el "una vez al día" (para probarlo a
    mano desde GitHub con FLIP_FORCE_BUY=1); las demás reglas y el tope siguen valiendo."""
    if s.flip_mode not in ("on", "shadow"):
        return ""
    now = datetime.now(timezone.utc)
    notes: list[str] = []
    if s.flip_mode == "on":
        notes += _resolve_pending(world, store, s, now)
        notes += _list_held(api, world, store, s, now)
        notes += _apply_breaker(store, s, now)
    if buy_now and (force or store.get("flip_last_buy") != today):
        if store.get("flip_paused"):
            notes.append("compra en pausa")
        else:
            notes += _buy(api, world, store, s, now)
        store.set("flip_last_buy", today)
    return " · ".join(notes)


def _apply_breaker(store, s: Settings, now: datetime) -> list[str]:
    """Frena la compra si los resultados lo piden, y la reanuda cuando cambias `FLIP_RESUME`
    (el valor visto antes de frenar no cuenta: hace falta un valor NUEVO)."""
    token = os.environ.get("FLIP_RESUME") or ""
    paused = store.get("flip_paused")
    if paused:
        if token and token != store.get("flip_resume_seen"):
            store.set("flip_paused", "")
            store.set("flip_baseline", now.isoformat())
            store.set("flip_resume_seen", token)
            notify.send_telegram(s, f"🤖 {b('Flipeo reanudado')}\nEmpiezo la cuenta de resultados desde cero.")
            return ["reanudado"]
        return []
    if token:
        store.set("flip_resume_seen", token)
    max_loss = round(float(os.environ.get("FLIP_MAX_LOSS_M") or MAX_LOSS_DEFAULT_M) * 1_000_000)
    reason = breaker_reason(_results(store), now, max_loss)
    if not reason:
        return []
    store.set("flip_paused", reason)
    notify.send_telegram(
        s, f"⛔ {b('Flipeo en pausa')}\n{esc(reason)}. No compro nada más hasta que lo reanudes: cambia el valor "
           f"de la variable FLIP_RESUME en GitHub (cualquier texto distinto al anterior). Lo que ya tengo "
           f"comprado sigue su curso.",
    )
    return [f"pausa: {reason}"]


def watch_offers(api: FantasyAPI, world: service.World, store, s: Settings) -> list[str]:
    """Cada oferta nueva de la liga por un jugador tuyo puesto a la venta se manda por Telegram
    con el importe, lo que diría `analysis.flip_decision` si se reconoce, y botones de
    "✅ Aceptar" / "❌ Rechazar" (con la confirmación de dos pasos del Worker de Telegram: nunca
    se ejecuta nada desde aquí, solo se arma el botón). Sin `id` de oferta reconocido no hay
    botón de aceptar/rechazar (no hay con qué llamar a la API). El JSON crudo ya no se manda
    (formato verificado el 2026-09-22, ver CLAUDE.md); si hace falta volver a mirarlo, está en
    el log de `execute-action`/`tick` en GitHub Actions."""
    listed = {it.player.id for it in service._my_listings(world)}
    held = _load(store, "flip_held:")
    out = []
    for slot in world.my_slots:
        if slot.player.id not in listed or not slot.player_team_id:
            continue
        try:
            payload = api.player_offers(world.league_id, slot.player_team_id)
        except Exception as exc:
            print(f"[ofertas] {slot.player.name}: {exc}")
            continue
        offers = models.as_list(payload, "offers", "elements", "items")
        if not offers and isinstance(payload, dict) and payload and not any(isinstance(v, list) for v in payload.values()):
            offers = [payload]
        for offer in offers:
            raw = json.dumps(offer, ensure_ascii=False, sort_keys=True)
            key = "offer_seen:" + hashlib.sha1(raw.encode()).hexdigest()[:16]
            if store.get(key):
                continue
            store.set(key, "1")
            print(f"[ofertas] {slot.player.name}: {raw}")
            amount = models.to_int(models.pick(offer, "money", "offerMoney", "amount", "price", "salePrice", default=0))
            offer_id = str(models.pick(offer, "id", "offerId", default="") or "")
            lines = [f"📨 {b('Oferta recibida')} por {b(slot.player.name)}"]
            if amount:
                lines.append(f"Te ofrecen: {b(service.m(amount))}")
                buy = int(store.get(f"buy_price:{slot.player.id}") or 0)
                if buy:
                    gain = amount - buy
                    pct = gain / buy * 100
                    lines.append(f"Lo pagaste: {service.m(buy)} · {b('Beneficio neto: ' + service.m(gain))} ({pct:+.0f}%)")
                    held_at = held.get(slot.player.id, {}).get("bought_at")
                    days = (datetime.now(timezone.utc) - datetime.fromisoformat(held_at)).days if held_at else 0
                    decision, why = analysis.flip_decision(buy, amount, days, None)
                    verb = "aceptar" if decision == "accept" else "esperar"
                    lines.append(f"Mi regla: {b(verb)} — {esc(why)}")
                else:
                    lines.append(i(
                        "No tengo el precio de compra de este jugador (estaba en tu plantilla antes de "
                        "que el bot empezara a seguir compras): no puedo calcular el beneficio.",
                    ))
            keyboard = None
            if offer_id.isdigit() and amount:
                keyboard = service._keyboard([
                    service._action_row(f"✅ Aceptar {service.m(amount)}", f"o:{slot.player.id}:{offer_id}:{amount}"),
                    service._action_row("❌ Rechazar", f"r:{slot.player.id}:{offer_id}"),
                ])
            notify.send_telegram(s, "\n".join(lines), buttons=keyboard)
            out.append(f"oferta {slot.player.name}")
    return out
