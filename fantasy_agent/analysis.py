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


def score_market_item(item: MarketItem, trend: Trend, my_cash: int | None) -> Opportunity:
    """Puntúa como FICHAJE (para tu once): importa lo que va a rendir de aquí en adelante
    (media de puntos por partido), no lo que ya sumó — esos puntos ya no te los llevas."""
    p = item.player
    reasons: list[str] = []
    score = 0.0

    millions = max(item.price, 1) / 1_000_000
    ppm = p.avg_points / millions  # media de puntos por partido, por millón invertido
    score += min(ppm, 15) * 2

    if p.market_value and item.price < p.market_value:
        gap = (p.market_value - item.price) / p.market_value * 100
        score += min(gap, 20)

    score += min(p.avg_points, 10) * 1.5

    if not p.available:
        score -= 25
        reasons.append(f"⚠️ estado: {p.status}")
    if my_cash is not None and item.price > my_cash:
        score -= 15
        reasons.append("no te llega el saldo")

    return Opportunity(item, trend, round(score, 1), reasons)


def market_verdict(
    item: MarketItem,
    trend: Trend,
    my_cash: int | None,
    is_top: bool,
) -> tuple[int, str, list[str]]:
    """Igual que `clause_verdict` pero para una entrada nueva del mercado de LaLiga: 1-4
    estrellas con los motivos explicados, para el "estudio de viabilidad" de cada fichaje
    recién salido, no solo una lista filtrada por nota de corte."""
    p = item.player
    points = 0.0
    reasons: list[str] = []

    millions = max(item.price, 1) / 1_000_000
    ppm = p.avg_points / millions
    if ppm >= 1.2:
        points += 2.0
        reasons.append(f"Muy rentable en puntos por millón ({p.avg_points:.1f} pts/partido a {_fmt_m(item.price)})")
    elif ppm >= 0.7:
        points += 1.0
        reasons.append(f"Rentable en puntos por millón ({p.avg_points:.1f} pts/partido)")
    else:
        reasons.append(f"Rendimiento ajustado al precio ({p.avg_points:.1f} pts/partido)")

    if p.market_value and item.price < p.market_value:
        gap_pct = (p.market_value - item.price) / p.market_value * 100
        if gap_pct >= 15:
            points += 1.5
            reasons.append(f"Precio muy por debajo de mercado (-{gap_pct:.0f}%)")
        elif gap_pct >= 5:
            points += 0.7
            reasons.append(f"Precio por debajo de mercado (-{gap_pct:.0f}%)")
    elif p.market_value and item.price > p.market_value * 1.05:
        over_pct = (item.price - p.market_value) / p.market_value * 100
        points -= 1.0
        reasons.append(f"Precio por encima de mercado (+{over_pct:.0f}%)")

    if trend.d7 >= 5:
        points += 1.0
        reasons.append(f"En racha: su valor sube ({trend.d7:+.1f}% en 7 días)")
    elif trend.d7 <= -5:
        points -= 0.5
        reasons.append(f"Su valor está cayendo ({trend.d7:+.1f}% en 7 días)")

    if not p.available:
        points -= 3.0
        reasons.append(f"⚠️ estado: {p.status}")

    if my_cash is not None and item.price > my_cash:
        points -= 2.0
        reasons.append("no te llega el saldo ahora mismo")

    if is_top:
        points += 1.5
        reasons.append("🌟 de los mejores de LaLiga en su posición")

    if points >= 4.5:
        stars, label = 4, "🔥 Fichaje claro"
    elif points >= 2.5:
        stars, label = 3, "✅ Buena opción"
    elif points >= 1.0:
        stars, label = 2, "🤔 Con dudas"
    else:
        stars, label = 1, "🚫 Paso"
    return stars, label, reasons


def score_investment(item: MarketItem, trend: Trend) -> float | None:
    """Puntuación centrada solo en potencial de revalorización (comprar barato, vender caro)."""
    p = item.player
    if not p.available or not p.market_value or item.price > p.market_value * 1.05:
        return None
    if trend.d3 < 1.5:
        return None
    return round(trend.d3 * 2 + max(trend.d1, 0) * 1.5, 1)


def project_value(current: int, trend: Trend, days: int = 14) -> int:
    """Cuánto podría valer dentro de `days` días si sigue el ritmo reciente.
    Por defecto 14 días: es lo que tarda en liberarse la cláusula tras comprar a alguien,
    así que ese es tu horizonte real para poder revenderlo. Usa el ritmo a 7 días (más
    estable que el de 3) para no disparar la proyección por un pico de un par de días."""
    daily_rate = trend.d7 / 7 if trend.d7 else trend.d3 / 3
    return round(current * (1 + daily_rate / 100) ** days)


def sell_high_candidates(trends: dict[str, tuple[Player, Trend]], mine: set[str]) -> list[tuple[Player, Trend]]:
    """Jugadores tuyos que llevan una buena subida a 7 días pero ya se están frenando: venderlos ya."""
    out = [(p, t) for pid, (p, t) in trends.items() if pid in mine and t.d7 >= 8 and t.d1 <= 0.5]
    return sorted(out, key=lambda x: -x[1].d7)[:5]


# ---------- alarmas de cláusulas --------------------------------------------
# Umbrales para avisar más de una vez de la misma cláusula según se acerca su liberación
# (24h, 6h, 1h): cada uno dispara una alerta nueva la primera vez que se cruza.
UNLOCK_ALERT_TIERS_HOURS = (24, 6, 1)


@dataclass
class ClauseAlert:
    kind: str  # "unlock_soon" | "open_affordable" | "my_risk"
    slot: SquadSlot
    message: str
    tier: str = ""  # p.ej. "6h": para que la misma cláusula pueda avisar varias veces al acercarse
    stars: int = 0  # veredicto 1-4, ver `clause_verdict`

    @property
    def key(self) -> str:
        lock = self.slot.clause_locked_until.isoformat() if self.slot.clause_locked_until else "open"
        return f"{self.kind}:{self.tier}:{self.slot.owner_team_id}:{self.slot.player.id}:{self.slot.clause}:{lock}"


def _fmt_m(amount: int) -> str:
    return f"{amount / 1_000_000:.2f}M"


def _fmt_delta(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    minutes = int(delta.total_seconds() % 3600 // 60)
    return f"{hours}h {minutes:02d}min"


def _fmt_when(until: datetime, now: datetime) -> str:
    """Cuenta atrás si es pronto; fecha y hora exactas si falta mucho (Xh no se lee bien a 13 días)."""
    delta = until - now
    if delta <= timedelta(hours=48):
        return f"en {_fmt_delta(delta)}"
    return f"el {until.astimezone().strftime('%d/%m %H:%M')}"


def _clause_filter(
    rival_slots: list[SquadSlot],
    now: datetime,
    window_hours: int,
    min_quality_avg: float,
    max_ratio: float,
) -> list[tuple[SquadSlot, float, str]]:
    """Filtro económico barato (ratio cláusula/mercado, calidad, ventana de tiempo), sin mirar
    tendencia ni noticias todavía. Lo reutilizan `clause_candidate_players` (para saber a quién
    merece la pena pedirle datos extra) y `clause_alerts` (para construir el veredicto)."""
    tiers = [h for h in UNLOCK_ALERT_TIERS_HOURS if h <= window_hours] or [window_hours]
    out = []
    for slot in rival_slots:
        p = slot.player
        if p.position_id == 5 or not p.market_value:
            continue
        ratio = slot.clause / p.market_value
        if ratio > max_ratio or p.avg_points < min_quality_avg:
            continue
        until = slot.clause_locked_until
        tier = ""
        if until and until > now:
            hours_left = (until - now).total_seconds() / 3600
            matched = next((h for h in sorted(tiers) if hours_left <= h), None)
            if matched is None:
                continue
            tier = f"{matched}h"
        out.append((slot, ratio, tier))
    return out


def clause_candidate_players(
    rival_slots: list[SquadSlot],
    now: datetime,
    window_hours: int = 24,
    min_quality_avg: float = 3.0,
    max_ratio: float = 1.2,
) -> list[Player]:
    """Quién podría acabar en una alerta de cláusula, mirando solo el filtro barato. El
    llamador usa esto para pedir tendencia de valor y noticias (titularidad/lesión) SOLO de
    estos jugadores, no de los 30+ rivales de la liga."""
    seen: dict[str, Player] = {}
    for slot, _ratio, _tier in _clause_filter(rival_slots, now, window_hours, min_quality_avg, max_ratio):
        seen[slot.player.id] = slot.player
    return list(seen.values())


def clause_verdict(
    p: Player,
    ratio: float,
    trend: Trend | None,
    news: dict | None,
) -> tuple[int, str, list[str]]:
    """El criterio propio del bot sobre una cláusula: no un ratio suelto, sino un veredicto de
    1 a 4 estrellas que junta precio, rendimiento, racha de valor, noticias reales del día
    (lesión/duda/titular casi seguro) y potencial de reventa a 14 días — con los motivos
    explicados para que se pueda revisar el razonamiento, no solo el número."""
    trend = trend or Trend(0.0, 0.0, 0.0)
    points = 0.0
    reasons: list[str] = []

    if ratio <= 0.85:
        points += 2.0
        reasons.append(f"Cláusula muy por debajo de mercado (x{ratio:.2f})")
    elif ratio <= 1.0:
        points += 1.5
        reasons.append(f"Cláusula por debajo de mercado (x{ratio:.2f})")
    elif ratio <= 1.1:
        points += 1.0
        reasons.append(f"Cláusula ajustada al valor de mercado (x{ratio:.2f})")
    else:
        points += 0.5
        reasons.append(f"Cláusula algo cara pero aún razonable (x{ratio:.2f})")

    if p.avg_points >= 7:
        points += 1.5
        reasons.append(f"Rendimiento muy alto ({p.avg_points:.1f} pts/partido de media)")
    elif p.avg_points >= 5:
        points += 1.0
        reasons.append(f"Buen rendimiento ({p.avg_points:.1f} pts/partido de media)")
    else:
        reasons.append(f"Rendimiento correcto ({p.avg_points:.1f} pts/partido de media)")

    if trend.d7 >= 5:
        points += 1.5
        reasons.append(f"En racha: su valor sube fuerte ({trend.d7:+.1f}% en 7 días)")
    elif trend.d7 >= 1:
        points += 0.7
        reasons.append(f"Tendencia de valor positiva ({trend.d7:+.1f}% en 7 días)")
    elif trend.d7 <= -5:
        points -= 1.0
        reasons.append(f"Ojo: su valor está cayendo ({trend.d7:+.1f}% en 7 días)")

    if news:
        status = news.get("status")
        note = news.get("note", "")
        prob = news.get("start_probability")
        if status == "lesionado":
            points -= 3.0
            reasons.append(f"⚠️ Lesionado según noticias de hoy: {note}")
        elif status == "duda":
            points -= 1.5
            reasons.append(f"⚠️ Duda para el once según noticias: {note}")
        elif prob is not None and prob >= 75:
            points += 1.0
            reasons.append(f"Buenas noticias: titular casi seguro ({prob}%)")
    else:
        reasons.append("Sin noticias de hoy contrastadas para este jugador")

    if p.market_value:
        proj = project_value(p.market_value, trend, 14)
        gain_pct = (proj - p.market_value) / p.market_value * 100
        if gain_pct >= 10:
            points += 1.0
            reasons.append(f"Buen potencial de reventa: ~{_fmt_m(proj)} en 14 días ({gain_pct:+.0f}%)")
        elif gain_pct >= 3:
            points += 0.5
            reasons.append(f"Algo de potencial de reventa a 14 días (~{gain_pct:+.0f}%)")

    if points >= 5.5:
        stars, label = 4, "🔥 Clausúrale ya"
    elif points >= 3.5:
        stars, label = 3, "✅ Buena oportunidad"
    elif points >= 2.0:
        stars, label = 2, "🤔 Con reservas"
    else:
        stars, label = 1, "🚫 No compensa"
    return stars, label, reasons


def clause_alerts(
    rival_slots: list[SquadSlot],
    my_cash: int | None,
    now: datetime,
    window_hours: int = 24,
    min_quality_avg: float = 3.0,
    max_ratio: float = 1.2,
    freeze: tuple[datetime, datetime] | None = None,
    trends: dict[str, Trend] | None = None,
    news: dict[str, dict] | None = None,
) -> list[ClauseAlert]:
    """Solo cláusulas "lógicas" (ver `_clause_filter`). Solo mira rivales — lo tuyo (blindar,
    arriesgarte) lo decides tú, no hace falta que te lo repitamos. `freeze` es la ventana en la
    que la propia liga bloquea TODAS las cláusulas (24h antes del primer partido de la jornada):
    un jugador "libre" según su cláusula puede seguir sin ser pagable si caemos dentro de esa
    ventana. `trends`/`news` son opcionales (id de jugador -> Trend / info de titularidad) y
    alimentan el veredicto de `clause_verdict`; sin ellos, el veredicto se basa solo en precio
    y rendimiento."""
    alerts: list[ClauseAlert] = []
    frozen_now = bool(freeze and freeze[0] <= now < freeze[1])
    trends = trends or {}
    news = news or {}

    for slot, ratio, tier in _clause_filter(rival_slots, now, window_hours, min_quality_avg, max_ratio):
        p = slot.player
        until = slot.clause_locked_until
        if until and until > now:
            estado = f"se libera {_fmt_when(until, now)}"
        elif slot.clause_open(now) and not frozen_now and (my_cash is None or slot.clause <= my_cash):
            estado = "pagable ya"
        else:
            continue

        stars, label, reasons = clause_verdict(p, ratio, trends.get(p.id), news.get(p.id))
        stars_str = "★" * stars + "☆" * (4 - stars)
        motivos = "\n".join(f"- {r}" for r in reasons)
        alerts.append(ClauseAlert(
            "open_affordable" if slot.clause_open(now) else "unlock_soon", slot,
            f"{p.name}\n"
            f"Dueño: {slot.owner_name}\n"
            f"{stars_str} {label}\n"
            f"Cláusula: {_fmt_m(slot.clause)} (mercado: {_fmt_m(p.market_value)}, x{ratio:.2f})\n"
            f"Estado: {estado}\n"
            f"Por qué:\n{motivos}",
            tier=tier,
            stars=stars,
        ))

    # Primero las que ya puedes pagar; entre iguales, mejor veredicto primero; luego, de más
    # cerca a más lejos de liberarse.
    alerts.sort(key=lambda a: (a.kind != "open_affordable", -a.stars, a.slot.clause_locked_until or now))
    return alerts


def loss_cut_candidates(
    my_slots: list[SquadSlot],
    buy_prices: dict[str, int],
    trends: dict[str, Trend],
    min_loss_pct: float = 8.0,
) -> list[tuple[SquadSlot, int, float]]:
    """Jugadores tuyos cuyo valor de mercado ha caído por debajo de lo que pagaste — una
    pérdida real si los vendieras ahora, no una simple tendencia de mercado. Solo con precio de
    compra conocido (registrado por `service.my_transactions` al comprarlos). Si ya está
    recuperando (d3 > 1%) no se avisa todavía: dale margen antes de decir "corta pérdidas"."""
    out = []
    for slot in my_slots:
        buy = buy_prices.get(slot.player.id)
        if not buy or not slot.player.market_value:
            continue
        loss_pct = (buy - slot.player.market_value) / buy * 100
        if loss_pct < min_loss_pct:
            continue
        trend = trends.get(slot.player.id)
        if trend and trend.d3 > 1:
            continue
        out.append((slot, buy, loss_pct))
    out.sort(key=lambda x: -x[2])
    return out


def top_movers(trends: dict[str, tuple[Player, Trend]], n: int = 5) -> tuple[list, list]:
    ordered = sorted(trends.values(), key=lambda t: t[1].d3)
    fallers = [t for t in ordered[:n] if t[1].d3 < 0]
    risers = [t for t in reversed(ordered[-n:]) if t[1].d3 > 0]
    return risers, fallers
