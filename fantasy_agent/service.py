"""Orquestación: descarga el estado de tu liga y genera informes y alertas."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from . import analysis, lineup, models
from .api import FantasyAPI
from .config import Settings


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
    world = World(league_id, my_team_id, my_cash, standing, my_slots, rival_slots, market, team_names=team_names, fixtures=fixtures)

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


def _opportunities(world: World) -> list[analysis.Opportunity]:
    neutral = analysis.Trend(0, 0, 0)
    opps = [
        analysis.score_market_item(i, world.trends.get(i.player.id, (i.player, neutral))[1], world.my_cash)
        for i in world.market if i.player.position_id != 5
    ]
    opps.sort(key=lambda o: o.score, reverse=True)
    return opps


def market_report(world: World, top: int = 5) -> str:
    lines = [f"🛒 MERCADO PARA TU ONCE · saldo {m(world.my_cash)}"]
    for o in _opportunities(world)[:top]:
        p, it = o.item.player, o.item
        exp = it.expires.astimezone().strftime("%d/%m %H:%M") if it.expires else "?"
        tag = " 💹" if o.investment else ""
        why = o.reasons[0] if o.reasons else ""
        lines.append(f"• {p.name} ({p.position}, {p.team}) {m(it.price)}{tag} · {why} · cierra {exp}")
    return "\n".join(lines)


def investment_report(world: World, top: int = 5) -> str:
    picks = []
    for i in world.market:
        if i.player.position_id == 5:
            continue
        trend = world.trends.get(i.player.id, (i.player, analysis.Trend(0, 0, 0)))[1]
        s = analysis.score_investment(i, trend)
        if s is not None:
            picks.append((s, i, trend))
    if not picks:
        return ""
    picks.sort(key=lambda x: -x[0])
    lines = ["💹 PARA INVERTIR (comprar y revender, no para tu once)"]
    for s, i, t in picks[:top]:
        p = i.player
        exp = i.expires.astimezone().strftime("%d/%m %H:%M") if i.expires else "?"
        lines.append(f"• {p.name} ({p.team}) {m(i.price)} · {t.label} ({t.d3:+}% 3d) · cierra {exp}")
    return "\n".join(lines)


def trends_report(world: World, top: int = 3) -> str:
    risers, fallers = analysis.top_movers(world.trends, n=top)
    mine = {sl.player.id for sl in world.my_slots}
    lines = ["📊 TENDENCIAS DE VALOR (3 días)"]
    for title, rows in (("Suben", risers), ("Bajan", fallers)):
        if not rows:
            continue
        lines.append(f"{title}: " + ", ".join(f"{p.name} ({t.d3:+}%)" for p, t in rows))
    my_falling = sorted(
        [(p, t) for pid, (p, t) in world.trends.items() if pid in mine and t.d3 <= -2], key=lambda x: x[1].d3
    )[:5]
    if my_falling:
        lines.append("⚠️ Vende antes de que baje más: " + ", ".join(f"{p.name} ({t.d3:+}%)" for p, t in my_falling))
    peaking = analysis.sell_high_candidates(world.trends, mine)
    if peaking:
        lines.append("🏔️ En máximo, vende ya: " + ", ".join(f"{p.name} (+{t.d7:.0f}% 7d)" for p, t in peaking))
    return "\n".join(lines) if len(lines) > 1 else ""


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
    alerts = analysis.clause_alerts(
        world.rival_slots, world.my_slots, world.my_cash,
        datetime.now(timezone.utc), s.clause_window_hours,
    )
    if not alerts:
        return f"🔐 CLÁUSULAS: nada relevante en las próximas {s.clause_window_hours}h.", alerts
    return "🔐 CLÁUSULAS\n" + "\n".join(a.message for a in alerts), alerts


def _fixture_tag(world: World, team_id: str) -> str:
    f = world.fixtures.get(team_id)
    if not f:
        return ""
    rival = world.team_names.get(f.rival_id, f"equipo #{f.rival_id}")
    loc = "🏠" if f.home else "✈️"
    when = f.when.astimezone().strftime("%a %d/%m %H:%M") if f.when else "?"
    return f" · {loc} vs {rival} ({when})"


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
    lines = [head]
    for c in sorted(eleven, key=lambda c: c.player.position_id):
        note = f" — {c.note}" if c.note else ""
        fixture = _fixture_tag(world, c.player.team_id)
        lines.append(f"  {c.player.position} {c.player.name}: titular {c.start_prob:.0%}, xPts {c.xpts}{note}{fixture}")
    bench = [c for c in cands if c not in eleven]
    risky = [c for c in eleven if c.start_prob < 0.6]
    if risky:
        lines.append("⚠️ Dudas en el once: " + ", ".join(c.player.name for c in risky))
    if bench:
        lines.append("Banquillo: " + ", ".join(f"{c.player.name} ({c.xpts})" for c in sorted(bench, key=lambda c: -c.xpts)))
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
