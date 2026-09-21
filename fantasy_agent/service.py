"""Orquestación: descarga el estado de tu liga y genera informes y alertas."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from . import analysis, lineup, models
from .analysis import b, esc, i
from .api import FantasyAPI
from .config import Settings

LEAGUE_TOP_N = 3  # cuántos de cada posición se consideran "TOP de la liga"


@dataclass
class World:
    league_id: str
    my_team_id: str
    my_cash: int | None
    standing: list[models.TeamStanding]
    my_slots: list[models.SquadSlot]
    rival_slots: list[models.SquadSlot]
    market: list[models.MarketItem]
    trends: dict[str, tuple[models.Player, analysis.Trend]] = field(default_factory=dict)
    team_names: dict[str, str] = field(default_factory=dict)
    fixtures: dict[str, models.Fixture] = field(default_factory=dict)
    league_top_ids: set[str] = field(default_factory=set)
    clause_freeze: tuple[datetime, datetime] | None = None
    next_jornada: datetime | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def resolve_league(api: FantasyAPI, s: Settings) -> tuple[str, str | None, int | None]:
    leagues = models.as_list(api.leagues(), "leagues", "elements")
    if not leagues:
        raise RuntimeError("No se encontraron ligas en tu cuenta.")
    chosen = None
    if s.league_id:
        chosen = next((l for l in leagues if str(models.pick(l, "id")) == s.league_id), None)
        if chosen is None:
            raise RuntimeError(f"FANTASY_LEAGUE_ID={s.league_id} no está entre tus ligas.")
    else:
        chosen = leagues[0]
    team_id = models.pick(chosen, "team.id", "teamId", "myTeam.id")
    cash = models.to_int(models.pick(chosen, "team.money", "teamMoney", "money"), default=None)  # type: ignore[arg-type]
    return str(models.pick(chosen, "id")), (str(team_id) if team_id else None), cash


def resolve_my_team(api: FantasyAPI, s: Settings, standing: list[models.TeamStanding], hinted: str | None) -> str:
    if s.team_id:
        return s.team_id
    if hinted:
        return hinted
    me = api.me()
    my_ids = {str(v) for v in (models.pick(me, "id"), models.pick(me, "managerId"), models.pick(me, "userId")) if v}
    for row in standing:
        if row.manager_id in my_ids:
            return row.team_id
    raise RuntimeError("No encuentro tu equipo en la clasificación. Pon FANTASY_TEAM_ID en el .env (usa `fantasy standing`).")


def next_fixtures(api: FantasyAPI, team_ids: set[str]) -> dict[str, models.Fixture]:
    """Próximo partido de cada team_id: mira la jornada actual y, si falta alguno (ya jugó), la siguiente."""
    team_ids = {t for t in team_ids if t}
    if not team_ids:
        return {}
    try:
        current = models.to_int(models.pick(api.current_week(), "weekNumber"), default=0)
    except Exception:
        return {}
    out: dict[str, models.Fixture] = {}
    for wk in (current, current + 1):
        if not wk or len(out) >= len(team_ids):
            continue
        try:
            fixtures = models.parse_calendar(api.calendar(wk))
        except Exception:
            continue
        for f in fixtures:
            if f.team_id in team_ids and f.team_id not in out:
                out[f.team_id] = f
    return out


def league_top_ids(api: FantasyAPI, top_n: int = LEAGUE_TOP_N) -> set[str]:
    """ids de los `top_n` jugadores con más puntos totales EN CADA posición, de toda LaLiga
    (no solo tu liga privada): estos no son "para invertir", son fichajes prioritarios."""
    try:
        by_pos = models.points_by_position(api.players())
    except Exception:
        return set()
    out: set[str] = set()
    for rows in by_pos.values():
        rows.sort(key=lambda r: -r[1])
        out.update(pid for pid, _ in rows[:top_n])
    return out


def clause_freeze_window(api: FantasyAPI) -> tuple[datetime, datetime] | None:
    """La liga bloquea TODAS las cláusulas desde 24h antes del primer partido de la jornada
    hasta que arranca ese partido."""
    try:
        current = models.to_int(models.pick(api.current_week(), "weekNumber"), default=0)
        fixtures = models.parse_calendar(api.calendar(current))
    except Exception:
        return None
    dates = [f.when for f in fixtures if f.when]
    if not dates:
        return None
    first = min(dates)
    return first - timedelta(hours=24), first


def next_jornada_start(api: FantasyAPI) -> datetime | None:
    """Primer partido de la próxima jornada: si la jornada "actual" según la API todavía no
    ha empezado, es esa; si ya arrancó (algún partido ya se jugó), mira la siguiente."""
    try:
        current = models.to_int(models.pick(api.current_week(), "weekNumber"), default=0)
    except Exception:
        return None
    if not current:
        return None
    now = datetime.now(timezone.utc)
    for wk in (current, current + 1):
        try:
            fixtures = models.parse_calendar(api.calendar(wk))
        except Exception:
            continue
        dates = [f.when for f in fixtures if f.when]
        if dates and min(dates) > now:
            return min(dates)
    return None


def build_world(api: FantasyAPI, s: Settings, with_trends: bool = True) -> World:
    league_id, hinted_team, my_cash = resolve_league(api, s)
    standing = models.parse_standing(api.standing(league_id))
    my_team_id = resolve_my_team(api, s, standing, hinted_team)

    my_slots: list[models.SquadSlot] = []
    rival_slots: list[models.SquadSlot] = []
    for row in standing:
        slots = models.parse_squad(api.team(league_id, row.team_id), row.team_id, row.manager_name)
        (my_slots if row.team_id == my_team_id else rival_slots).extend(slots)

    market = models.parse_market(api.market(league_id))
    team_names = {sl.player.team_id: sl.player.team for sl in (*my_slots, *rival_slots) if sl.player.team != "?"}
    for item in market:
        if item.player.team == "?" and item.player.team_id in team_names:
            item.player.team = team_names[item.player.team_id]
    fixtures = next_fixtures(api, {sl.player.team_id for sl in my_slots})
    world = World(
        league_id, my_team_id, my_cash, standing, my_slots, rival_slots, market,
        team_names=team_names, fixtures=fixtures,
        league_top_ids=league_top_ids(api), clause_freeze=clause_freeze_window(api),
        next_jornada=next_jornada_start(api),
    )

    if with_trends:
        tracked = {i.player.id: i.player for i in market}
        tracked.update({sl.player.id: sl.player for sl in my_slots})
        for pid, player in tracked.items():
            try:
                hist = models.parse_value_history(api.market_value_history(pid))
                world.trends[pid] = (player, analysis.trend_from_history(hist))
            except Exception as exc:
                print(f"[aviso] sin histórico para {player.name}: {exc}")
    return world


# ---------------- informes de texto -----------------------------------------
def m(amount: int | None) -> str:
    return "?" if amount is None else f"{amount / 1_000_000:.2f}M"


def _fmt_when(when: datetime, now: datetime) -> str:
    delta = when - now
    if delta <= timedelta(hours=48):
        hours = int(delta.total_seconds() // 3600)
        minutes = int(delta.total_seconds() % 3600 // 60)
        return f"en {hours}h {minutes:02d}min"
    return f"el {when.astimezone().strftime('%d/%m %H:%M')}"


def _biddable(world: World) -> list[models.MarketItem]:
    """Solo lo que puede pujarse de verdad: anuncios de LaLiga. Lo que 'venden' otros
    entrenadores de la liga NO es pujable entre nosotros — a esos solo se llega por cláusula."""
    return [i for i in world.market if i.seller == "LaLiga" and i.player.position_id != 5]


def _opportunities(world: World) -> list[analysis.Opportunity]:
    neutral = analysis.Trend(0, 0, 0)
    opps = [
        analysis.score_market_item(i, world.trends.get(i.player.id, (i.player, neutral))[1], world.my_cash)
        for i in _biddable(world)
    ]
    opps.sort(key=lambda o: o.score, reverse=True)
    return opps


def position_ppm_benchmark(world: World, position_id: int, exclude_id: str | None = None) -> float:
    """Mediana de puntos-por-millón entre los anuncios pujables de LaLiga en esa posición
    ahora mismo: la "tarifa" vigente de mercado por punto, referencia para el techo de puja
    (`analysis.bid_ceiling`)."""
    ppms = []
    for item in _biddable(world):
        if item.player.position_id != position_id or item.player.id == exclude_id:
            continue
        if not item.player.avg_points or item.price <= 0:
            continue
        ppms.append(item.player.avg_points / (item.price / 1_000_000))
    if not ppms:
        return 0.0
    ppms.sort()
    mid = len(ppms) // 2
    return ppms[mid] if len(ppms) % 2 else (ppms[mid - 1] + ppms[mid]) / 2


def bid_ceiling_report(world: World, player: models.Player) -> str:
    benchmark = position_ppm_benchmark(world, player.position_id, exclude_id=player.id)
    is_top = player.id in world.league_top_ids
    ceiling = analysis.bid_ceiling(player.avg_points, benchmark, is_top)
    star = "🌟 " if is_top else ""
    header = f"{star}{b(player.name)} <i>{player.position}·{esc(player.team)}</i> · {player.avg_points:.1f}p/partido"
    header += f" · tarifa {benchmark:.2f}p/M" if benchmark else " · " + i("sin referencia de mercado en su posición")
    lines = [header]
    if ceiling:
        prima = " (+30% prima top)" if is_top else ""
        lines.append(f"{b('Techo de puja: ' + m(ceiling))}{prima}")
        lines.append(i("Por encima, mejor negocio la alternativa del mercado aunque ganes la puja ciega."))
    else:
        lines.append(i("Sin datos suficientes para un techo fiable ahora mismo."))
    return "\n".join(lines)


def find_player(api: FantasyAPI, world: World, player_id: str) -> models.Player | None:
    for sl in (*world.my_slots, *world.rival_slots):
        if sl.player.id == player_id:
            return sl.player
    for item in world.market:
        if item.player.id == player_id:
            return item.player
    try:
        return models.parse_player(api.player(player_id))
    except Exception:
        return None


def market_report(world: World, min_score: float = 8.0) -> str:
    """Fichajes deportivos para tu once. Un TOP de la liga sale siempre, aunque su score sea
    bajo por precio: no es una cuestión de "compensa el precio", es que es de los mejores del
    campeonato en su puesto y te lo estás perdiendo si no lo ves. Lleva también su lado
    económico (tendencia y proyección a 14 días): fichar bien y que encima suba de valor
    no son cosas distintas, es la misma decisión."""
    cards = []
    for o in _opportunities(world):
        p = o.item.player
        is_top = p.id in world.league_top_ids
        if o.score < min_score and not is_top:
            continue
        star = "🌟 " if is_top else ""
        trend = world.trends.get(p.id, (p, analysis.Trend(0, 0, 0)))[1]
        cards.append(
            f"{star}{b(p.name)}  <i>{p.position} · {esc(p.team)}</i>\n"
            f"💰 {b(m(o.item.price))} · {p.avg_points:.1f} pts/partido\n"
            f"{i(analysis.trend_words(trend))}"
        )
    if not cards:
        return ""
    head = f"{b('🛒 Mercado para tu once')}\n{i('Saldo disponible: ' + m(world.my_cash))}"
    return head + "\n\n" + "\n\n".join(cards)


def investment_report(world: World, top: int = 5) -> str:
    """Comprar barato y revender. Los TOP de la liga NO entran aquí: a esos los quieres
    para tu equipo, no para venderlos en 14 días."""
    picks = []
    for item in _biddable(world):
        if item.player.id in world.league_top_ids:
            continue
        trend = world.trends.get(item.player.id, (item.player, analysis.Trend(0, 0, 0)))[1]
        score = analysis.score_investment(item, trend)
        if score is not None:
            picks.append((score, item, trend))
    if not picks:
        return ""
    picks.sort(key=lambda x: -x[0])
    cards = []
    for score, item, t in picks[:top]:
        p = item.player
        cards.append(
            f"{b(p.name)}  <i>{esc(p.team)}</i>\n"
            f"💰 {b(m(item.price))}\n"
            f"{i(analysis.trend_words(t))}"
        )
    head = f"{b('💹 Oportunidades de inversión')}\n{i('Comprar y revender, no para tu once')}"
    return head + "\n\n" + "\n\n".join(cards)


def trends_report(world: World) -> str:
    mine = {sl.player.id for sl in world.my_slots}
    lines = []
    my_falling = sorted(
        [(p, t) for pid, (p, t) in world.trends.items() if pid in mine and t.d3 <= -2], key=lambda x: x[1].d3
    )[:5]
    if my_falling:
        lines.append(
            f"{b('⚠️ Véndelos antes de que bajen más')}\n"
            + "\n".join(f"{b(p.name)} {i(analysis.trend_words(t))}" for p, t in my_falling)
        )
    peaking = analysis.sell_high_candidates(world.trends, mine)
    if peaking:
        lines.append(
            f"{b('🏔️ En máximo, ya se frena, véndelos ya')}\n"
            + "\n".join(f"{b(p.name)} {i(analysis.trend_words(t))}" for p, t in peaking)
        )
    if not lines:
        return ""
    return f"{b('📊 Tus jugadores: vender o mantener')}\n\n" + "\n".join(lines)


def market_arrivals_report(world: World, store) -> str:
    """Lo que ha entrado nuevo al mercado de LaLiga desde el último estudio (pensado para
    correr una vez al día, justo tras el refresco diario del mercado a las 21:00) con un
    veredicto propio para cada fichaje — no una lista recortada por nota de corte, sino
    "esto es lo fresco y esto es lo que opino de cada uno". Guarda qué ids ha visto para poder
    distinguir "nuevo" de "ya lo vi ayer y sigue sin venderse"."""
    current = _biddable(world)
    seen = set(store.prefixed("market_seen:").keys())
    now_ids = {item.player.id for item in current}
    new_items = [item for item in current if item.player.id not in seen]
    for pid in seen - now_ids:
        store.set(f"market_seen:{pid}", "")
    for item in current:
        store.set(f"market_seen:{item.player.id}", "1")
    if not new_items:
        return ""

    rows = []
    for item in new_items:
        trend = world.trends.get(item.player.id, (item.player, analysis.Trend(0, 0, 0)))[1]
        is_top = item.player.id in world.league_top_ids
        stars, label, reasons = analysis.market_verdict(item, trend, world.my_cash, is_top)
        rows.append((stars, item, trend, label, reasons))
    rows.sort(key=lambda r: -r[0])

    cards = []
    for stars, item, trend, label, reasons in rows:
        p = item.player
        stars_str = "★" * stars + "☆" * (4 - stars)
        detail = " · ".join(esc(r) for r in reasons) + " · " + analysis.trend_words(trend)
        cards.append(
            f"{b(p.name)} <i>{p.position}·{esc(p.team)}</i> · {b(m(item.price))} · {stars_str} {label}\n"
            f"{i(detail)}"
        )
    head = f"{b('🗞️ Nuevo en el mercado')}\n{i('Estudio de viabilidad')}"
    return head + "\n\n" + "\n".join(cards)


def losing_positions_report(world: World, store) -> str:
    """Jugadores tuyos por debajo de lo que pagaste (precio de compra real, no tendencia de
    mercado sin más) — usa el histórico de `my_transactions`, así que solo cubre lo comprado
    desde que ese seguimiento arrancó."""
    buy_prices = {pid: int(v) for pid, v in store.prefixed("buy_price:").items()}
    trends = {pid: t for pid, (_, t) in world.trends.items()}
    candidates = analysis.loss_cut_candidates(world.my_slots, buy_prices, trends)
    if not candidates:
        return ""
    cards = []
    for slot, buy, loss_pct in candidates:
        p = slot.player
        cards.append(
            f"{b(p.name)} · {m(buy)} → {m(p.market_value)} · "
            f"{i(f'-{loss_pct:.0f}% ({m(buy - p.market_value)})')}"
        )
    return f"{b('🔻 Corta pérdidas')}\n{i('Por debajo de lo que pagaste')}\n\n" + "\n".join(cards)


def sell_candidates_report(world: World, store) -> str:
    """Candidatos a poner a la venta según tendencia (ver `analysis.sell_candidates`): lleva
    bajando 3 días, sea cual sea la pérdida acumulada. Es un aviso, no ejecuta nada — pon a la
    venta con el comando `sell` tras revisarlo."""
    buy_prices = {pid: int(v) for pid, v in store.prefixed("buy_price:").items()}
    trends = {pid: t for pid, (_, t) in world.trends.items()}
    candidates = analysis.sell_candidates(world.my_slots, buy_prices, trends)
    if not candidates:
        return ""
    cards = []
    for slot in candidates:
        p = slot.player
        buy = buy_prices[p.id]
        trend = trends[p.id]
        diff_pct = (p.market_value - buy) / buy * 100 if buy else 0
        cards.append(
            f"{b(p.name)} · {m(buy)} → {m(p.market_value)} ({diff_pct:+.0f}%) · "
            f"{i(analysis.trend_words(trend))}"
        )
    head = f"{b('📉 Candidatos a vender')}\n{i('Tendencia bajando 3 días — no esperar a que caiga más')}"
    return head + "\n\n" + "\n".join(cards)


def rivals_report(world: World, rival_cash: dict[str, int] | None = None) -> str:
    """Solo el dinero de cada rival y si te puede pagar alguna cláusula tuya con lo que
    tiene — nada de detalle de sus plantillas."""
    now = datetime.now(timezone.utc)
    my_open_clauses = [sl.clause for sl in world.my_slots if sl.clause_open(now)]
    cards = []
    for row in sorted(world.standing, key=lambda r: r.points, reverse=True):
        if row.team_id == world.my_team_id:
            continue
        cash = (rival_cash or {}).get(row.team_id)
        if cash is None:
            cards.append(b(row.manager_name))
            continue
        threat = any(cash >= c for c in my_open_clauses)
        icon = "✅" if threat else "❌"
        note = "puede clausularte" if threat else "no le llega"
        cards.append(f"{b(row.manager_name)} · {m(cash)} · {icon} {i(note)}")
    if not cards:
        return f"{b('👥 Rivales')}\n{i('Sin rivales que mostrar.')}"
    head = b("👥 Rivales")
    if rival_cash:
        head += f"\n{i('Saldo estimado a partir del historial de fichajes')}"
    return head + "\n\n" + "\n".join(cards)


def estimate_rival_cash(api: FantasyAPI, world: World, max_pages: int = 20) -> dict[str, int]:
    """Saldo estimado de cada equipo a partir del historial completo de movimientos
    (`/activity`, paginado hacia atrás hasta que llega vacío): la API solo expone tu propio
    saldo (`world.my_cash`), así que se reconstruye el de los rivales sumando compras, ventas,
    cláusulas y bonos semanales desde el principio de temporada, calibrando el presupuesto de
    partida con tu saldo real. Ver `analysis.estimate_cash` para el detalle y sus supuestos."""
    if world.my_cash is None:
        return {}
    events: list[models.Activity] = []
    for idx in range(max_pages):
        try:
            raw = api.activity(world.league_id, idx)
        except Exception as exc:
            print(f"[aviso] fallo leyendo actividad (index {idx}): {exc}")
            break
        if not raw:
            break
        events.extend(models.parse_activity(raw))

    my_manager_id = next((r.manager_id for r in world.standing if r.team_id == world.my_team_id), None)
    if not my_manager_id:
        return {}
    manager_ids = {r.team_id: r.manager_id for r in world.standing}
    return analysis.estimate_cash(events, my_manager_id, world.my_cash, manager_ids)


def clause_theft_report(world: World, rival_cash: dict[str, int]) -> str:
    """Tus jugadores con la cláusula pagable ahora mismo que algún rival podría pagarte, según
    el saldo estimado. Solo se muestra si hay riesgo real, para no repetir en cada informe."""
    now = datetime.now(timezone.utc)
    others_cash = {tid: cash for tid, cash in rival_cash.items() if tid != world.my_team_id}
    risky = analysis.clause_theft_risk(world.my_slots, others_cash, now)
    if not risky:
        return ""
    names_by_team = {r.team_id: r.manager_name for r in world.standing}
    cards = []
    for slot, threats in risky:
        p = slot.player
        threat_names = esc(", ".join(names_by_team.get(t, t) for t in threats))
        cards.append(f"{b(p.name)} · {b(m(slot.clause))} · {i('podrían pagarla: ' + threat_names)}")
    head = f"{b('⚠️ Riesgo de que te clausulen')}\n{i('Saldo estimado, puede desviarse')}"
    return head + "\n\n" + "\n".join(cards)


def daily_advice_report(world: World, rival_cash: dict[str, int] | None = None) -> str:
    """Consejo táctico del día — un cierre con un solo paso concreto, priorizado por lo que
    de verdad urge ahora mismo: primero si te pueden clausular algo importante, luego si
    tienes dinero parado sin invertir, y si no hay nada urgente, un principio general de
    estrategia de la comunidad (rota uno distinto cada día para no repetir)."""
    tip = None
    if rival_cash:
        now = datetime.now(timezone.utc)
        others_cash = {tid: cash for tid, cash in rival_cash.items() if tid != world.my_team_id}
        risky = analysis.clause_theft_risk(world.my_slots, others_cash, now)
        if risky:
            slot, _ = max(risky, key=lambda r: r[0].player.avg_points)
            tip = f"⚠️ Blinda a {b(slot.player.name)} en la app: con lo que tiene ahora, algún rival podría pagarte su cláusula."

    if tip is None and world.my_cash:
        squad_value = sum(sl.player.market_value for sl in world.my_slots) or 1
        if world.my_cash / squad_value > 0.25:
            tip = f"💰 Tienes {b(m(world.my_cash))} parados — dinero sin invertir no puntúa, busca un chollo o refuerza el once."

    if tip is None:
        day_index = datetime.now(timezone.utc).timetuple().tm_yday
        tip = esc(analysis.STRATEGY_TIPS[day_index % len(analysis.STRATEGY_TIPS)])

    return f"{b('🎓 Consejo del día')}\n{tip}"


def ensure_clause_trends(api: FantasyAPI, world: World, s: Settings) -> None:
    """Pide histórico de valor solo para quien de verdad puede acabar en una alerta de
    cláusula (el filtro económico barato ya lo acota) — no para los 30+ rivales de la liga."""
    now = datetime.now(timezone.utc)
    for p in analysis.clause_candidate_players(world.rival_slots, now, s.clause_window_hours):
        if p.id in world.trends:
            continue
        try:
            hist = models.parse_value_history(api.market_value_history(p.id))
            world.trends[p.id] = (p, analysis.trend_from_history(hist))
        except Exception as exc:
            print(f"[aviso] sin histórico para {p.name}: {exc}")


def clause_titularidad_candidates(world: World, s: Settings) -> list[models.Player]:
    now = datetime.now(timezone.utc)
    return analysis.clause_candidate_players(world.rival_slots, now, s.clause_window_hours)


def clauses_report(world: World, s: Settings, news: dict[str, dict] | None = None) -> tuple[list[str], list[analysis.ClauseAlert]]:
    """Un mensaje de Telegram por cláusula, no todas juntas en un tocho — así cada jugador
    puede llevar más adelante sus propios botones de acción (pagar / ignorar)."""
    now = datetime.now(timezone.utc)
    trends = {pid: t for pid, (_, t) in world.trends.items()}
    alerts = analysis.clause_alerts(
        world.rival_slots, world.my_cash, now, s.clause_window_hours,
        freeze=world.clause_freeze, trends=trends, news=news,
    )
    frozen_note = ""
    if world.clause_freeze and world.clause_freeze[0] <= now < world.clause_freeze[1]:
        until = world.clause_freeze[1].astimezone().strftime("%d/%m %H:%M")
        frozen_note = i(f"⏸️ Cláusulas congeladas hasta las {until} (empieza la jornada)")
    if not alerts:
        head = f"{b('🔐 Cláusulas')}\n{i(f'Nada relevante en las próximas {s.clause_window_hours}h')}"
        return [head + (("\n\n" + frozen_note) if frozen_note else "")], alerts
    messages = [f"🔐 {a.message}" for a in alerts]
    if frozen_note:
        messages.append(frozen_note)
    return messages, alerts


def ensure_speculative_trends(api: FantasyAPI, world: World, s: Settings) -> None:
    """Igual que `ensure_clause_trends` pero para candidatos especulativos (cláusula cara
    respecto a mercado, pero podría compensar si la racha es fuerte de verdad)."""
    now = datetime.now(timezone.utc)
    for p in analysis.speculative_clause_candidates(world.rival_slots, now):
        if p.id in world.trends:
            continue
        try:
            hist = models.parse_value_history(api.market_value_history(p.id))
            world.trends[p.id] = (p, analysis.trend_from_history(hist))
        except Exception as exc:
            print(f"[aviso] sin histórico para {p.name}: {exc}")


def speculative_clauses_report(world: World, s: Settings) -> tuple[list[str], list[analysis.SpeculativeAlert]]:
    """Igual que `clauses_report`: un mensaje por jugador, no todos juntos."""
    now = datetime.now(timezone.utc)
    trends = {pid: t for pid, (_, t) in world.trends.items()}
    alerts = analysis.speculative_clause_alerts(
        world.rival_slots, world.my_cash, now, freeze=world.clause_freeze, trends=trends,
    )
    if not alerts:
        return [], alerts
    return [f"📈 {a.message}" for a in alerts], alerts


def _player_name(api: FantasyAPI, world: World, player_id: str) -> str:
    for sl in (*world.my_slots, *world.rival_slots):
        if sl.player.id == player_id:
            return sl.player.name
    for item in world.market:
        if item.player.id == player_id:
            return item.player.name
    # No está en la plantilla/mercado actual (p.ej. lo revendieron varias veces desde
    # entonces): pedir su ficha suelta, que existe siempre aunque ya no circule por la liga.
    try:
        return models.parse_player(api.player(player_id)).name
    except Exception:
        return f"jugador #{player_id}"


_TX_BUY, _TX_SELL, _TX_CLAUSE = models.ACTIVITY_BUY, models.ACTIVITY_SELL, models.ACTIVITY_CLAUSE


def my_transactions(api: FantasyAPI, world: World, store) -> list[str]:
    """Compras, ventas y cláusulas (pagadas o sufridas) tuyas desde la última vez que se miró
    el feed de actividad, con la ganancia/pérdida calculada cuando se conoce el precio de
    compra. Usa como marca de agua el id de actividad más alto ya visto —no un TTL— porque el
    feed siempre devuelve el historial completo: con TTL, pasado ese tiempo se volvería a
    notificar como si fuera nuevo. La primera vez que se ejecuta no manda nada (evita
    reproducir toda la temporada de golpe), solo registra precios de compra recientes y arranca
    el seguimiento desde ahí."""
    my_id = next((r.manager_id for r in world.standing if r.team_id == world.my_team_id), None)
    if not my_id:
        return []
    try:
        events = models.parse_activity(api.activity(world.league_id, 0))
    except Exception as exc:
        print(f"[aviso] sin feed de actividad: {exc}")
        return []

    cold_start = store.get("last_activity_id") is None
    last_seen = models.to_int(store.get("last_activity_id"))
    newest = last_seen
    mine = []
    for ev in events:
        eid = models.to_int(ev.id)
        newest = max(newest, eid)
        if eid <= last_seen or ev.player_id is None:
            continue
        if my_id not in (ev.user1_id, ev.user2_id):
            continue
        mine.append(ev)
    store.set("last_activity_id", str(newest))
    mine.sort(key=lambda e: models.to_int(e.id))

    if cold_start:
        for ev in mine:
            if ev.type_id in (_TX_BUY, _TX_CLAUSE) and ev.user1_id == my_id:
                store.set(f"buy_price:{ev.player_id}", str(ev.amount))
                store.set(f"buy_date:{ev.player_id}", (ev.when or datetime.now(timezone.utc)).isoformat())
        return []

    messages = []
    for ev in mine:
        name = b(_player_name(api, world, ev.player_id))
        if ev.type_id == _TX_BUY:
            store.set(f"buy_price:{ev.player_id}", str(ev.amount))
            store.set(f"buy_date:{ev.player_id}", (ev.when or datetime.now(timezone.utc)).isoformat())
            messages.append(f"{b('🛒 Compra')}\n{name}\nPagado: {m(ev.amount)}")
        elif ev.type_id == _TX_CLAUSE and ev.user1_id == my_id:
            store.set(f"buy_price:{ev.player_id}", str(ev.amount))
            store.set(f"buy_date:{ev.player_id}", (ev.when or datetime.now(timezone.utc)).isoformat())
            messages.append(f"{b('🔐 Cláusula pagada por ti')}\n{name}\nPagaste: {m(ev.amount)}")
        elif ev.type_id == _TX_SELL or (ev.type_id == _TX_CLAUSE and ev.user2_id == my_id):
            label = b("💰 Venta") if ev.type_id == _TX_SELL else b("⚠️ Te han clausulado")
            verb = "Vendido por" if ev.type_id == _TX_SELL else "Recibiste"
            buy = store.get(f"buy_price:{ev.player_id}")
            if buy:
                gain = ev.amount - int(buy)
                pct = gain / int(buy) * 100 if int(buy) else 0.0
                sign = "+" if gain >= 0 else ""
                gain_line = f"Ganancia: {sign}{m(gain)} ({pct:+.0f}%)"
                messages.append(
                    f"{label}\n{name}\n{verb}: {m(ev.amount)} · Comprado por: {m(int(buy))}\n"
                    f"{i(gain_line)}"
                )
            else:
                messages.append(f"{label}\n{name}\n{verb}: {m(ev.amount)}\n{i('(sin precio de compra registrado)')}")
            store.set(f"buy_price:{ev.player_id}", "")
            store.set(f"buy_date:{ev.player_id}", "")
    return messages


def _rival_name(world: World, team_id: str) -> str:
    f = world.fixtures.get(team_id)
    if not f:
        return i("rival: ?")
    rival = world.team_names.get(f.rival_id, f"equipo #{f.rival_id}")
    icon = "🏠" if f.home else "✈️"
    return i(f"{icon} {rival}")


def lineup_report(world: World, news: dict[str, dict] | None) -> str:
    cands = []
    for sl in world.my_slots:
        p = sl.player
        if p.position_id == 5:
            continue
        info = (news or {}).get(p.id, {})
        prob = float(info.get("start_probability", 70)) / 100
        if info.get("status") in ("lesionado", "sancionado"):
            p = replace(p, status="injured")
        elif info.get("status") == "duda" and p.available:
            p = replace(p, status="doubtful")
        cands.append(lineup.Candidate(p, prob, lineup.expected_points(p, prob), info.get("note", "")))

    formation, eleven, total = lineup.best_eleven(cands)
    if not eleven:
        return f"{b('🧩 Once')}\n{i('No hay jugadores suficientes en la plantilla.')}"
    subtitle = f"{'-'.join(map(str, formation))} · {total} pts esperados"
    if len(eleven) < 11:
        subtitle += f" · ⚠️ solo {len(eleven)} jugadores válidos"
    head = f"{b('🧩 Once recomendado')}\n{i(subtitle)}"
    now = datetime.now(timezone.utc)
    if world.next_jornada:
        head += f"\n🗓️ Empieza la jornada: {_fmt_when(world.next_jornada, now)}"
    lines = [head, ""]
    group_names = {1: "🧤 Portero", 2: "🛡️ Defensas", 3: "🎯 Centrocampistas", 4: "⚔️ Delanteros"}
    for pos_id in (1, 2, 3, 4):
        group = sorted((c for c in eleven if c.player.position_id == pos_id), key=lambda c: -c.xpts)
        if not group:
            continue
        lines.append(b(group_names[pos_id]))
        for c in group:
            lines.append(
                f"{b(c.player.name)} · {c.start_prob:.0%} · {c.xpts}p · {_rival_name(world, c.player.team_id)}"
            )
    risky = [c for c in eleven if c.start_prob < 0.6]
    if risky:
        lines.append(b("⚠️ Dudas en el once"))
        for c in risky:
            why = c.note or f"probabilidad de titularidad baja ({c.start_prob:.0%})"
            lines.append(f"{b(c.player.name)}: {esc(why)}")
    if news is None:
        lines.append(i("Sin noticias: probabilidad de titularidad por defecto 70%. Usa --news para afinarlo."))
    return "\n".join(lines)


def report_sections(
    world: World, s: Settings, news: dict[str, dict] | None, store=None, rival_cash: dict[str, int] | None = None,
) -> list[str]:
    """Un mensaje por especialidad (alineación / mercado / cláusulas...), listo para Telegram.
    Las cláusulas van una por mensaje (ver `clauses_report`), no agrupadas. Omite lo que no
    tenga nada relevante que decir, para no mandar un tocho. `store` es opcional: sin él no se
    puede saber qué pagaste por tus jugadores, así que se omite la sección de corta-pérdidas.
    `rival_cash` opcional: sin él se omiten el riesgo de que te clausulen y el saldo de
    rivales (requieren el historial completo de movimientos, más caro de pedir)."""
    stamp = world.fetched_at.astimezone().strftime("%d/%m/%Y %H:%M")
    sections = [f"{b('⚽ Informe')}\n{i(stamp)}\n\n{lineup_report(world, news)}"]

    market_parts = [p for p in (market_report(world), investment_report(world), trends_report(world)) if p]
    if store is not None:
        losing = losing_positions_report(world, store)
        if losing:
            market_parts.append(losing)
        selling = sell_candidates_report(world, store)
        if selling:
            market_parts.append(selling)
    if market_parts:
        sections.append("\n\n".join(market_parts))

    clause_messages, alerts = clauses_report(world, s, news)
    if alerts:
        sections.extend(clause_messages)

    if rival_cash:
        theft = clause_theft_report(world, rival_cash)
        if theft:
            sections.append(theft)
        sections.append(rivals_report(world, rival_cash))

    sections.append(daily_advice_report(world, rival_cash))
    return sections


def full_report(world: World, s: Settings, news: dict[str, dict] | None) -> str:
    return "\n\n".join(report_sections(world, s, news))
