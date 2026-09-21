"""Línea de comandos: `python -m fantasy_agent <comando>`."""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone

from . import auth, models, notify, service
from .api import FantasyAPI
from .attendance import estimate_titularidad
from .config import load_settings
from .storage import Store


def _last_sunday(year: int, month: int) -> datetime:
    d = datetime(year, month, 31, 1, 0, tzinfo=timezone.utc)  # el cambio de hora UE es a la 01:00 UTC
    while d.weekday() != 6:  # domingo
        d -= timedelta(days=1)
    return d


def _madrid_now() -> datetime:
    """Hora de España (CET/CEST) calculada a mano: en Windows `zoneinfo` necesita el paquete
    `tzdata` (no viene con el sistema) y el proyecto es solo librería estándar, así que se
    aplica la regla de la UE (DST del último domingo de marzo al último domingo de octubre)
    sin depender de nada externo. Necesario para que las horas de juego (mercado, cláusulas)
    signifiquen lo mismo tanto en local como en el runner de GitHub Actions (que va en UTC)."""
    utc_now = datetime.now(timezone.utc)
    dst_start = _last_sunday(utc_now.year, 3)
    dst_end = _last_sunday(utc_now.year, 10)
    offset = 2 if dst_start <= utc_now < dst_end else 1
    return utc_now.astimezone(timezone(timedelta(hours=offset)))


def _out(settings, text: str, telegram: bool) -> None:
    print(text)
    if telegram:
        notify.send_telegram(settings, text)


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
    amount = args.amount or item.price
    print(f"Jugador: {item.player.name} ({item.player.position} · {item.player.team})")
    print(f"Anuncio: {item.listing_id} · vendedor: {item.seller} · precio pedido: {service.m(item.price)}")
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


def cmd_section(args, s) -> None:
    api = FantasyAPI(s)
    world = _world(api, s, trends=args.cmd in ("market", "trends", "report", "losses", "sell-candidates", "market-news"))
    if args.cmd in ("clauses", "clauses-hot", "report"):
        service.ensure_clause_trends(api, world, s)
        service.ensure_speculative_trends(api, world, s)

    news_targets = []
    if args.cmd in ("lineup", "report") and getattr(args, "news", False):
        news_targets += [sl.player for sl in world.my_slots if sl.player.position_id != 5]
    if args.cmd in ("clauses", "report"):
        news_targets += service.clause_titularidad_candidates(world, s)
    news = estimate_titularidad(api, news_targets) if news_targets else None

    store = Store(s.db_file) if args.cmd in ("report", "losses", "sell-candidates", "market-news") else None

    rival_cash = {}
    if args.cmd in ("rivals", "clause-risk"):
        rival_cash = service.estimate_rival_cash(api, world)

    if args.cmd == "report":
        sections = service.report_sections(world, s, news, store, service.estimate_rival_cash(api, world))
        print("\n\n".join(sections))
        if args.telegram:
            notify.send_report(s, sections)
        return
    text = {
        "market": lambda: service.market_report(world),
        "trends": lambda: service.trends_report(world),
        "rivals": lambda: service.rivals_report(world, rival_cash),
        "clauses": lambda: service.clauses_report(world, s, news)[0],
        "clauses-hot": lambda: service.speculative_clauses_report(world, s)[0] or "Nada especulativo ahora mismo.",
        "clause-risk": lambda: service.clause_theft_report(world, rival_cash) or "Ningún rival te llega ahora mismo.",
        "lineup": lambda: service.lineup_report(world, news),
        "losses": lambda: service.losing_positions_report(world, store) or "Nada por debajo de lo que pagaste.",
        "sell-candidates": lambda: service.sell_candidates_report(world, store) or "Nadie con tendencia bajando ahora mismo.",
        "market-news": lambda: service.market_arrivals_report(world, store) or "Nada nuevo desde el último estudio.",
    }[args.cmd]()
    _out(s, text, args.telegram)


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
    api = FantasyAPI(s)
    world = _world(api, s, trends=daily_due or market_due)
    service.ensure_clause_trends(api, world, s)
    service.ensure_speculative_trends(api, world, s)

    clause_news = {}
    try:
        clause_news = estimate_titularidad(api, service.clause_titularidad_candidates(world, s))
    except Exception as exc:
        print(f"[titularidad] error cláusulas: {exc}")

    _, alerts = service.clauses_report(world, s, clause_news)
    fresh = [a for a in alerts if store.alert_is_new(a.key)]
    if fresh:
        notify.send_telegram(s, "<b>🚨 Alertas</b>\n\n" + "\n\n".join(a.message for a in fresh))

    _, hot_alerts = service.speculative_clauses_report(world, s)
    fresh_hot = [a for a in hot_alerts if store.alert_is_new(a.key)]
    if fresh_hot:
        notify.send_telegram(
            s, "<b>📈 Cláusulas especulativas</b>\n\n" + "\n\n".join(a.message for a in fresh_hot)
        )

    tx = service.my_transactions(api, world, store)
    if tx:
        notify.send_telegram(s, "<b>📒 Movimientos en tu equipo</b>\n\n" + "\n\n".join(tx))

    if market_due:
        arrivals = service.market_arrivals_report(world, store)
        if arrivals:
            notify.send_telegram(s, arrivals)
        store.set("last_market_study", today)

    if daily_due:
        news = dict(clause_news)
        try:
            news.update(estimate_titularidad(api, [sl.player for sl in world.my_slots if sl.player.position_id != 5]))
        except Exception as exc:
            print(f"[titularidad] error: {exc}")
        rival_cash = {}
        try:
            rival_cash = service.estimate_rival_cash(api, world)
        except Exception as exc:
            print(f"[aviso] sin saldo estimado de rivales: {exc}")
        notify.send_report(s, service.report_sections(world, s, news, store, rival_cash))
        store.set("last_daily", today)
    return (
        f"[{now:%H:%M}] ok · {len(fresh)} alertas nuevas · {len(fresh_hot)} especulativas · {len(tx)} movimientos"
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
    print(_watch_once(Store(s.db_file), s))


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
