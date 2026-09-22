"""Línea de comandos: `python -m fantasy_agent <comando>`."""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from . import analysis, auth, clause_snipe, flip, models, notify, service, snipe
from .api import FantasyAPI
from .attendance import estimate_titularidad
from .config import load_settings
from .storage import Store


def _madrid_now() -> datetime:
    """Hora de España ahora (ver `analysis.to_madrid`). Necesario para que las horas de juego
    (informe diario, estudio de mercado) signifiquen lo mismo en local y en el runner de GitHub
    Actions, que va en UTC."""
    return analysis.to_madrid(datetime.now(timezone.utc))


def _out(settings, text: str, telegram: bool, buttons: dict | None = None) -> None:
    print(text)
    if telegram:
        notify.send_telegram(settings, text, buttons=buttons)


def cmd_auth(args, s) -> None:
    if args.step == "url":
        print("1) Abre Chrome → DevTools (F12) → pestaña Network → marca 'Preserve log'.")
        print("2) Pega esta URL e inicia sesión con tu cuenta de LaLiga Fantasy:\n")
        print(auth.build_login_url(s))
        print("\n3) La página se quedará en blanco (es normal). En Network, la última fila '(canceled)'")
        print("   empieza por ?state=...&code=... → clic derecho → Copy link address.")
        print("4) Ejecuta: python -m fantasy_agent auth code 'authredirect://...'")
    elif args.step == "code":
        if not args.url:
            sys.exit("Falta la URL: auth code 'authredirect://...'")
        tokens = auth.exchange_code(s, args.url)
        mins = int((tokens["expires_at"] - time.time()) / 60)
        print(f"✅ Sesión guardada. Caduca en {mins} min · refresh: {'sí' if tokens['refresh_token'] else 'no'}")
    elif args.step == "status":
        t = auth.load_tokens(s)
        if not t:
            print("Sin sesión.")
        else:
            mins = int((t["expires_at"] - time.time()) / 60)
            print(f"Token caduca en {mins} min · refresh token: {'sí' if t.get('refresh_token') else 'no'}")
    elif args.step == "refresh":
        auth.refresh(s)
        print("✅ Token renovado.")


def cmd_probe(args, s) -> None:
    api = FantasyAPI(s)
    data = api.get(args.path, authed=not args.public)
    print(json.dumps(data, indent=2, ensure_ascii=False)[: args.max_chars])


def cmd_leagues(args, s) -> None:
    api = FantasyAPI(s)
    league_id, team_id, _ = service.resolve_league(api, s)
    print(json.dumps(api.leagues(), indent=2, ensure_ascii=False)[:3000])
    print(f"\nLiga seleccionada: {league_id} · equipo detectado: {team_id or '(no viene en la respuesta)'}")


def cmd_standing(args, s) -> None:
    api = FantasyAPI(s)
    league_id, _, _ = service.resolve_league(api, s)
    from .models import parse_standing
    for r in parse_standing(api.standing(league_id)):
        print(f"team_id={r.team_id:<10} manager_id={r.manager_id:<12} {r.manager_name:<20} {r.points:>5} pts  {service.m(r.team_value)}")


def _world(api, s, trends=True):
    return service.build_world(api, s, with_trends=trends)


def cmd_bid(args, s) -> None:
    """PRUEBA CONTROLADA del endpoint de puja (sin verificar todavía contra una cuenta real).
    Sin --confirm solo enseña qué haría, no gasta nada. Con --confirm, ejecuta de verdad."""
    api = FantasyAPI(s)
    league_id, _, _ = service.resolve_league(api, s)
    market = models.parse_market(api.market(league_id))
    item = next((i for i in market if i.listing_id == args.listing_id), None)
    if not item:
        sys.exit(f"No encuentro el anuncio {args.listing_id} en el mercado ahora mismo.")
    amount = args.amount or service.bid_amount(item)
    print(f"Jugador: {item.player.name} ({item.player.position} · {item.player.team})")
    print(f"Anuncio: {item.listing_id} · vendedor: {item.seller} · precio pedido: {service.m(item.price)}"
          f" · valor de mercado: {service.m(item.player.market_value)}")
    print(f"Pujarías: {service.m(amount)}")
    if not args.confirm:
        print("\n(vista previa — no se ha pujado nada. Repite con --confirm para pujar de verdad)")
        return
    print("\n⚠️  Pujando de verdad...")
    result = api.bid(league_id, item.listing_id, amount)
    print("Respuesta del servidor:")
    print(json.dumps(result, indent=2, ensure_ascii=False)[:2000])


def cmd_clause(args, s) -> None:
    """PRUEBA CONTROLADA del endpoint de cláusula. Sin --confirm solo enseña qué haría.
    Usa `player_team_id` (el hueco de plantilla, no el id genérico del jugador) y relee la
    plantilla justo antes de pagar, sin caché, para minimizar la ventana de desactualización."""
    api = FantasyAPI(s)
    world = service.build_world(api, s, with_trends=False)
    slot = next((sl for sl in world.rival_slots if sl.player.id == args.player_id), None)
    if not slot:
        sys.exit(f"No encuentro a ningún rival con el jugador {args.player_id}.")
    now = datetime.now(timezone.utc)
    print(f"Jugador: {slot.player.name} ({slot.player.position} · {slot.player.team})")
    print(f"Dueño actual: {slot.owner_name}")
    print(f"Cláusula: {service.m(slot.clause)} · pagable ahora: {'sí' if slot.clause_open(now) else 'no'}")
    print(f"Pagarías: {service.m(args.amount or slot.clause)}")
    if not args.confirm:
        print("\n(vista previa — no se ha pagado nada. Repite con --confirm para pagar de verdad)")
        return

    api.clear_cache()
    fresh = models.parse_squad(api.team(world.league_id, slot.owner_team_id), slot.owner_team_id, slot.owner_name)
    fresh_slot = next((sl for sl in fresh if sl.player.id == args.player_id), None)
    if not fresh_slot:
        sys.exit("El jugador ya no está en esa plantilla (puede que ya se lo hayan clausulado).")
    amount = args.amount or fresh_slot.clause
    print(f"\n⚠️  Pagando la cláusula de verdad... (playerTeamId={fresh_slot.player_team_id}, importe={service.m(amount)})")
    result = api.pay_clause(world.league_id, fresh_slot.player_team_id, amount)
    print("Respuesta del servidor:")
    print(json.dumps(result, indent=2, ensure_ascii=False)[:2000])


def cmd_ceiling(args, s) -> None:
    """Lectura pura: techo de puja recomendado para un jugador (para tu once, no flipeo)."""
    api = FantasyAPI(s)
    world = service.build_world(api, s, with_trends=False)
    player = service.find_player(api, world, args.player_id)
    if not player:
        sys.exit(f"No encuentro a ningún jugador con id {args.player_id}.")
    text = service.bid_ceiling_report(world, player)
    _out(s, text, args.telegram)


def cmd_offers(args, s) -> None:
    """Lectura pura: ofertas pendientes sobre un jugador tuyo (solo si lo has puesto a la
    venta). Sin riesgo, no gasta nada — para ver la forma real de una oferta por primera vez."""
    api = FantasyAPI(s)
    world = service.build_world(api, s, with_trends=False)
    slot = next((sl for sl in world.my_slots if sl.player.id == args.player_id), None)
    if not slot:
        sys.exit(f"Ese jugador no está en tu plantilla ({args.player_id}).")
    result = api.player_offers(world.league_id, slot.player_team_id)
    print(f"Ofertas para {slot.player.name} (playerTeamId={slot.player_team_id}):")
    print(json.dumps(result, indent=2, ensure_ascii=False)[:3000])


def cmd_sell(args, s) -> None:
    """PRUEBA CONTROLADA: poner un jugador tuyo a la venta. Sin --confirm solo enseña qué
    haría. Al confirmar, el jugador queda a la venta (no se cobra nada al instante; el juego
    empezará a mandar ofertas en los próximos ciclos de mercado — usa `offers` para verlas)."""
    api = FantasyAPI(s)
    world = service.build_world(api, s, with_trends=False)
    slot = next((sl for sl in world.my_slots if sl.player.id == args.player_id), None)
    if not slot:
        sys.exit(f"Ese jugador no está en tu plantilla ({args.player_id}).")
    price = args.price or slot.player.market_value
    print(f"Jugador: {slot.player.name} ({slot.player.position} · {slot.player.team})")
    print(f"Valor de mercado: {service.m(slot.player.market_value)}")
    print(f"Precio de venta: {service.m(price)}")
    if not args.confirm:
        print("\n(vista previa — no se ha puesto a la venta. Repite con --confirm para hacerlo de verdad)")
        return
    print("\n⚠️  Poniendo a la venta de verdad...")
    result = api.list_for_sale(world.league_id, slot.player_team_id, price)
    print("Respuesta del servidor:")
    print(json.dumps(result, indent=2, ensure_ascii=False)[:2000])


def cmd_withdraw(args, s) -> None:
    """PRUEBA CONTROLADA: retirar un jugador tuyo del mercado (deja de estar a la venta).
    Sin --confirm solo enseña qué haría."""
    api = FantasyAPI(s)
    league_id, _, _ = service.resolve_league(api, s)
    market = models.parse_market(api.market(league_id))
    item = next((i for i in market if i.player.id == args.player_id), None)
    if not item:
        sys.exit(f"No encuentro ningún anuncio tuyo para el jugador {args.player_id} en el mercado ahora mismo.")
    print(f"Jugador: {item.player.name} ({item.player.position} · {item.player.team})")
    print(f"Anuncio: {item.listing_id} · vendedor: {item.seller} · precio: {service.m(item.price)}")
    if not args.confirm:
        print("\n(vista previa — no se ha retirado nada. Repite con --confirm para retirarlo de verdad)")
        return
    print("\n⚠️  Retirando del mercado de verdad...")
    result = api.withdraw_from_market(league_id, item.listing_id)
    print("Respuesta del servidor:")
    print(json.dumps(result, indent=2, ensure_ascii=False)[:2000] if result else "(sin cuerpo de respuesta)")


def _act_clause(api, s, player_id: str) -> str:
    world = service.build_world(api, s, with_trends=False)
    slot = next((sl for sl in world.rival_slots if sl.player.id == player_id), None)
    if not slot:
        raise RuntimeError("Ese jugador ya no está disponible para clausular (puede que ya se lo hayan llevado).")
    api.clear_cache()
    fresh = models.parse_squad(api.team(world.league_id, slot.owner_team_id), slot.owner_team_id, slot.owner_name)
    fresh_slot = next((sl for sl in fresh if sl.player.id == player_id), None)
    if not fresh_slot:
        raise RuntimeError("Ese jugador ya no está en esa plantilla (puede que ya se lo hayan clausulado).")
    api.pay_clause(world.league_id, fresh_slot.player_team_id, fresh_slot.clause)
    return f"✅ {service.b('Cláusula pagada')}\n{service.b(fresh_slot.player.name)} por {service.m(fresh_slot.clause)}"


def _act_bid(api, s, target: str) -> str:
    """Puja la cantidad del botón ("<anuncio>:<cantidad>"; sin cantidad, el mínimo válido de
    `service.bid_amount`). Nunca por debajo del mínimo (el servidor lo rechaza), ni por encima
    del saldo (regla del usuario: prohibido quedarse en negativo), ni una cantidad absurda."""
    listing_id, _, raw_amount = target.partition(":")
    league_id, _, cash = service.resolve_league(api, s)
    item = next((x for x in models.parse_market(api.market(league_id)) if x.listing_id == listing_id), None)
    if not item:
        raise RuntimeError("Ese anuncio ya no está en el mercado.")
    if item.seller != "LaLiga":
        raise RuntimeError("Solo se puede pujar por anuncios de LaLiga.")
    minimum = service.bid_amount(item)
    amount = int(raw_amount) if raw_amount else minimum
    if amount < minimum:
        raise RuntimeError(f"{service.m(amount)} queda por debajo del mínimo válido ({service.m(minimum)}).")
    if amount > minimum * 3:
        raise RuntimeError(f"{service.m(amount)} es una cantidad sospechosa (más de 3x el mínimo); no pujo.")
    if cash is not None and amount > cash:
        raise RuntimeError(f"No te llega el saldo ({service.m(cash)}) para pujar {service.m(amount)}.")
    response = api.bid(league_id, item.listing_id, amount)
    bid_id = str(response.get("id", "")) if isinstance(response, dict) else ""
    return (
        f"✅ {service.b('Puja enviada')}\n{service.b(item.player.name)} por {service.m(amount)}\n"
        f"{service.i('Queda pendiente: se resuelve al cierre del mercado, el saldo no baja ya.')}"
        + (f"\n{service.i(f'id de la puja: {bid_id} (anúnciala si quieres poder cambiarla luego)')}" if bid_id else "")
    )


def _act_sell(api, s, player_id: str) -> str:
    world = service.build_world(api, s, with_trends=False)
    slot = next((sl for sl in world.my_slots if sl.player.id == player_id), None)
    if not slot:
        raise RuntimeError("Ese jugador ya no está en tu plantilla.")
    price = slot.player.market_value
    if not price or not slot.player_team_id:
        raise RuntimeError("No tengo precio o id de plantilla para ponerlo a la venta.")
    api.list_for_sale(world.league_id, slot.player_team_id, price)
    return (
        f"✅ {service.b('A la venta')}\n{service.b(slot.player.name)} por {service.m(price)}\n"
        f"{service.i('El juego manda ofertas en el ciclo de las 21:00; las revisas y decides tú.')}"
    )


def _act_withdraw(api, s, player_id: str) -> str:
    world = service.build_world(api, s, with_trends=False)
    mine = {sl.player.id for sl in world.my_slots}
    item = next((x for x in world.market if x.player.id == player_id and player_id in mine), None)
    if not item:
        raise RuntimeError("Ese jugador ya no está en venta.")
    api.withdraw_from_market(world.league_id, item.listing_id)
    return f"✅ {service.b('Retirado del mercado')}\n{service.b(item.player.name)} ya no está a la venta."


def _act_update_bid(api, s, target: str) -> str:
    """Cambia la cantidad de una puja pendiente ("<anuncio>:<puja>:<cantidad>"). Comprueba antes
    que sigue siendo TU puja pendiente en ese anuncio (el id viaja en el botón, pero pudo
    resolverse o cambiarse desde que se mandó) y aplica los mismos topes que `_act_bid`."""
    listing_id, bid_id, raw_amount = (target.split(":") + ["", "", ""])[:3]
    league_id, _, cash = service.resolve_league(api, s)
    item = next((x for x in models.parse_market(api.market(league_id)) if x.listing_id == listing_id), None)
    if not item:
        raise RuntimeError("Ese anuncio ya no está en el mercado.")
    if item.my_bid_id != bid_id:
        raise RuntimeError("Esa puja ya no está pendiente (se resolvió o la cambiaste desde otro sitio).")
    minimum = service.bid_amount(item)
    amount = int(raw_amount)
    if amount < minimum:
        raise RuntimeError(f"{service.m(amount)} queda por debajo del mínimo válido ({service.m(minimum)}).")
    if amount > minimum * 3:
        raise RuntimeError(f"{service.m(amount)} es una cantidad sospechosa (más de 3x el mínimo); no la cambio.")
    if cash is not None and amount > cash:
        raise RuntimeError(f"No te llega el saldo ({service.m(cash)}) para pujar {service.m(amount)}.")
    api.update_bid(league_id, item.listing_id, bid_id, amount)
    return (
        f"✅ {service.b('Puja cambiada')}\n{service.b(item.player.name)}: de {service.m(item.my_bid)} "
        f"a {service.m(amount)}\n{service.i('Sigue pendiente hasta el cierre del mercado.')}"
    )


ELEVEN_CHECK_WINDOW = timedelta(hours=48)  # por debajo de esto ya no da tiempo a fichar reemplazo


def _act_accept_offer(api, s, target: str) -> str:
    """Acepta una oferta ("<jugador>:<oferta>:<importe>"). Antes de llamar a la API comprueba
    que el jugador sigue en venta (la oferta pudo resolverse sola, expirar, o ya haberse
    aceptado/rechazado desde otro sitio entre el aviso y la pulsación). Solo si faltan
    `ELEVEN_CHECK_WINDOW` o menos para la próxima jornada comprueba además que la plantilla sin
    él sigue pudiendo alinear un once legal (regla del usuario: con más margen todavía da
    tiempo a fichar un reemplazo, así que no bloquea la venta)."""
    player_id, offer_id, raw_amount = (target.split(":") + ["", "", ""])[:3]
    amount = int(raw_amount)
    world = service.build_world(api, s, with_trends=False)
    slot = next((sl for sl in world.my_slots if sl.player.id == player_id), None)
    if not slot:
        raise RuntimeError("Ese jugador ya no está en tu plantilla.")
    item = next((x for x in service._my_listings(world) if x.player.id == player_id), None)
    if not item:
        raise RuntimeError("Ese jugador ya no está en venta (puede que la oferta ya se resolviera).")
    close_to_jornada = world.next_jornada and world.next_jornada - datetime.now(timezone.utc) <= ELEVEN_CHECK_WINDOW
    if close_to_jornada and not analysis.squad_can_field_eleven(world.my_slots, exclude_player_id=player_id):
        raise RuntimeError(
            "Vender a este jugador te deja sin cuerpos para alinear un once legal y la jornada empieza en "
            "menos de 48h (sin tiempo para fichar reemplazo): no lo acepto.",
        )
    api.accept_offer(world.league_id, item.listing_id, offer_id, amount)
    return f"✅ {service.b('Oferta aceptada')}\n{service.b(slot.player.name)} vendido por {service.m(amount)}"


def _act_reject_offer(api, s, target: str) -> str:
    """Rechaza una oferta ("<jugador>:<oferta>"). El jugador sigue a la venta después."""
    player_id, offer_id = (target.split(":") + ["", ""])[:2]
    world = service.build_world(api, s, with_trends=False)
    slot = next((sl for sl in world.my_slots if sl.player.id == player_id), None)
    item = next((x for x in service._my_listings(world) if x.player.id == player_id), None)
    if not slot or not item:
        raise RuntimeError("Ese jugador ya no está en venta (puede que la oferta ya se resolviera).")
    api.reject_offer(world.league_id, item.listing_id, offer_id)
    return f"✅ {service.b('Oferta rechazada')}\n{service.b(slot.player.name)} sigue a la venta."


_ACTIONS = {
    "c": _act_clause, "b": _act_bid, "s": _act_sell, "w": _act_withdraw, "u": _act_update_bid,
    "o": _act_accept_offer, "r": _act_reject_offer,
}


def cmd_execute_action(args, s) -> None:
    """Entrada interna para el flujo de botones de Telegram (webhook → Cloudflare Worker →
    repository_dispatch → este comando). A diferencia de `bid`/`clause`/`sell`/`withdraw`, NO
    tiene vista previa: solo se llega aquí después de que el Worker ya pidió confirmación con
    un segundo botón ("¿Seguro?"), así que aquí siempre se ejecuta de verdad. No lo lances a
    mano salvo que sepas exactamente qué código de acción estás pasando (ver CLAUDE.md, sección
    de botones, para el formato "<verbo>:<id>")."""
    if not notify.telegram_enabled(s):
        sys.exit("execute-action necesita Telegram configurado (informa del resultado por ahí)")
    verb, _, target = args.action.partition(":")
    api = FantasyAPI(s)
    try:
        act = _ACTIONS.get(verb)
        if act is None or not target:
            raise RuntimeError(f"Acción no reconocida: {args.action!r}")
        notify.send_telegram(s, act(api, s, target))
    except Exception as exc:
        # El fallo ya se entrega por Telegram; salir con error solo mandaría además un correo
        # de "workflow fallido" de GitHub por cada botón que no pudo ejecutarse. El Worker ya
        # quitó el botón de "Confirmar" del mensaje original antes de disparar esto (no sabe si
        # la acción va a fallar), así que sin un botón nuevo aquí no hay forma de reintentar sin
        # pedir el aviso de nuevo: se manda uno igual al original (mismo código de acción).
        print(f"[error] {args.action}: {exc}")
        notify.send_telegram(
            s, f"❌ {service.b('No se pudo ejecutar')}\n{service.esc(str(exc))}",
            buttons=service._keyboard([service._action_row("🔁 Reintentar", args.action)]),
        )


def cmd_flip(args, s) -> None:
    """Prueba del flipeo en MODO SOMBRA (siempre, aunque FLIP_MODE=on): cuenta por Telegram lo
    que pujaría ahora mismo con el mercado real, sin pujar ni guardar nada. El estado real del
    flipeo vive en la caché de GitHub Actions, no en tu SQLite local, así que ejecutarlo en
    real desde aquí duplicaría operaciones."""
    s = replace(s, flip_mode="shadow")
    api = FantasyAPI(s)
    world = _world(api, s, trends=True)
    note = flip.run(api, world, Store(s.db_file), s, buy_now=True, today=datetime.now().isoformat())
    print(note or "Sin candidatos que cumplan las reglas ahora mismo.")


def cmd_snipe(args, s) -> None:
    """Rebaja de último segundo (ver snipe.py). `SNIPE_MODE=on` baja de verdad; por defecto solo
    cuenta lo que haría. `--close-in N` simula un cierre dentro de N segundos, para probarlo."""
    mode = (os.environ.get("SNIPE_MODE") or "shadow").strip().lower()
    snipe.send(FantasyAPI(s), s, mode, close_in=args.close_in)


def cmd_clause_snipe(args, s) -> None:
    """Entrada interna del botón "🎯 Comprar al desbloquearse" (Worker -> repository_dispatch ->
    clause-snipe.yml -> aquí): espera al desbloqueo y paga en ese segundo (ver clause_snipe.py).
    Código de acción "a:<player_id>". Como `execute-action`, no tiene vista previa."""
    if not notify.telegram_enabled(s):
        sys.exit("clause-snipe necesita Telegram configurado (informa del resultado por ahí)")
    verb, player_id, *rest = args.action.split(":")
    if verb != "a" or not player_id.isdigit() or (rest and not rest[0].isdigit()):
        sys.exit(f"Acción no reconocida: {args.action!r}")
    try:
        clause_snipe.run(
            FantasyAPI(s), s, player_id, expected=int(rest[0]) if rest else None, dry=args.dry, unlock_in=args.unlock_in,
        )
    except Exception as exc:
        print(f"[error] {args.action}: {exc}")
        notify.send_telegram(s, f"❌ {service.b('No pude armar/ejecutar la compra')}\n{service.esc(str(exc)[:300])}")


def cmd_simulate_clause(args, s) -> None:
    """Manda por Telegram el SIMULACRO de los mensajes de la compra armada (aviso de "¿la dejas
    comprada por exactamente X?" y aviso de cambio de importe). Datos de mentira, tu saldo real."""
    if not notify.telegram_enabled(s):
        sys.exit("simulate-clause necesita Telegram configurado")
    clause_snipe.simulate(FantasyAPI(s), s)


def cmd_set_webhook(args, s) -> None:
    """Una sola vez tras desplegar el Worker: le dice a Telegram a dónde mandar los botones."""
    print(json.dumps(notify.set_webhook(s, args.url, args.secret), ensure_ascii=False))


def cmd_section(args, s) -> None:
    api = FantasyAPI(s)
    world = _world(
        api, s,
        trends=args.cmd in ("market", "trends", "report", "losses", "sell-candidates", "market-news", "listen", "bids"),
    )
    if args.cmd in ("clauses", "clauses-hot", "report"):
        service.ensure_clause_trends(api, world, s)
        service.ensure_speculative_trends(api, world, s)

    news_targets = []
    if args.cmd in ("lineup", "report") and getattr(args, "news", False):
        news_targets += [sl.player for sl in world.my_slots if sl.player.position_id != 5]
    if args.cmd in ("clauses", "report"):
        news_targets += service.clause_titularidad_candidates(world, s)
    news = estimate_titularidad(api, news_targets) if news_targets else None

    store = Store(s.db_file) if args.cmd in (
        "report", "losses", "sell-candidates", "market-news", "listen", "rivals", "clause-risk", "advice", "market",
    ) else None

    rival_cash = {}
    if args.cmd in ("rivals", "clause-risk", "advice", "market"):
        rival_cash = service.estimate_rival_cash(api, world, store)

    if args.cmd == "report":
        sections = service.report_sections(world, s, news, store, service.estimate_rival_cash(api, world, store))
        print("\n\n".join(text for text, _ in sections))
        if args.telegram:
            notify.send_report(s, sections)
        return
    if args.cmd in ("clauses", "clauses-hot"):
        if args.cmd == "clauses":
            messages, _ = service.clauses_report(world, s, news)
        else:
            messages, _ = service.speculative_clauses_report(world, s)
            if not messages:
                messages = [("Nada especulativo ahora mismo.", None)]
        for msg, buttons in messages:
            _out(s, msg, args.telegram, buttons=buttons)
        return
    def arrivals() -> tuple[str, dict | None]:
        text, buttons = service.market_arrivals_report(world, store)
        return text or "Nada nuevo desde el último estudio.", buttons

    def unlocks() -> tuple[str, dict | None]:
        text, buttons = service.unlocks_report(world)
        return text or "Ninguna cláusula se desbloquea en las próximas 24 h.", buttons

    def bids() -> tuple[str, dict | None]:
        text, buttons = service.my_bids_report(world)
        return text or "No tienes ninguna puja pendiente ahora mismo.", buttons

    def listings() -> tuple[str, dict | None]:
        text, buttons = service.my_listings_report(world)
        return text or "No tienes a nadie a la venta ahora mismo.", buttons

    text, buttons = {
        "market": lambda: (
            service.market_report(world, rival_cash=rival_cash), service.market_keyboard(world, rival_cash=rival_cash),
        ),
        "trends": lambda: (service.trends_report(world), None),
        "rivals": lambda: (service.rivals_report(world, rival_cash), None),
        "clause-risk": lambda: (
            service.clause_theft_report(world, rival_cash) or "Ningún rival te llega ahora mismo.", None,
        ),
        "lineup": lambda: (service.lineup_report(world, news), None),
        "losses": lambda: (service.losing_positions_report(world, store) or "Nada por debajo de lo que pagaste.", None),
        "sell-candidates": lambda: (
            service.sell_candidates_report(world, store) or "Nadie con tendencia bajando ahora mismo.",
            service.sell_keyboard(world, store),
        ),
        "market-news": arrivals,
        "listen": lambda: (
            lambda r: (r[0] or "Ningún jugador tuyo con la tendencia bajando ahora mismo.", r[1])
        )(service.offers_watch_report(world, store)),
        "listings": listings,
        "bids": bids,
        "unlocks": unlocks,
        "advice": lambda: (service.daily_advice_report(world, rival_cash), None),
    }[args.cmd]()
    _out(s, text, args.telegram, buttons=buttons)


def _watch_once(store: Store, s) -> str:
    """Una pasada: alertas de cláusula siempre (con veredicto propio: precio, racha y
    noticias reales de los candidatos), informe completo si toca hoy, estudio de mercado
    justo tras el refresco diario. Hora de España siempre (aunque esto corra en un runner de
    GitHub Actions en UTC), para que REPORT_HOUR/MARKET_STUDY_HOUR signifiquen lo que dicen."""
    now = _madrid_now()
    today = now.strftime("%Y-%m-%d")
    daily_due = now.hour >= s.report_hour and store.get("last_daily") != today
    market_due = (
        (now.hour, now.minute) >= (s.market_study_hour, s.market_study_minute)
        and store.get("last_market_study") != today
    )
    force_flip = os.environ.get("FLIP_FORCE_BUY") == "1"
    api = FantasyAPI(s)
    world = _world(api, s, trends=daily_due or market_due or force_flip)
    service.ensure_clause_trends(api, world, s)
    service.ensure_speculative_trends(api, world, s)

    clause_news = {}
    try:
        clause_news = estimate_titularidad(api, service.clause_titularidad_candidates(world, s))
    except Exception as exc:
        print(f"[titularidad] error cláusulas: {exc}")

    _, alerts = service.clauses_report(world, s, clause_news)
    fresh = [a for a in alerts if store.alert_is_new(a.key)]
    for a in fresh:
        notify.send_telegram(
            s, f"🚨 {a.message}", buttons=service.alert_keyboard(a.slot, a.kind, datetime.now(timezone.utc)),
        )

    _, hot_alerts = service.speculative_clauses_report(world, s)
    fresh_hot = [a for a in hot_alerts if store.alert_is_new(a.key)]
    for a in fresh_hot:
        notify.send_telegram(s, f"📈 {a.message}", buttons=service._clause_keyboard(a.slot.player.id))

    tx = service.my_transactions(api, world, store)
    if tx:
        notify.send_telegram(s, "<b>📒 Movimientos en tu equipo</b>\n\n" + "\n\n".join(tx))

    try:
        audit = service.audit_my_cash(api, world, store)
        if audit:
            notify.send_telegram(s, audit)
    except Exception as exc:
        print(f"[auditoría] error: {exc}")

    try:
        for note in flip.watch_offers(api, world, store, s):
            print(f"[ofertas] {note}")
    except Exception as exc:
        print(f"[ofertas] error: {exc}")

    try:
        for note in clause_snipe.remind_wanted(world, store, s):
            print(f"[seguimiento] {note}")
    except Exception as exc:
        print(f"[seguimiento] error: {exc}")

    flip_note = ""
    try:
        if force_flip and s.flip_mode not in ("on", "shadow"):
            notify.send_telegram(
                s, f"🤖 {service.b('Flipeo apagado')}\nHas pedido forzar una compra pero FLIP_MODE no está en "
                   f"on (ni shadow): créala en Settings > Secrets and variables > Actions > Variables.",
            )
        flip_note = flip.run(api, world, store, s, buy_now=daily_due or force_flip, today=today, force=force_flip)
    except Exception as exc:
        # Un fallo del flipeo no debe tumbar el resto de la vigilancia (cláusulas, informe...).
        print(f"[flip] error: {exc}")
        notify.send_telegram(s, f"🤖 {service.b('Error en el flipeo')}\n{service.esc(str(exc))}")

    if market_due:
        arrivals, arrivals_buttons = service.market_arrivals_report(world, store)
        if arrivals:
            notify.send_telegram(s, arrivals, buttons=arrivals_buttons)
        store.set("last_market_study", today)

    if daily_due:
        news = dict(clause_news)
        try:
            news.update(estimate_titularidad(api, [sl.player for sl in world.my_slots if sl.player.position_id != 5]))
        except Exception as exc:
            print(f"[titularidad] error: {exc}")
        rival_cash = {}
        try:
            rival_cash = service.estimate_rival_cash(api, world, store)
        except Exception as exc:
            print(f"[aviso] sin saldo estimado de rivales: {exc}")
        notify.send_report(s, service.report_sections(world, s, news, store, rival_cash))
        store.set("last_daily", today)
    return (
        f"[{now:%H:%M}] ok · {len(fresh)} alertas nuevas · {len(fresh_hot)} especulativas · {len(tx)} movimientos"
        f"{f' · flip: {flip_note}' if flip_note else ''}"
        f"{' · informe diario enviado' if daily_due else ''}"
        f"{' · estudio de mercado enviado' if market_due else ''}"
    )


def cmd_movements(args, s) -> None:
    """Compras/ventas/clausulas tuyas desde la ultima vez que se miro (marca de agua en sqlite)."""
    api = FantasyAPI(s)
    world = _world(api, s, trends=False)
    store = Store(s.db_file)
    tx = service.my_transactions(api, world, store)
    text = "<b>📒 Movimientos en tu equipo</b>\n\n" + "\n\n".join(tx) if tx else "Sin movimientos nuevos."
    _out(s, text, args.telegram)


def cmd_tick(args, s) -> None:
    """Una sola pasada de vigilancia (pensado para cron / GitHub Actions)."""
    if not notify.telegram_enabled(s):
        sys.exit("tick necesita Telegram configurado")
    store = Store(s.db_file)
    try:
        print(_watch_once(store, s))
    except Exception as exc:
        # Sin esto un fallo (sesión caducada, API caída, un bug) solo se ve como un correo de
        # "workflow fallido" de GitHub. Se avisa por Telegram, como máximo una vez cada 6 h por
        # tipo de error para no inundarte con un fallo que se repite cada 30 minutos.
        if store.alert_is_new(f"health:{type(exc).__name__}", ttl_hours=6):
            hint = ""
            if isinstance(exc, (RuntimeError, KeyError)) and "sesi" in str(exc).lower():
                hint = "\nLa sesión de LaLiga parece caducada: repite el login (auth url / auth code)."
            notify.send_telegram(
                s, f"⚠️ {service.b('El vigilante ha fallado')}\n{service.esc(type(exc).__name__)}: "
                   f"{service.esc(str(exc)[:300])}{hint}",
            )
        raise


def cmd_watch(args, s) -> None:
    """Bucle local: alarmas de cláusula cada X minutos e informe completo una vez al día."""
    if not notify.telegram_enabled(s):
        sys.exit("El modo watch necesita Telegram configurado en el .env")
    store = Store(s.db_file)
    print(f"👀 Vigilando cada ~{s.watch_interval_min} min. Informe diario a las {s.report_hour}:00. Ctrl+C para salir.")
    while True:
        try:
            print(_watch_once(store, s))
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[{datetime.now():%H:%M}] error: {exc}")
        # Intervalo con algo de aleatoriedad para no pegar peticiones a hora fija.
        time.sleep(s.watch_interval_min * 60 * random.uniform(0.85, 1.15))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fantasy", description="Analista de LaLiga Fantasy (solo lectura)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("auth", help="Login y sesión")
    p.add_argument("step", choices=["url", "code", "status", "refresh"])
    p.add_argument("url", nargs="?")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("probe", help="Ver JSON crudo de una ruta de la API")
    p.add_argument("path", help="p.ej. /v1/competition/1/leagues")
    p.add_argument("--public", action="store_true", help="sin token")
    p.add_argument("--max-chars", type=int, default=6000)
    p.set_defaults(func=cmd_probe)

    sub.add_parser("leagues", help="Tus ligas").set_defaults(func=cmd_leagues)
    sub.add_parser("standing", help="Clasificación con ids").set_defaults(func=cmd_standing)

    for name, help_ in [
        ("market", "Oportunidades de mercado"),
        ("trends", "Tus jugadores: cuáles conviene vender ya"),
        ("rivals", "Resumen de rivales"),
        ("clauses", "Alarmas de cláusulas"),
        ("clauses-hot", "Cláusulas especulativas: caras pero con racha fuerte sostenida"),
        ("clause-risk", "Tus jugadores que algún rival podría pagarte de cláusula (saldo estimado)"),
        ("lineup", "Once recomendado"),
        ("losses", "Jugadores tuyos por debajo de lo que pagaste"),
        ("sell-candidates", "Candidatos a vender: tendencia bajando 3 días"),
        ("market-news", "Nuevo en el mercado desde el último estudio, con veredicto"),
        ("listen", "A quién poner a escuchar ofertas (tendencia bajando), con botón de vender"),
        ("listings", "Tus jugadores en venta ahora, con botón para retirarlos"),
        ("bids", "Tus pujas pendientes, con botón para cambiarlas"),
        ("unlocks", "Próximos desbloqueos de cláusula (24 h), con botón de comprar al desbloquearse"),
        ("advice", "Consejo táctico del día"),
        ("report", "Informe completo"),
    ]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--telegram", action="store_true", help="enviar también por Telegram")
        if name in ("lineup", "report"):
            p.add_argument("--news", action="store_true", help="estima titularidad por histórico de jornadas jugadas")
        p.set_defaults(func=cmd_section)

    p = sub.add_parser("movements", help="Compras/ventas/clausulas tuyas desde la ultima vez")
    p.add_argument("--telegram", action="store_true", help="enviar también por Telegram")
    p.set_defaults(func=cmd_movements)

    p = sub.add_parser("bid", help="PRUEBA: pujar por un anuncio del mercado (--confirm para ejecutar de verdad)")
    p.add_argument("listing_id", help="id del anuncio (columna 'listing_id', no el del jugador)")
    p.add_argument("amount", nargs="?", type=int, default=None, help="cantidad a pujar (por defecto, el precio pedido)")
    p.add_argument("--confirm", action="store_true", help="ejecuta de verdad; sin esto solo es vista previa")
    p.set_defaults(func=cmd_bid)

    p = sub.add_parser("clause", help="PRUEBA: pagar la cláusula de un jugador (--confirm para ejecutar de verdad)")
    p.add_argument("player_id", help="id del jugador (columna 'id' en los informes, ej. clauses)")
    p.add_argument("amount", nargs="?", type=int, default=None, help="cantidad a pagar (por defecto, la cláusula actual)")
    p.add_argument("--confirm", action="store_true", help="ejecuta de verdad; sin esto solo es vista previa")
    p.set_defaults(func=cmd_clause)

    p = sub.add_parser("ceiling", help="Techo de puja recomendado para un jugador (para tu once, no flipeo)")
    p.add_argument("player_id", help="id del jugador")
    p.add_argument("--telegram", action="store_true", help="enviar también por Telegram")
    p.set_defaults(func=cmd_ceiling)

    p = sub.add_parser("offers", help="Lectura: ofertas pendientes sobre un jugador tuyo puesto a la venta")
    p.add_argument("player_id", help="id del jugador (el tuyo)")
    p.set_defaults(func=cmd_offers)

    p = sub.add_parser("sell", help="PRUEBA: poner un jugador tuyo a la venta (--confirm para ejecutar de verdad)")
    p.add_argument("player_id", help="id del jugador (el tuyo)")
    p.add_argument("price", nargs="?", type=int, default=None, help="precio de venta (por defecto, su valor de mercado)")
    p.add_argument("--confirm", action="store_true", help="ejecuta de verdad; sin esto solo es vista previa")
    p.set_defaults(func=cmd_sell)

    p = sub.add_parser("withdraw", help="PRUEBA: retirar un jugador tuyo del mercado (--confirm para ejecutar de verdad)")
    p.add_argument("player_id", help="id del jugador (el tuyo, puesto a la venta)")
    p.add_argument("--confirm", action="store_true", help="ejecuta de verdad; sin esto solo es vista previa")
    p.set_defaults(func=cmd_withdraw)

    sub.add_parser(
        "simulate-clause", help="Simulacro por Telegram de los avisos de compra de cláusula armada (no compra nada)",
    ).set_defaults(func=cmd_simulate_clause)

    p = sub.add_parser("clause-snipe", help="Interno: espera al desbloqueo de una cláusula y la paga en ese segundo")
    p.add_argument("action", help='código de acción, p.ej. "a:2206"')
    p.add_argument("--dry", action="store_true", help="solo pruebas: simula el pago (no paga nada)")
    p.add_argument("--unlock-in", type=float, default=None, help="solo pruebas: desbloqueo ficticio en N segundos")
    p.set_defaults(func=cmd_clause_snipe)

    p = sub.add_parser("snipe", help="Rebaja de último segundo de tus pujas si eres el único que puja")
    p.add_argument("--close-in", type=float, default=None, help="solo pruebas: cierre ficticio en N segundos")
    p.set_defaults(func=cmd_snipe)

    sub.add_parser("flip", help="Prueba del flipeo en modo sombra: qué pujaría ahora (no ejecuta nada)").set_defaults(func=cmd_flip)

    p = sub.add_parser("set-webhook", help="Configura el webhook de Telegram hacia el Worker (una vez)")
    p.add_argument("url", help="URL pública del Worker desplegado")
    p.add_argument("secret", help="el mismo valor que TELEGRAM_WEBHOOK_SECRET del Worker")
    p.set_defaults(func=cmd_set_webhook)

    p = sub.add_parser("execute-action", help="Interno: ejecuta una acción del botón de Telegram (sin vista previa)")
    p.add_argument("action", help='código de acción, p.ej. "c:2206"')
    p.set_defaults(func=cmd_execute_action)

    sub.add_parser("watch", help="Vigilancia continua con alertas por Telegram").set_defaults(func=cmd_watch)
    sub.add_parser("tick", help="Una sola pasada de vigilancia (para cron / GitHub Actions)").set_defaults(func=cmd_tick)

    args = parser.parse_args(argv)
    settings = load_settings()
    try:
        args.func(args, settings)
    except KeyboardInterrupt:
        print("\nHasta luego.")
    except RuntimeError as exc:
        sys.exit(f"❌ {exc}")


if __name__ == "__main__":
    main()
