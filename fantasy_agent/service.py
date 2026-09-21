"""Orquestación: descarga el estado de tu liga y genera informes y alertas."""
from __future__ import annotations

import json
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
    if len(ppms) < 3:  # con menos referencias la mediana no significa nada
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


def _keyboard(rows: list[list[dict]]) -> dict | None:
    """Teclado inline de Telegram (o None si no hay filas). Una fila por acción, sin repetir
    la misma acción dos veces (`callback_data` igual)."""
    seen, unique = set(), []
    for row in rows:
        code = row[0]["callback_data"]
        if code not in seen:
            seen.add(code)
            unique.append(row)
    return {"inline_keyboard": unique} if unique else None


def _action_row(label: str, code: str) -> list[dict]:
    return [{"text": label, "callback_data": code}]


def _market_picks(world: World, min_score: float = 8.0) -> list[tuple[models.MarketItem, analysis.Trend, bool]]:
    picks = []
    for o in _opportunities(world):
        p = o.item.player
        is_top = p.id in world.league_top_ids
        if o.score < min_score and not is_top:
            continue
        trend = world.trends.get(p.id, (p, analysis.Trend(0, 0, 0)))[1]
        picks.append((o.item, trend, is_top))
    return picks


def _investment_picks(world: World, top: int = 5) -> list[tuple[models.MarketItem, analysis.Trend]]:
    picks = []
    for item in _biddable(world):
        if item.player.id in world.league_top_ids:
            continue
        trend = world.trends.get(item.player.id, (item.player, analysis.Trend(0, 0, 0)))[1]
        score = analysis.score_investment(item, trend)
        if score is not None:
            picks.append((score, item, trend))
    picks.sort(key=lambda x: -x[0])
    return [(item, trend) for _, item, trend in picks[:top]]


def bid_amount(item: models.MarketItem) -> int:
    """Cantidad a pujar: el mayor entre el precio pedido y el valor de mercado actual. El
    anuncio puede pedir menos que lo que vale ahora el jugador (el precio se fija en el ciclo
    de las 21:00 y el valor se actualiza después) y el servidor rechaza esa puja con
    `030.01.01 "is not a valid money quantity for this player"` — comprobado con Yuri
    (pedía 15.21M, valía 15.52M); Cestero, que pedía algo más de lo que valía, sí coló."""
    return max(item.price, item.player.market_value or 0)


def _plan_for(
    world: World, item: models.MarketItem, trend: analysis.Trend, is_top: bool, with_ceiling: bool = True,
) -> analysis.BidPlan:
    """Las tres pujas posibles de un anuncio (ver `analysis.bid_plan`). El techo por puntos solo
    tiene sentido para fichajes de tu once, no para flipeo (`with_ceiling=False`)."""
    p = item.player
    benchmark = position_ppm_benchmark(world, p.position_id, exclude_id=p.id) if with_ceiling else 0.0
    return analysis.bid_plan(bid_amount(item), p.market_value, trend, p.avg_points, benchmark, is_top)


def _rivals_line(world: World, price: int, rival_cash: dict[str, int] | None) -> list[str]:
    """Cuántos rivales podrían pujar tanto como `price`, según su saldo estimado (con el
    margen de error de `analysis.CASH_UNCERTAINTY`: "seguro" solo si les sobra aun restándolo)."""
    if not rival_cash:
        return []
    verdicts = [
        analysis.can_bid(price, cash) for team_id, cash in rival_cash.items() if team_id != world.my_team_id
    ]
    if not verdicts:
        return []
    yes, maybe, no = verdicts.count("yes"), verdicts.count("maybe"), verdicts.count("no")
    return [i(f"👥 Rivales que pueden pujar: ✅ {yes} · ❔ {maybe} · ❌ {no} (seguro · dudoso · no; saldo estimado ±40M)")]


def _plan_lines(plan: analysis.BidPlan) -> list[str]:
    lines = []
    if plan.margin:
        lines.append(
            f"📈 Con margen: {b(m(plan.margin))} · se espera ~{m(plan.expected)} en 3 días, "
            "te quedas la mitad de la ganancia"
        )
    if plan.ceiling:
        lines.append(f"🎯 Si lo quieres sí o sí: hasta {b(m(plan.ceiling))} · más allá, mejor la alternativa del mercado")
    return lines


def _bid_rows(
    world: World, item: models.MarketItem, trend: analysis.Trend, is_top: bool, with_ceiling: bool = True,
) -> list[list[dict]]:
    """Una fila de botón por cada puja posible (mínimo / con margen / techo). La cantidad viaja
    en el propio código ("b:<anuncio>:<cantidad>"): lo que confirmas es exactamente lo que se
    puja, aunque la tendencia cambie entre que se manda el aviso y pulsas. Nunca ofrece una
    puja que no te llega de saldo (regla del usuario: prohibido quedarse en negativo)."""
    if not item.listing_id:
        return []
    plan = _plan_for(world, item, trend, is_top, with_ceiling)
    options = [("💰", "mínimo", plan.minimum)]
    if plan.margin:
        options.append(("📈", "margen", plan.margin))
    if plan.ceiling:
        options.append(("🎯", "techo", plan.ceiling))
    affordable = [(icon, tag, amount) for icon, tag, amount in options if world.my_cash is None or amount <= world.my_cash]
    if item.my_bid_id:
        # Ya hay una puja tuya pendiente: un segundo POST da error (030.01.09), lo que se puede
        # hacer es CAMBIAR su cantidad (PUT .../bid/{id}); código "u:<anuncio>:<puja>:<cantidad>".
        return [
            _action_row(
                f"✏️ {item.player.name}: puja {m(item.my_bid)} → {tag} {m(amount)}",
                f"u:{item.listing_id}:{item.my_bid_id}:{amount}",
            )
            for icon, tag, amount in affordable if amount != item.my_bid
        ]
    return [
        _action_row(f"{icon} Pujar {item.player.name} · {tag} {m(amount)}", f"b:{item.listing_id}:{amount}")
        for icon, tag, amount in affordable
    ]


def _my_bid_line(item: models.MarketItem) -> list[str]:
    return [f"📌 Tu puja pendiente: {b(m(item.my_bid))}"] if item.my_bid_id else []


def market_report(world: World, min_score: float = 8.0, rival_cash: dict[str, int] | None = None) -> str:
    """Fichajes deportivos para tu once. Un TOP de la liga sale siempre, aunque su score sea
    bajo por precio: no es una cuestión de "compensa el precio", es que es de los mejores del
    campeonato en su puesto y te lo estás perdiendo si no lo ves. Lleva también su lado
    económico (tendencia y proyección a 14 días): fichar bien y que encima suba de valor
    no son cosas distintas, es la misma decisión."""
    cards = []
    for item, trend, is_top in _market_picks(world, min_score):
        p = item.player
        star = "🌟 " if is_top else ""
        plan = _plan_for(world, item, trend, is_top)
        cards.append("\n".join([
            f"{star}{b(p.name)}  <i>{p.position} · {esc(p.team)}</i>",
            f"💰 Mínimo {b(m(plan.minimum))} · {p.avg_points:.1f} pts/partido",
            i(analysis.trend_words(trend)),
            *_my_bid_line(item),
            *_plan_lines(plan),
            *_rivals_line(world, plan.minimum, rival_cash),
        ]))
    if not cards:
        return ""
    head = f"{b('🛒 Mercado para tu once')}\n{i('Saldo disponible: ' + m(world.my_cash))}"
    return head + "\n\n" + "\n\n".join(cards)


def market_keyboard(world: World, min_score: float = 8.0) -> dict | None:
    return _keyboard([
        row for item, trend, is_top in _market_picks(world, min_score)
        for row in _bid_rows(world, item, trend, is_top)
    ])


def investment_report(world: World, top: int = 5, rival_cash: dict[str, int] | None = None) -> str:
    """Comprar barato y revender. Los TOP de la liga NO entran aquí: a esos los quieres
    para tu equipo, no para venderlos en 14 días."""
    picks = _investment_picks(world, top)
    if not picks:
        return ""
    cards = []
    for item, t in picks:
        p = item.player
        plan = _plan_for(world, item, t, False, with_ceiling=False)
        cards.append("\n".join([
            f"{b(p.name)}  <i>{esc(p.team)}</i>",
            f"💰 Mínimo {b(m(plan.minimum))}",
            i(analysis.trend_words(t)),
            *_my_bid_line(item),
            *_plan_lines(plan),
            *_rivals_line(world, plan.minimum, rival_cash),
        ]))
    head = f"{b('💹 Oportunidades de inversión')}\n{i('Comprar y revender, no para tu once')}"
    return head + "\n\n" + "\n\n".join(cards)


def buy_keyboard(world: World) -> dict | None:
    """Botones de puja de las dos listas de compra (mercado para tu once + inversión). La
    inversión es flipeo: sin botón de techo por puntos, solo mínimo y con margen."""
    rows = [row for item, t, top in _market_picks(world) for row in _bid_rows(world, item, t, top)]
    rows += [
        row for item, t in _investment_picks(world)
        for row in _bid_rows(world, item, t, False, with_ceiling=False)
    ]
    return _keyboard(rows)


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


def market_arrivals_report(world: World, store) -> tuple[str, dict | None]:
    """Lo que ha entrado nuevo al mercado de LaLiga desde el último estudio (pensado para
    correr una vez al día, justo tras el refresco diario del mercado a las 21:00) con un
    veredicto propio para cada fichaje — no una lista recortada por nota de corte, sino
    "esto es lo fresco y esto es lo que opino de cada uno". Guarda qué ids ha visto para poder
    distinguir "nuevo" de "ya lo vi ayer y sigue sin venderse". Devuelve (texto, teclado): solo
    llevan botón de puja los que el veredicto valora con 3 estrellas o más."""
    current = _biddable(world)
    seen = set(store.prefixed("market_seen:").keys())
    now_ids = {item.player.id for item in current}
    new_items = [item for item in current if item.player.id not in seen]
    for pid in seen - now_ids:
        store.set(f"market_seen:{pid}", "")
    for item in current:
        store.set(f"market_seen:{item.player.id}", "1")
    if not new_items:
        return "", None

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
    keyboard = _keyboard([
        row for stars, item, trend, *_ in rows if stars >= 3
        for row in _bid_rows(world, item, trend, item.player.id in world.league_top_ids)
    ])
    return head + "\n\n" + "\n".join(cards), keyboard


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


def _my_listings(world: World) -> list[models.MarketItem]:
    """Tus jugadores que ahora mismo están puestos a la venta en el mercado."""
    mine = {sl.player.id for sl in world.my_slots}
    return [item for item in world.market if item.player.id in mine]


def _sell_candidates(world: World, store) -> tuple[list[models.SquadSlot], dict[str, int], dict[str, analysis.Trend]]:
    buy_prices = {pid: int(v) for pid, v in store.prefixed("buy_price:").items()}
    trends = {pid: t for pid, (_, t) in world.trends.items()}
    return analysis.sell_candidates(world.my_slots, buy_prices, trends), buy_prices, trends


def sell_candidates_report(world: World, store) -> str:
    """Candidatos a poner a la venta según tendencia (ver `analysis.sell_candidates`): lleva
    bajando 3 días, sea cual sea la pérdida acumulada. Los que ya están en venta se marcan
    (siguen cumpliendo la regla, pero no hay nada que hacer con ellos)."""
    candidates, buy_prices, trends = _sell_candidates(world, store)
    if not candidates:
        return ""
    listed = {item.player.id for item in _my_listings(world)}
    cards = []
    for slot in candidates:
        p = slot.player
        buy = buy_prices[p.id]
        trend = trends[p.id]
        diff_pct = (p.market_value - buy) / buy * 100 if buy else 0
        status = " · 📤 ya en venta" if p.id in listed else ""
        cards.append(
            f"{b(p.name)} · {m(buy)} → {m(p.market_value)} ({diff_pct:+.0f}%) · "
            f"{i(analysis.trend_words(trend))}{status}"
        )
    head = f"{b('📉 Candidatos a vender')}\n{i('Tendencia bajando 3 días — no esperar a que caiga más')}"
    return head + "\n\n" + "\n".join(cards)


def sell_keyboard(world: World, store) -> dict | None:
    """Botón de vender (a valor de mercado) para cada candidato que aún no está en venta."""
    candidates, _, _ = _sell_candidates(world, store)
    listed = {item.player.id for item in _my_listings(world)}
    return _keyboard([
        _action_row(f"📤 Vender {sl.player.name} {m(sl.player.market_value)}", f"s:{sl.player.id}")
        for sl in candidates
        if sl.player.id not in listed and sl.player_team_id and sl.player.market_value
    ])


def offers_watch_report(world: World, store=None) -> tuple[str, dict | None]:
    """A quién poner a escuchar ofertas de la liga: tus jugadores con la tendencia bajando
    (d1 y d3 negativos), CON o SIN precio de compra conocido — `sell_candidates` exige ese
    precio porque compara con lo pagado, pero para escuchar ofertas no hace falta. Los que ya
    están en venta se marcan; los titulares de tu once recomendado se avisan (venderlos te deja
    un hueco en el once) pero llevan botón igual, la decisión es tuya; todos los demás también."""
    buy_prices = {pid: int(v) for pid, v in store.prefixed("buy_price:").items()} if store else {}
    trends = {pid: t for pid, (_, t) in world.trends.items()}
    listed = {item.player.id for item in _my_listings(world)}
    cands = [
        lineup.Candidate(sl.player, 0.7, lineup.expected_points(sl.player, 0.7))
        for sl in world.my_slots if sl.player.position_id != 5
    ]
    starters = {c.player.id for c in lineup.best_eleven(cands)[1]}
    falling = [
        sl for sl in world.my_slots
        if (t := trends.get(sl.player.id)) and t.d1 < 0 and t.d3 < 0 and sl.player.position_id != 5
    ]
    falling.sort(key=lambda sl: trends[sl.player.id].d3)
    if not falling:
        return "", None
    cards, rows = [], []
    for sl in falling:
        p = sl.player
        buy = buy_prices.get(p.id)
        paid = f" · pagado {m(buy)} ({(p.market_value - buy) / buy * 100:+.0f}%)" if buy else ""
        status = ""
        if p.id in listed:
            status = " · 📤 ya en venta"
        else:
            if p.id in starters:
                status = " · ⚠️ titular en tu once"
            if sl.player_team_id and p.market_value:
                rows.append(_action_row(f"📤 Vender {p.name} {m(p.market_value)}", f"s:{p.id}"))
        cards.append(f"{b(p.name)} · vale {m(p.market_value)}{paid} · {i(analysis.trend_words(trends[p.id]))}{status}")
    head = f"{b('👂 Poner a escuchar ofertas')}\n{i('Tendencia bajando: mejor vender antes de que caigan más')}"
    return head + "\n\n" + "\n".join(cards), _keyboard(rows)


def my_bids_report(world: World) -> tuple[str, dict | None]:
    """Tus pujas pendientes (la API marca la tuya en cada anuncio, ver `MarketItem.my_bid_id`),
    cada una con botón para cambiarla al mínimo de ahora / con margen si hay margen."""
    pending = [it for it in world.market if it.my_bid_id]
    if not pending:
        return "", None
    cards, rows = [], []
    for it in pending:
        trend = world.trends.get(it.player.id, (it.player, analysis.Trend(0, 0, 0)))[1]
        minimum = bid_amount(it)
        note = "" if it.my_bid == minimum else f" · el mínimo ahora es {m(minimum)}"
        cards.append(f"{b(it.player.name)} · tu puja {b(m(it.my_bid))}{note} · {i(analysis.trend_words(trend))}")
        rows += _bid_rows(world, it, trend, it.player.id in world.league_top_ids, with_ceiling=False)
    head = f"{b('📌 Tus pujas pendientes')}\n{i('Se cierran a las 21:00 en punto; el resultado tarda unos minutos')}"
    return head + "\n\n" + "\n".join(cards), _keyboard(rows)


def my_listings_report(world: World) -> tuple[str, dict | None]:
    """Tus jugadores puestos a la venta ahora, cada uno con su botón de retirarlo."""
    listings = _my_listings(world)
    if not listings:
        return "", None
    cards = [f"{b(item.player.name)} · pides {b(m(item.price))}" for item in listings]
    head = f"{b('📤 En venta ahora')}\n{i('Ofertas del juego cada ciclo de las 21:00')}"
    keyboard = _keyboard([
        _action_row(f"↩️ Retirar {item.player.name}", f"w:{item.player.id}") for item in listings
    ])
    return head + "\n\n" + "\n".join(cards), keyboard


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
        head += f"\n{i('Saldo estimado del historial de movimientos — orientativo, margen ±40M')}"
    return head + "\n\n" + "\n".join(cards)


def _all_activity(api: FantasyAPI, world: World, max_pages: int = 20) -> list[models.Activity]:
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
    return events


def initial_squad_values(api: FantasyAPI, world: World, events: list[models.Activity], store=None) -> dict[str, int]:
    """Valor de la plantilla inicial de cada mánager (manager_id -> valor), al día en que se
    unió a la liga. Es un dato fijo, así que se guarda en el `Store` (`sv0:<manager_id>`): la
    primera vez cuesta ~14 llamadas de histórico por mánager, después nada. Si a algún jugador
    no se le encuentra valor, ese mánager se omite (y el estimador cae al supuesto simple)."""
    cached = {k: int(v) for k, v in store.prefixed("sv0:").items()} if store is not None else {}
    mgr_of_team = {r.team_id: r.manager_id for r in world.standing}
    current: dict[str, set[str]] = {}
    for sl in (*world.my_slots, *world.rival_slots):
        current.setdefault(mgr_of_team.get(sl.owner_team_id, ""), set()).add(sl.player.id)
    out = dict(cached)
    for mid in mgr_of_team.values():
        if mid in out:
            continue
        joined = next((e.when for e in events if e.type_id == 9 and e.user1_id == mid and e.when), None)
        if joined is None:
            continue
        total, complete = 0, True
        for pid in analysis.initial_squad_ids(events, mid, current.get(mid, set())):
            try:
                hist = models.parse_value_history(api.market_value_history(pid))
            except Exception:
                complete = False
                break
            before = [v for d, v in hist if d <= joined]
            value = before[-1] if before else (hist[0][1] if hist else 0)
            if not value:
                complete = False
                break
            total += value
        if complete and total:
            out[mid] = total
            if store is not None:
                store.set(f"sv0:{mid}", str(total))
    return out


def estimate_rival_cash(api: FantasyAPI, world: World, store=None) -> dict[str, int]:
    """Saldo estimado de cada equipo a partir del historial completo de movimientos
    (`/activity`, paginado hacia atrás hasta que llega vacío): la API solo expone tu propio
    saldo (`world.my_cash`), así que se reconstruye el de los rivales sumando compras, ventas,
    cláusulas y bonos semanales desde el principio de temporada. Todos arrancan con el mismo
    valor total (plantilla inicial + dinero), calibrado con tu saldo real. Ver
    `analysis.estimate_cash` para el detalle y sus supuestos."""
    if world.my_cash is None:
        return {}
    events = _all_activity(api, world)
    my_manager_id = next((r.manager_id for r in world.standing if r.team_id == world.my_team_id), None)
    if not my_manager_id:
        return {}
    manager_ids = {r.team_id: r.manager_id for r in world.standing}
    initial = initial_squad_values(api, world, events, store)
    return analysis.estimate_cash(events, my_manager_id, world.my_cash, manager_ids, initial or None)


def audit_my_cash(api: FantasyAPI, world: World, store) -> str:
    """Auditor del saldo: en cada vigilancia compara cuánto ha cambiado TU saldo real desde la
    vez anterior con lo que predicen los movimientos nuevos del historial. Es la única forma de
    localizar las salidas de dinero que el historial no explica (~43-60M en total, ver
    CLAUDE.md): cada diferencia queda atribuida al tipo de movimiento que la acompaña. Devuelve
    el aviso a mandar por Telegram ("" si no pasó nada)."""
    my_id = next((r.manager_id for r in world.standing if r.team_id == world.my_team_id), None)
    if not my_id or world.my_cash is None:
        return ""
    try:
        events = models.parse_activity(api.activity(world.league_id, 0))
    except Exception:
        return ""
    newest = max((models.to_int(e.id) for e in events), default=0)
    prev_raw = store.get("cash_snap")
    store.set("cash_snap", json.dumps({"cash": world.my_cash, "last_id": newest}))
    if not prev_raw:
        return ""
    prev = json.loads(prev_raw)
    new = [e for e in events if models.to_int(e.id) > prev["last_id"] and my_id in (e.user1_id, e.user2_id) and e.amount]
    predicted = analysis.reconstruct_cash_flow(new, my_id)
    actual = world.my_cash - prev["cash"]
    residual = actual - predicted
    if not new and abs(residual) < 1000:
        return ""
    kinds = {
        models.ACTIVITY_BUY: "compra a LaLiga", models.ACTIVITY_SELL: "venta a LaLiga",
        models.ACTIVITY_CLAUSE: "cláusula", models.ACTIVITY_WEEKLY_BONUS: "bono semanal",
    }
    what = ", ".join(f"{kinds.get(e.type_id, f'tipo {e.type_id}')} {m(e.amount)}" for e in new) or "ningún movimiento"
    volume = sum(e.amount for e in new)
    if abs(residual) < 1000:
        verdict = "✅ Coincide exactamente"
    else:
        pct = f" ({residual / volume * 100:+.1f}% del importe)" if volume else ""
        verdict = f"⚠️ Diferencia {m(residual)}{pct}"
    store.set(f"audit:{newest}", json.dumps({"what": what, "actual": actual, "predicted": predicted, "residual": residual}))
    return (
        f"🔎 {b('Auditoría del saldo')}\n{esc(what)}\n"
        f"El saldo cambió {m(actual)}; el historial decía {m(predicted)}.\n{verdict}"
    )


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


def _clause_keyboard(player_id: str) -> dict:
    """Teclado con el botón de pagar cláusula. `callback_data` lleva el código de acción que
    entiende `cli.cmd_execute_action` (ver CLAUDE.md, sección de botones): "c:<player_id>"."""
    return {"inline_keyboard": [[{"text": "💳 Pagar cláusula", "callback_data": f"c:{player_id}"}]]}


def clauses_report(
    world: World, s: Settings, news: dict[str, dict] | None = None,
) -> tuple[list[tuple[str, dict | None]], list[analysis.ClauseAlert]]:
    """Un mensaje de Telegram por cláusula, no todas juntas en un tocho — así cada jugador
    lleva su propio botón de "pagar cláusula" (solo si es pagable ya; los avisos de "se libera
    en Xh" no llevan botón, todavía no se puede)."""
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
        return [(head + (("\n\n" + frozen_note) if frozen_note else ""), None)], alerts
    messages = [
        (f"🔐 {a.message}", _clause_keyboard(a.slot.player.id) if a.kind == "open_affordable" else None)
        for a in alerts
    ]
    if frozen_note:
        messages.append((frozen_note, None))
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


def speculative_clauses_report(
    world: World, s: Settings,
) -> tuple[list[tuple[str, dict | None]], list[analysis.SpeculativeAlert]]:
    """Igual que `clauses_report`: un mensaje por jugador, no todos juntos. Los candidatos que
    llegan aquí ya están filtrados a cláusula pagable ahora (ver
    `analysis.speculative_clause_candidates`), así que todos llevan botón."""
    now = datetime.now(timezone.utc)
    trends = {pid: t for pid, (_, t) in world.trends.items()}
    alerts = analysis.speculative_clause_alerts(
        world.rival_slots, world.my_cash, now, freeze=world.clause_freeze, trends=trends,
    )
    if not alerts:
        return [], alerts
    return [(f"📈 {a.message}", _clause_keyboard(a.slot.player.id)) for a in alerts], alerts


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


def flip_status_report(s: Settings, store) -> str:
    """Una línea con el estado del flipeo autónomo, para que en el informe diario se vea si está
    encendido y qué tiene entre manos (la ausencia de pujas por sí sola no dice nada)."""
    mode = {"on": "activo", "shadow": "en sombra (solo cuenta lo que haría)"}.get(s.flip_mode)
    if mode is None:
        return f"🤖 {b('Flipeo')}: apagado — {i('la variable FLIP_MODE del repositorio no está en on')}"
    pending = len(store.prefixed("flip_pending:")) if store is not None else 0
    held = store.prefixed("flip_held:") if store is not None else {}
    listed = sum(1 for v in held.values() if json.loads(v).get("listed"))
    return f"🤖 {b('Flipeo')}: {mode} · {pending} pujas pendientes · {len(held)} jugadores en cartera ({listed} a la venta)"


def report_sections(
    world: World, s: Settings, news: dict[str, dict] | None, store=None, rival_cash: dict[str, int] | None = None,
) -> list[tuple[str, dict | None]]:
    """Un mensaje por especialidad (alineación / mercado / cláusulas...), listo para Telegram.
    Cada entrada es (texto, teclado_o_None) — solo las cláusulas llevan botón. Las cláusulas
    van una por mensaje (ver `clauses_report`), no agrupadas. Omite lo que no tenga nada
    relevante que decir, para no mandar un tocho. `store` es opcional: sin él no se puede saber
    qué pagaste por tus jugadores, así que se omite la sección de corta-pérdidas. `rival_cash`
    opcional: sin él se omiten el riesgo de que te clausulen y el saldo de rivales (requieren
    el historial completo de movimientos, más caro de pedir)."""
    stamp = world.fetched_at.astimezone().strftime("%d/%m/%Y %H:%M")
    sections: list[tuple[str, dict | None]] = [
        (f"{b('⚽ Informe')}\n{i(stamp)}\n\n{lineup_report(world, news)}", None)
    ]

    buy_parts = [p for p in (market_report(world, rival_cash=rival_cash), investment_report(world, rival_cash=rival_cash)) if p]
    if buy_parts:
        sections.append(("\n\n".join(buy_parts), buy_keyboard(world)))

    mine_parts = [trends_report(world)]
    sell_buttons = None
    if store is not None:
        mine_parts += [losing_positions_report(world, store), sell_candidates_report(world, store)]
        sell_buttons = sell_keyboard(world, store)
    mine_parts = [p for p in mine_parts if p]
    if mine_parts:
        sections.append(("\n\n".join(mine_parts), sell_buttons))

    listings_text, listings_buttons = my_listings_report(world)
    if listings_text:
        sections.append((listings_text, listings_buttons))

    bids_text, bids_buttons = my_bids_report(world)
    if bids_text:
        sections.append((bids_text, bids_buttons))

    clause_messages, alerts = clauses_report(world, s, news)
    if alerts:
        sections.extend(clause_messages)

    if rival_cash:
        theft = clause_theft_report(world, rival_cash)
        if theft:
            sections.append((theft, None))
        sections.append((rivals_report(world, rival_cash), None))

    sections.append((daily_advice_report(world, rival_cash) + "\n\n" + flip_status_report(s, store), None))
    return sections


def full_report(world: World, s: Settings, news: dict[str, dict] | None) -> str:
    return "\n\n".join(text for text, _ in report_sections(world, s, news))
