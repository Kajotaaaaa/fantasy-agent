"""Línea de comandos: `python -m fantasy_agent <comando>`."""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime

from . import auth, notify, service
from .api import FantasyAPI
from .attendance import estimate_start_probability
from .config import load_settings
from .storage import Store


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


def cmd_section(args, s) -> None:
    api = FantasyAPI(s)
    world = _world(api, s, trends=args.cmd in ("market", "trends", "report"))
    news = None
    if args.cmd in ("lineup", "report") and getattr(args, "news", False):
        news = estimate_start_probability(api, [sl.player for sl in world.my_slots if sl.player.position_id != 5])
    text = {
        "market": lambda: service.market_report(world, args.top),
        "trends": lambda: service.trends_report(world),
        "rivals": lambda: service.rivals_report(world),
        "clauses": lambda: service.clauses_report(world, s)[0],
        "lineup": lambda: service.lineup_report(world, news),
        "report": lambda: service.full_report(world, s, news),
    }[args.cmd]()
    _out(s, text, args.telegram)


def _watch_once(store: Store, s) -> str:
    """Una pasada: alertas de cláusula siempre, informe completo si toca hoy."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    daily_due = now.hour >= s.report_hour and store.get("last_daily") != today
    api = FantasyAPI(s)
    world = _world(api, s, trends=daily_due)

    _, alerts = service.clauses_report(world, s)
    fresh = [a for a in alerts if store.alert_is_new(a.key)]
    if fresh:
        notify.send_telegram(s, "🚨 ALERTAS\n" + "\n".join(a.message for a in fresh))

    if daily_due:
        news = None
        try:
            news = estimate_start_probability(api, [sl.player for sl in world.my_slots if sl.player.position_id != 5])
        except Exception as exc:
            print(f"[titularidad] error: {exc}")
        notify.send_telegram(s, service.full_report(world, s, news))
        store.set("last_daily", today)
    return f"[{now:%H:%M}] ok · {len(fresh)} alertas nuevas{' · informe diario enviado' if daily_due else ''}"


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
        ("trends", "Subidas y bajadas de valor"),
        ("rivals", "Resumen de rivales"),
        ("clauses", "Alarmas de cláusulas"),
        ("lineup", "Once recomendado"),
        ("report", "Informe completo"),
    ]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--telegram", action="store_true", help="enviar también por Telegram")
        p.add_argument("--top", type=int, default=10)
        if name in ("lineup", "report"):
            p.add_argument("--news", action="store_true", help="estima titularidad por histórico de jornadas jugadas")
        p.set_defaults(func=cmd_section)

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
