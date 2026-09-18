"""Análisis puro (sin red): tendencias, oportunidades de mercado y alarmas de cláusula."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import MarketItem, Player, SquadSlot


# ---------- tendencias de valor ---------------------------------------------
@dataclass
class Trend:
    d1: float  # variación porcentual a 1 día
    d3: float
    d7: float

    @property
    def label(self) -> str:
        if self.d3 >= 3:
            return "🚀 subiendo fuerte"
        if self.d3 >= 0.5:
            return "📈 subiendo"
        if self.d3 <= -3:
            return "🔻 cayendo fuerte"
        if self.d3 <= -0.5:
            return "📉 bajando"
        return "➖ estable"


def trend_from_history(history: list[tuple[datetime, int]]) -> Trend:
    if len(history) < 2:
        return Trend(0.0, 0.0, 0.0)
    values = [v for _, v in history]
    last = values[-1]

    def pct(days: int) -> float:
        base = values[-1 - days] if len(values) > days else values[0]
        return round((last - base) / base * 100, 2) if base else 0.0

    return Trend(pct(1), pct(3), pct(7))


# ---------- oportunidades de mercado ----------------------------------------
@dataclass
class Opportunity:
    item: MarketItem
    trend: Trend
    score: float
    reasons: list[str]
    investment: bool = False


def score_market_item(item: MarketItem, trend: Trend, my_cash: int | None) -> Opportunity:
    p = item.player
    reasons: list[str] = []
    score = 0.0

    millions = max(item.price, 1) / 1_000_000
    ppm = p.points / millions  # puntos por millón
    score += min(ppm, 15) * 2
    if ppm >= 5:
        reasons.append(f"{ppm:.1f} pts por millón")

    score += max(min(trend.d3, 10), -10) * 2.5
    if trend.d3 >= 1:
        reasons.append(f"valor +{trend.d3}% en 3 días")

    if p.market_value and item.price < p.market_value:
        gap = (p.market_value - item.price) / p.market_value * 100
        score += min(gap, 20)
        reasons.append(f"precio {gap:.0f}% por debajo de su valor")

    score += min(p.avg_points, 10) * 1.5
    if p.avg_points >= 5:
        reasons.append(f"media {p.avg_points:.1f} pts")

    if not p.available:
        score -= 25
        reasons.append(f"⚠️ estado: {p.status}")
    if item.bids >= 2:
        reasons.append(f"{item.bids} pujas: habrá competencia")
    if my_cash is not None and item.price > my_cash:
        score -= 15
        reasons.append("no te llega el saldo")

    # Buena INVERSIÓN (comprar y revender) ≠ buen fichaje deportivo: importa el
    # momentum de subida y que siga barato, no cuánto puntúa.
    investment = bool(
        p.available
        and trend.d3 >= 2
        and trend.d1 >= -0.3
        and p.market_value
        and item.price <= p.market_value * 1.03
    )
    if investment:
        reasons.append("💹 buena inversión: en subida y aún infravalorado")

    return Opportunity(item, trend, round(score, 1), reasons, investment)


def score_investment(item: MarketItem, trend: Trend) -> float | None:
    """Puntuación centrada solo en potencial de revalorización (comprar barato, vender caro)."""
    p = item.player
    if not p.available or not p.market_value or item.price > p.market_value * 1.05:
        return None
    if trend.d3 < 1.5:
        return None
    return round(trend.d3 * 2 + max(trend.d1, 0) * 1.5, 1)


def sell_high_candidates(trends: dict[str, tuple[Player, Trend]], mine: set[str]) -> list[tuple[Player, Trend]]:
    """Jugadores tuyos que llevan una buena subida a 7 días pero ya se están frenando: venderlos ya."""
    out = [(p, t) for pid, (p, t) in trends.items() if pid in mine and t.d7 >= 8 and t.d1 <= 0.5]
    return sorted(out, key=lambda x: -x[1].d7)[:5]


# ---------- alarmas de cláusulas --------------------------------------------
@dataclass
class ClauseAlert:
    kind: str  # "unlock_soon" | "open_affordable" | "my_risk"
    slot: SquadSlot
    message: str

    @property
    def key(self) -> str:
        lock = self.slot.clause_locked_until.isoformat() if self.slot.clause_locked_until else "open"
        return f"{self.kind}:{self.slot.owner_team_id}:{self.slot.player.id}:{self.slot.clause}:{lock}"


def _fmt_m(amount: int) -> str:
    return f"{amount / 1_000_000:.2f}M"


def _fmt_delta(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    minutes = int(delta.total_seconds() % 3600 // 60)
    return f"{hours}h {minutes:02d}min"


def clause_alerts(
    rival_slots: list[SquadSlot],
    my_slots: list[SquadSlot],
    my_cash: int | None,
    now: datetime,
    window_hours: int = 24,
    min_quality_avg: float = 3.0,
) -> list[ClauseAlert]:
    alerts: list[ClauseAlert] = []
    window = timedelta(hours=window_hours)

    for slot in rival_slots:
        p = slot.player
        if p.position_id == 5:
            continue
        ratio = slot.clause / p.market_value if p.market_value else 0
        # Solo cláusulas que compensan: baratas respecto al valor, o jugadores top a precio razonable.
        worth = (ratio <= 1.6 and p.avg_points >= min_quality_avg) or (p.avg_points >= 6 and ratio <= 2.2)
        if not worth:
            continue
        until = slot.clause_locked_until
        if until and now < until <= now + window:
            alerts.append(ClauseAlert(
                "unlock_soon", slot,
                f"🔓 La cláusula de {p.name} ({slot.owner_name}) se desbloquea en {_fmt_delta(until - now)}: "
                f"{_fmt_m(slot.clause)} (valor {_fmt_m(p.market_value)}, media {p.avg_points:.1f})",
            ))
        elif slot.clause_open(now) and my_cash is not None and slot.clause <= my_cash:
            alerts.append(ClauseAlert(
                "open_affordable", slot,
                f"💰 Puedes pagar YA la cláusula de {p.name} ({slot.owner_name}): {_fmt_m(slot.clause)} "
                f"(x{ratio:.2f} su valor, media {p.avg_points:.1f})",
            ))

    for slot in my_slots:
        p = slot.player
        if p.position_id == 5 or not p.market_value:
            continue
        ratio = slot.clause / p.market_value
        until = slot.clause_locked_until
        soon = until is not None and now < until <= now + window
        if (slot.clause_open(now) or soon) and ratio <= 1.25 and p.avg_points >= min_quality_avg:
            when = "ya está abierta" if slot.clause_open(now) else f"se abre en {_fmt_delta(until - now)}"
            alerts.append(ClauseAlert(
                "my_risk", slot,
                f"🛡️ Riesgo de clausulazo: {p.name}, cláusula {_fmt_m(slot.clause)} (x{ratio:.2f} valor), {when}. Plantéate blindarlo.",
            ))
    return alerts


def top_movers(trends: dict[str, tuple[Player, Trend]], n: int = 5) -> tuple[list, list]:
    ordered = sorted(trends.values(), key=lambda t: t[1].d3)
    fallers = [t for t in ordered[:n] if t[1].d3 < 0]
    risers = [t for t in reversed(ordered[-n:]) if t[1].d3 > 0]
    return risers, fallers
