"""Orquestación: descarga el estado de tu liga y genera informes y alertas."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from . import analysis, lineup, models
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


def _my_avg_by_position(world: World) -> dict[int, float]:
    by_pos: dict[int, list[float]] = {}
    for sl in world.my_slots:
        if sl.player.position_id == 5:
            continue
        by_pos.setdefault(sl.player.position_id, []).append(sl.player.avg_points)
    return {pos: sum(vals) / len(vals) for pos, vals in by_pos.items() if vals}


def _upgrade_reason(p: models.Player, my_avg_by_position: dict[int, float]) -> str:
    """Motivo pensado hacia delante: lo que ya puntuó no te lo llevas tú, importa su media
    de puntos por partido comparada con lo que ya tienes en esa posición."""
    my_avg = my_avg_by_position.get(p.position_id)
    if my_avg is None:
        return f"Media {p.avg_points:.1f} pts/partido"
    if p.avg_points > my_avg + 0.3:
        return f"Media {p.avg_points:.1f} pts/partido, mejora tu media actual en {p.position} ({my_avg:.1f})"
    return f"Media {p.avg_points:.1f} pts/partido (similar a tu media actual en {p.position}: {my_avg:.1f})"


def market_report(world: World, min_score: float = 8.0) -> str:
    """Fichajes deportivos para tu once. Un TOP de la liga sale siempre, aunque su score sea
    bajo por precio: no es una cuestión de "compensa el precio", es que es de los mejores del
    campeonato en su puesto y te lo estás perdiendo si no lo ves. Lleva también su lado
    económico (tendencia y proyección a 14 días): fichar bien y que encima suba de valor
    no son cosas distintas, es la misma decisión."""
    my_avg_by_position = _my_avg_by_position(world)
    cards = []
    for o in _opportunities(world):
        p = o.item.player
        is_top = p.id in world.league_top_ids
        if o.score < min_score and not is_top:
            continue
        motivo = _upgrade_reason(p, my_avg_by_position)
        if is_top:
            motivo = "🌟 De los mejores de LaLiga en su posición. " + motivo
        trend = world.trends.get(p.id, (p, analysis.Trend(0, 0, 0)))[1]
        proj = analysis.project_value(o.item.price, trend)
        gain_pct = (proj - o.item.price) / o.item.price * 100 if o.item.price else 0
        cards.append(
            f"{p.name}\n"
            f"Posición: {p.position} · Equipo: {p.team}\n"
            f"Precio: {m(o.item.price)}\n"
            f"Motivo: {motivo}\n"
            f"Valor: {trend.label} ({trend.d7:+}% en 7 días) · a 14 días ~{m(proj)} ({gain_pct:+.0f}%)"
        )
    if not cards:
        return ""
    return "🛒 MERCADO PARA TU ONCE · saldo " + m(world.my_cash) + "\n\n" + "\n\n".join(cards)


def investment_report(world: World, top: int = 5) -> str:
    """Comprar barato y revender. Los TOP de la liga NO entran aquí: a esos los quieres
    para tu equipo, no para venderlos en 14 días."""
    picks = []
    for i in _biddable(world):
        if i.player.id in world.league_top_ids:
            continue
        trend = world.trends.get(i.player.id, (i.player, analysis.Trend(0, 0, 0)))[1]
        s = analysis.score_investment(i, trend)
        if s is not None:
            picks.append((s, i, trend))
    if not picks:
        return ""
    picks.sort(key=lambda x: -x[0])
    cards = []
    for s, i, t in picks[:top]:
        p = i.player
        proj = analysis.project_value(i.price, t)
        gain_pct = (proj - i.price) / i.price * 100 if i.price else 0
        cards.append(
            f"{p.name}\n"
            f"Equipo: {p.team}\n"
            f"Precio ahora: {m(i.price)}\n"
            f"Tendencia: {t.label} ({t.d7:+}% en 7 días)\n"
            f"Al comprarlo se blinda 14 días. Si sigue este ritmo, en 14 días: ~{m(proj)} ({gain_pct:+.0f}%)\n"
            f"¿Pujar? (próximamente podrás confirmarlo aquí mismo)"
        )
    return "💹 POSIBILIDADES DE INVERSIÓN (comprar y revender, no para tu once)\n\n" + "\n\n".join(cards)


def trends_report(world: World) -> str:
    mine = {sl.player.id for sl in world.my_slots}
    lines = []
    my_falling = sorted(
        [(p, t) for pid, (p, t) in world.trends.items() if pid in mine and t.d3 <= -2], key=lambda x: x[1].d3
    )[:5]
    if my_falling:
        lines.append("⚠️ Tuyos, véndelos antes de que bajen más:\n" + "\n".join(f"{p.name} ({t.d3:+}% en 3 días)" for p, t in my_falling))
    peaking = analysis.sell_high_candidates(world.trends, mine)
    if peaking:
        lines.append("🏔️ Tuyos en máximo, véndelos ya:\n" + "\n".join(f"{p.name} (+{t.d7:.0f}% en 7 días)" for p, t in peaking))
    if not lines:
        return ""
    return "📊 TUS JUGADORES: vender o mantener\n\n" + "\n\n".join(lines)


def rivals_report(world: World) -> str:
    lines = ["👥 RIVALES"]
    by_owner: dict[str, list[models.SquadSlot]] = {}
    for sl in world.rival_slots:
        by_owner.setdefault(sl.owner_team_id, []).append(sl)
    now = datetime.now(timezone.utc)
    for row in sorted(world.standing, key=lambda r: r.points, reverse=True):
        if row.team_id == world.my_team_id:
            lines.append(f"• TÚ ({row.manager_name}): {row.points} pts · valor {m(row.team_value)}")
            continue
        slots = sorted(by_owner.get(row.team_id, []), key=lambda s: s.player.market_value, reverse=True)
        stars = ", ".join(s.player.name for s in slots[:3])
        open_cl = sum(1 for s in slots if s.clause_open(now))
        lines.append(f"• {row.manager_name}: {row.points} pts · valor {m(row.team_value)} · cláusulas abiertas {open_cl} · top: {stars}")
    return "\n".join(lines)


def clauses_report(world: World, s: Settings) -> tuple[str, list[analysis.ClauseAlert]]:
    now = datetime.now(timezone.utc)
    alerts = analysis.clause_alerts(
        world.rival_slots, world.my_cash, now, s.clause_window_hours,
        freeze=world.clause_freeze,
    )
    frozen_note = ""
    if world.clause_freeze and world.clause_freeze[0] <= now < world.clause_freeze[1]:
        until = world.clause_freeze[1].astimezone().strftime("%d/%m %H:%M")
        frozen_note = f"\n\n⏸️ Cláusulas congeladas hasta las {until} (empieza la jornada)."
    if not alerts:
        return f"🔐 CLÁUSULAS: nada relevante en las próximas {s.clause_window_hours}h.{frozen_note}", alerts
    return "🔐 CLÁUSULAS\n\n" + "\n\n".join(a.message for a in alerts) + frozen_note, alerts


def _rival_name(world: World, team_id: str) -> str:
    f = world.fixtures.get(team_id)
    if not f:
        return "?"
    rival = world.team_names.get(f.rival_id, f"equipo #{f.rival_id}")
    icon = "🏠" if f.home else "✈️"
    return f"{icon} {rival}"


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
        return "🧩 ONCE: no hay jugadores suficientes en la plantilla."
    head = f"🧩 ONCE RECOMENDADO {'-'.join(map(str, formation))} · {total} pts esperados"
    if len(eleven) < 11:
        head += f" · ⚠️ solo {len(eleven)} jugadores válidos"
    lines = [head, ""]
    group_names = {1: "PORTERO", 2: "DEFENSAS", 3: "CENTROCAMPISTAS", 4: "DELANTEROS"}
    for pos_id in (1, 2, 3, 4):
        group = sorted((c for c in eleven if c.player.position_id == pos_id), key=lambda c: -c.xpts)
        if not group:
            continue
        lines.append(group_names[pos_id])
        for c in group:
            lines.append(
                f"{c.player.name}\n"
                f"Titularidad: {c.start_prob:.0%}\n"
                f"Puntos esperados: {c.xpts}\n"
                f"Rival: {_rival_name(world, c.player.team_id)}\n"
            )
    risky = [c for c in eleven if c.start_prob < 0.6]
    if risky:
        lines.append("⚠️ Dudas en el once:")
        for c in risky:
            why = c.note or f"probabilidad de titularidad baja ({c.start_prob:.0%})"
            lines.append(f"{c.player.name}: {why}")
    if news is None:
        lines.append("(Sin noticias: probabilidad de titularidad por defecto 70%. Usa --news para afinarlo.)")
    return "\n".join(lines)


def report_sections(world: World, s: Settings, news: dict[str, dict] | None) -> list[str]:
    """Un mensaje por especialidad (alineación / mercado / cláusulas), listo para Telegram.
    Omite lo que no tenga nada relevante que decir, para no mandar un tocho."""
    stamp = world.fetched_at.astimezone().strftime("%d/%m/%Y %H:%M")
    sections = [f"⚽ INFORME · {stamp}\n\n{lineup_report(world, news)}"]

    market_parts = [p for p in (market_report(world), investment_report(world), trends_report(world)) if p]
    if market_parts:
        sections.append("\n\n".join(market_parts))

    clauses_text, alerts = clauses_report(world, s)
    if alerts:
        sections.append(clauses_text)

    return sections


def full_report(world: World, s: Settings, news: dict[str, dict] | None) -> str:
    return "\n\n".join(report_sections(world, s, news))
