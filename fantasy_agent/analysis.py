"""Análisis puro (sin red): tendencias, oportunidades de mercado y alarmas de cláusula."""
from __future__ import annotations

import html as _html
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .lineup import Candidate, best_eleven
from .models import (
    ACTIVITY_BUY,
    ACTIVITY_CLAUSE,
    ACTIVITY_SELL,
    ACTIVITY_WEEKLY_BONUS,
    Activity,
    MarketItem,
    Player,
    SquadSlot,
)


def esc(text) -> str:
    """Escapa para Telegram HTML (solo & < > cuentan); todo texto que venga de la API o de
    scraping pasa por aquí antes de insertarse en un mensaje con `parse_mode=HTML`."""
    return _html.escape(str(text), quote=False)


def b(text) -> str:
    return f"<b>{esc(text)}</b>"


def i(text) -> str:
    return f"<i>{esc(text)}</i>"


# ---------- tendencias de valor ---------------------------------------------
@dataclass
class Trend:
    d1: float  # variación porcentual a 1 día
    d3: float
    d7: float

    @property
    def cooling(self) -> bool:
        """Subió a 7 días pero el corto plazo (3 días) ya no lo confirma: la "racha" que
        cuentan los 7 días es vieja, ahora mismo se está frenando o revirtiendo."""
        return self.d7 >= 5 and self.d3 < 1

    @property
    def recovering(self) -> bool:
        """Al revés: cayó a 7 días pero el corto plazo ya está remontando."""
        return self.d7 <= -5 and self.d3 > 1

def trend_words(t: "Trend") -> str:
    """Frase corta en palabras llanas del ritmo reciente — solo día y 3 días, sin la ventana
    de 7 días: para decidir si algo está para flipear (comprar y revender en pocos días)
    importa el ritmo de ahora mismo, no una media más lenta de toda la semana."""
    if abs(t.d3) < 0.5 and abs(t.d1) < 0.5:
        return "➖ estable estos días"
    if t.d3 >= 3:
        arrow = "🚀"
    elif t.d3 > 0:
        arrow = "📈"
    elif t.d3 <= -3:
        arrow = "🔻"
    else:
        arrow = "📉"
    verbo = "sube" if t.d3 >= 0 else "baja"
    return f"{arrow} {verbo} un {abs(t.d1):.1f}% al día, un {abs(t.d3):.1f}% en 3 días"


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


def bid_ceiling(avg_points: float, alternative_ppm: float, is_top: bool) -> int:
    """Techo de puja para un jugador que quieres para TU ONCE (puntos, no reventa): el precio
    máximo a partir del cual, aunque ganes la puja ciega, te habría salido mejor gastar ese
    dinero en la mejor alternativa realista disponible ahora mismo (`alternative_ppm`: puntos
    por millón de esa alternativa — normalmente la mediana del mercado en su posición). Pasado
    ese precio, estás pagando más por punto que lo que cuesta un punto ahora mismo en el
    mercado. Los TOP de liga (solo 3 por posición en TODA LaLiga) llevan una prima del 30%:
    no son "buenos", son escasos e insustituibles, y eso vale dinero aparte de sus puntos.
    Como las pujas son ciegas (no se ve lo que oferta nadie más — confirmado en el FAQ oficial
    del juego), este techo es tu única defensa real contra pagar de más: o pujas hasta aquí
    sabiendo que sigue siendo buen negocio, o no merece la pena arriesgar más."""
    if alternative_ppm <= 0 or avg_points <= 0:
        return 0
    ceiling = avg_points / alternative_ppm * 1_000_000
    if is_top:
        ceiling *= 1.3
    return round(ceiling)


@dataclass
class BidPlan:
    minimum: int
    margin: int | None  # puja "con margen": más que el mínimo porque la subida lo justifica
    expected: int | None  # valor esperado a 3 días que justifica ese margen
    ceiling: int | None  # máximo lógico para "lo quiero sí o sí"


def bid_plan(
    minimum: int,
    market_value: int,
    trend: Trend,
    avg_points: float,
    alternative_ppm: float,
    is_top: bool,
) -> BidPlan:
    """Tres cantidades para el mismo anuncio, porque las pujas son ciegas y no juega solo el
    usuario: pujar el mínimo es barato pero pierde contra cualquiera que ponga algo más.
    - `margin`: si sube (d3 > 0) y se espera que valga más de lo que cuesta el mínimo (>2%),
      puja el mínimo + la MITAD de esa ganancia esperada: la ventaja sobre el resto sale de
      regalar solo parte del beneficio, y aunque ganes sigues quedándote con la otra mitad. El
      valor esperado proyecta 3 días con el ritmo diario MÁS BAJO entre el de hoy (d1) y el
      medio de los últimos 3 (d3/3): una subida que se está frenando (Pablo García, 2026-09-21:
      +7.5%, +6.7%... ayer +1.8%, hoy +0.18%) no debe proyectarse con la media de una racha que
      ya pasó; no se mira la ventana de 7.
    - `ceiling`: el techo por puntos (`bid_ceiling`) para "lo quiero sí o sí" — solo si queda
      claramente por encima de la puja anterior, si no no aporta nada.
    Las cantidades se redondean a miles hacia arriba (no por debajo del mínimo)."""
    margin = expected = None
    daily = min(trend.d1, trend.d3 / 3)
    if daily > 0 and market_value:
        expected = round(market_value * (1 + daily / 100) ** 3)
        gain = expected - minimum
        if gain > minimum * 0.02:
            margin = -(-round(minimum + gain * 0.5) // 1000) * 1000
    ceiling = bid_ceiling(avg_points, alternative_ppm, is_top) or None
    if ceiling is not None:
        # Nunca más del doble del mínimo: con pocas referencias de mercado el techo por puntos
        # se dispara (un medio de 0.70M salió con techo de 12M) y eso ya no es una puja lógica.
        ceiling = -(-min(ceiling, minimum * 2) // 1000) * 1000
        if ceiling <= (margin or minimum) * 1.01:
            ceiling = None
    return BidPlan(minimum, margin, expected if margin else None, ceiling)


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

    if trend.cooling:
        points += 0.2
        reasons.append(f"Venía subiendo pero se frena ahora: {trend.d1:+.1f}% al día, {trend.d3:+.1f}% en 3 días")
    elif trend.d7 >= 5:
        points += 1.0
        reasons.append(f"En racha sostenida: sube {trend.d1:+.1f}% al día, {trend.d3:+.1f}% en 3 días")
    elif trend.recovering:
        reasons.append(f"Venía cayendo pero ya recupera: {trend.d3:+.1f}% en 3 días")
    elif trend.d7 <= -5:
        points -= 0.5
        reasons.append(f"Su valor está cayendo: {trend.d1:+.1f}% al día, {trend.d3:+.1f}% en 3 días")

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
    estable que el de 3) para no disparar la proyección por un pico de un par de días —
    salvo que la tendencia ya haya cambiado de sentido (`cooling`/`recovering`), porque
    entonces el ritmo de 7 días describe una racha que ya terminó y el de 3 es lo vigente."""
    if trend.cooling or trend.recovering:
        daily_rate = trend.d3 / 3
    else:
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
    price: int,
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

    if trend.cooling:
        points += 0.3
        reasons.append(
            f"Venía subiendo pero se frena ahora ({trend.d1:+.1f}% al día, {trend.d3:+.1f}% en 3 días): "
            "la racha ya es vieja, no pagues de más por ella"
        )
    elif trend.d7 >= 5:
        points += 1.5
        reasons.append(f"En racha sostenida: sube {trend.d1:+.1f}% al día, {trend.d3:+.1f}% en 3 días")
    elif trend.d7 >= 1:
        points += 0.7
        reasons.append(f"Tendencia a medio plazo positiva — ahora mismo: {trend.d1:+.1f}% al día, {trend.d3:+.1f}% en 3 días")
    elif trend.recovering:
        reasons.append(f"Venía cayendo pero ya recupera: {trend.d3:+.1f}% en 3 días")
    elif trend.d7 <= -5:
        points -= 1.0
        reasons.append(f"Ojo: su valor está cayendo ({trend.d1:+.1f}% al día, {trend.d3:+.1f}% en 3 días)")

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

    if p.market_value and price:
        # Ojo: la proyección parte del valor de mercado (lo que de verdad va a evolucionar),
        # pero la ganancia se mide contra `price` — lo que realmente pagas por la cláusula—,
        # no contra el valor de mercado. Si pagas por encima de mercado (ratio > 1) esto da
        # una ganancia real menor que comparar contra el valor de mercado; si es una ganga
        # (ratio < 1), da una ganancia mayor. Es la pregunta real: "¿recupero lo invertido?".
        proj = project_value(p.market_value, trend, 14)
        gain_pct = (proj - price) / price * 100
        if gain_pct >= 10:
            points += 1.0
            reasons.append(f"Rentable de verdad: pagas {_fmt_m(price)}, ~{_fmt_m(proj)} en 14 días ({gain_pct:+.0f}% sobre lo pagado)")
        elif gain_pct >= 3:
            points += 0.5
            reasons.append(f"Algo de margen sobre lo pagado a 14 días (~{gain_pct:+.0f}%)")
        elif gain_pct < 0:
            points -= 0.5
            reasons.append(f"⚠️ Al ritmo actual no recuperarías lo pagado en 14 días (~{_fmt_m(proj)} vs {_fmt_m(price)} pagados)")

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

        stars, label, reasons = clause_verdict(p, slot.clause, ratio, trends.get(p.id), news.get(p.id))
        stars_str = "🟢" * stars + "⚪" * (4 - stars)
        detail = " · ".join(esc(r) for r in reasons)
        alerts.append(ClauseAlert(
            "open_affordable" if slot.clause_open(now) else "unlock_soon", slot,
            f"{b(p.name)} <i>{esc(slot.owner_name)}</i> · {stars_str} {label} · "
            f"{b(_fmt_m(slot.clause))} (x{ratio:.2f}) · {estado}\n"
            f"{i(detail)}",
            tier=tier,
            stars=stars,
        ))

    # Primero las que ya puedes pagar; entre iguales, mejor veredicto primero; luego, de más
    # cerca a más lejos de liberarse.
    alerts.sort(key=lambda a: (a.kind != "open_affordable", -a.stars, a.slot.clause_locked_until or now))
    return alerts


# ---------- cláusulas especulativas (rompen el filtro de precio, pero racha muy fuerte) -----
@dataclass
class SpeculativeAlert:
    slot: SquadSlot
    message: str

    @property
    def key(self) -> str:
        return f"speculative:{self.slot.owner_team_id}:{self.slot.player.id}:{self.slot.clause}"


def speculative_clause_candidates(
    rival_slots: list[SquadSlot],
    now: datetime,
    min_quality_avg: float = 3.0,
    max_ratio: float = 1.2,
    ratio_ceiling: float = 3.0,
) -> list[Player]:
    """Rivales que NO pasan el filtro "lógico" (cláusula > `max_ratio` veces su valor de
    mercado) pero podrían compensar igual si la subida de valor es lo bastante fuerte y
    sostenida — para pedirles tendencia/noticias sin tener que hacerlo con cualquier cláusula
    cara de la liga (`ratio_ceiling` acota lo disparatado: pagar 3x mercado no lo salva
    ninguna racha)."""
    seen: dict[str, Player] = {}
    for slot in rival_slots:
        p = slot.player
        if p.position_id == 5 or not p.market_value or p.avg_points < min_quality_avg:
            continue
        ratio = slot.clause / p.market_value
        if ratio <= max_ratio or ratio > ratio_ceiling:
            continue
        if not slot.clause_open(now):
            continue
        seen[p.id] = p
    return list(seen.values())


def _breakeven_days(current: int, target: int, daily_rate_pct: float, horizon: int = 14) -> int | None:
    """Cuántos días (enteros, redondeando hacia arriba) tardaría `current` en alcanzar
    `target` creciendo un `daily_rate_pct` cada día. None si el ritmo no es positivo o se
    pasa del horizonte."""
    if daily_rate_pct <= 0 or target <= current:
        return None
    days = math.log(target / current) / math.log(1 + daily_rate_pct / 100)
    days = math.ceil(days)
    return days if days <= horizon else None


def speculative_clause_verdict(
    p: Player,
    price: int,
    trend: Trend | None,
    min_d7: float = 15.0,
    horizon: int = 14,
) -> list[str] | None:
    """A diferencia de `clause_verdict`, aquí ya sabemos que el precio está por encima de lo
    "lógico" — la pregunta no es "¿es una ganga?" sino "¿la racha es lo bastante fuerte y
    sostenida como para que compense de todos modos?". En vez de una única proyección a 14
    días (poco fiable cuando el ritmo diario es tan alto: compone de forma irreal), se calculan
    dos escenarios de ritmo decreciente — optimista (mantiene el ritmo de los últimos 3 días)
    y pesimista (ese ritmo se parte a la mitad cada 3 días) — y se cuenta en cuántos días de
    cada uno recuperarías lo pagado. None si ni siquiera es una racha sostenida de verdad
    (`trend.cooling`, o el 7 días no llega a `min_d7`): eso evita ofrecer como "especulativo
    interesante" algo que ya se frenó, que es justo el error que no queremos repetir."""
    if not trend or trend.cooling or trend.d7 < min_d7 or not p.market_value:
        return None
    current = p.market_value
    if price <= current:
        return None
    d3_daily = ((1 + trend.d3 / 100) ** (1 / 3) - 1) * 100 if trend.d3 > 0 else 0.0

    optimistic = _breakeven_days(current, price, d3_daily, horizon)

    v, rate, pessimistic = float(current), d3_daily, None
    for day in range(1, horizon + 1):
        v *= (1 + rate / 100)
        if v >= price and pessimistic is None:
            pessimistic = day
        if day % 3 == 0:
            rate /= 2

    reasons = [
        f"Racha sostenida, no un pico de un día: sube {trend.d1:+.1f}% al día, {trend.d3:+.1f}% en 3 días",
        f"Pagarías {_fmt_m(price)} por algo que vale {_fmt_m(current)} ahora mismo (x{price / current:.2f})",
    ]
    if optimistic:
        reasons.append(f"Si mantiene el ritmo de los últimos 3 días: recuperas lo pagado en ~{optimistic} días")
    else:
        reasons.append(f"Ni manteniendo el ritmo actual llegarías a recuperarlo en {horizon} días")
    if pessimistic:
        reasons.append(f"Aunque la racha se frene rápido (a la mitad cada 3 días): lo recuperas sobre el día {pessimistic}")
    else:
        gap_pct = (v - price) / price * 100
        reasons.append(f"Si se frena rápido, te quedarías corto: ~{_fmt_m(round(v))} de los {_fmt_m(price)} pagados ({gap_pct:+.0f}%)")
    return reasons


def speculative_clause_alerts(
    rival_slots: list[SquadSlot],
    my_cash: int | None,
    now: datetime,
    freeze: tuple[datetime, datetime] | None = None,
    trends: dict[str, Trend] | None = None,
    min_quality_avg: float = 3.0,
    max_ratio: float = 1.2,
    ratio_ceiling: float = 3.0,
) -> list[SpeculativeAlert]:
    """Cláusulas caras respecto a mercado pero con una racha fuerte y sostenida de verdad —
    alto riesgo, no son una recomendación "segura" como las de `clause_alerts`, por eso van
    aparte y etiquetadas como especulativas."""
    frozen_now = bool(freeze and freeze[0] <= now < freeze[1])
    trends = trends or {}
    if frozen_now:
        return []
    alerts = []
    for p in speculative_clause_candidates(rival_slots, now, min_quality_avg, max_ratio, ratio_ceiling):
        slot = next(s for s in rival_slots if s.player.id == p.id)
        if my_cash is not None and slot.clause > my_cash:
            continue
        reasons = speculative_clause_verdict(p, slot.clause, trends.get(p.id))
        if reasons is None:
            continue
        detail = " · ".join(esc(r) for r in reasons)
        ratio = slot.clause / p.market_value
        message = (
            f"{b(p.name)} <i>{esc(slot.owner_name)}</i> · 📈 especulativo, alto riesgo · "
            f"{b(_fmt_m(slot.clause))} (x{ratio:.2f})\n"
            f"{i(detail)}"
        )
        alerts.append(SpeculativeAlert(slot, message))
    return alerts


def sell_candidates(
    my_slots: list[SquadSlot],
    buy_prices: dict[str, int],
    trends: dict[str, Trend],
) -> list[SquadSlot]:
    """Candidatos a poner a la venta: el disparador es la TENDENCIA (d1 y d3 negativos, lleva
    cayendo estos últimos días), no cuánto has perdido ya. Si compraste caro como apuesta
    especulativa pero la racha de subida sigue viva (d3 positivo), no entra aquí aunque el
    valor actual siga por debajo de lo pagado — la apuesta puede seguir siendo buena. Solo
    cubre jugadores con precio de compra conocido (`buy_prices`, de `my_transactions`)."""
    out = []
    for slot in my_slots:
        if slot.player.id not in buy_prices:
            continue
        trend = trends.get(slot.player.id)
        if trend and trend.d1 < 0 and trend.d3 < 0:
            out.append(slot)
    return out


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


# ---------- saldo estimado de rivales (la API solo expone el tuyo) --------------------------
def reconstruct_cash_flow(events: list[Activity], manager_id: str) -> int:
    """Efecto neto en el saldo de un manager a partir del historial de movimientos: compras y
    cláusulas pagadas restan, ventas, cláusulas cobradas y bonos semanales suman. El blindaje
    (`ACTIVITY_SHIELD`) no mueve dinero, se ignora."""
    total = 0
    for e in events:
        if e.type_id == ACTIVITY_BUY and e.user1_id == manager_id:
            total -= e.amount
        elif e.type_id == ACTIVITY_SELL and e.user1_id == manager_id:
            total += e.amount
        elif e.type_id == ACTIVITY_CLAUSE and e.user1_id == manager_id:
            total -= e.amount
        elif e.type_id == ACTIVITY_CLAUSE and e.user2_id == manager_id:
            total += e.amount
        elif e.type_id == ACTIVITY_WEEKLY_BONUS and e.user1_id == manager_id:
            total += e.amount
    return total


def initial_squad_ids(events: list[Activity], manager_id: str, current_ids: set[str]) -> set[str]:
    """Los jugadores con los que arrancó un mánager (el juego da una plantilla inicial que NO
    sale en el historial): los que su primer movimiento fue perderlos (venderlos o que se los
    clausulen) sin haberlos fichado antes, más los de su plantilla de hoy que nunca han tenido
    un movimiento suyo. Requiere el historial completo desde el arranque de la liga."""
    def gained(e: Activity) -> bool:
        return e.user1_id == manager_id and e.type_id in (ACTIVITY_BUY, ACTIVITY_CLAUSE)

    def lost(e: Activity) -> bool:
        return (e.type_id == ACTIVITY_SELL and e.user1_id == manager_id) or (
            e.type_id == ACTIVITY_CLAUSE and e.user2_id == manager_id
        )

    got: set[str] = set()
    initial: set[str] = set()
    touched: set[str] = set()
    for e in sorted(events, key=lambda ev: (ev.when, int(ev.id))):
        if not e.player_id:
            continue
        if gained(e):
            got.add(e.player_id)
            touched.add(e.player_id)
        elif lost(e):
            touched.add(e.player_id)
            if e.player_id not in got:
                initial.add(e.player_id)
    return initial | {p for p in current_ids if p not in touched}


def estimate_cash(
    events: list[Activity],
    my_manager_id: str,
    my_cash: int,
    manager_ids: dict[str, str],
    initial_values: dict[str, int] | None = None,
) -> dict[str, int]:
    """Saldo estimado de cada equipo (`manager_ids`: team_id -> manager_id), calibrado con tu
    saldo real (el único que la API expone).
    Con `initial_values` (manager_id -> valor de su plantilla inicial): todos los mánagers
    arrancan con el MISMO valor total = plantilla inicial + dinero (regla del juego, según el
    usuario), así que el dinero inicial de cada uno es ese valor común menos su plantilla. El
    valor común se despeja con tu saldo real, y queda: saldo_rival = tu saldo + (tu plantilla
    inicial - la suya) + (su flujo - el tuyo). Sin `initial_values` se cae al supuesto (falso)
    de que todos empezaron con el mismo dinero.
    Es una estimación: hay ~43-60M de salidas de dinero tuyas que el historial no explica
    (ver CLAUDE.md), y un rival puede compartirlas o no."""
    my_flow = reconstruct_cash_flow(events, my_manager_id)
    if initial_values and my_manager_id in initial_values and all(m in initial_values for m in manager_ids.values()):
        base = my_cash - my_flow + initial_values[my_manager_id]  # valor común V
        return {
            team_id: base - initial_values[mgr_id] + reconstruct_cash_flow(events, mgr_id)
            for team_id, mgr_id in manager_ids.items()
        }
    implied_start = my_cash - my_flow
    return {team_id: implied_start + reconstruct_cash_flow(events, mgr_id) for team_id, mgr_id in manager_ids.items()}


# Margen de error del saldo estimado. Medido (2026-09-21) con el historial completo de la liga:
# con el presupuesto inicial que sale de calibrar contra TU saldo real (57.1M), el saldo de los
# 6 mánagers —tú incluido— cae por debajo de cero en algún momento (hasta -70M), y no puede ser:
# faltan movimientos de dinero en el historial que no sabemos cuáles son. Como mínimo ~40M.
CASH_UNCERTAINTY = 40_000_000


def can_bid(price: int, estimated_cash: int, margin: int = CASH_UNCERTAINTY) -> str:
    """¿Puede un rival pujar `price`? "yes" solo si le sobra incluso restando el margen de
    error del saldo estimado, "no" si no le llegaría ni sumándolo, "maybe" en medio (que con
    pujas baratas es casi siempre: el margen es grande comparado con ellas)."""
    if estimated_cash >= price + margin:
        return "yes"
    if estimated_cash >= price - margin:
        return "maybe"
    return "no"


def clause_theft_risk(
    my_slots: list[SquadSlot],
    rival_cash: dict[str, int],
    now: datetime,
) -> list[tuple[SquadSlot, list[str]]]:
    """De tus jugadores con la cláusula pagable ahora mismo, cuáles tienen algún rival con
    saldo estimado suficiente para pagarla — quién podría robártelo."""
    out = []
    for slot in my_slots:
        if not slot.clause_open(now):
            continue
        threats = [team_id for team_id, cash in rival_cash.items() if cash >= slot.clause]
        if threats:
            out.append((slot, threats))
    return out


# ---------- flipeo: comprar en subida, vender rápido con beneficio o mínima pérdida ---------
def squad_can_field_eleven(slots: list[SquadSlot], exclude_player_id: str | None = None) -> bool:
    """¿La plantilla (quitando `exclude_player_id`, si se da) tiene cuerpos suficientes para
    alinear un once legal? No mira quién es mejor, solo si hay disponibles de sobra en cada
    posición — la pregunta de seguridad antes de aceptar una venta: "¿me quedo corto para la
    próxima jornada?". `start_prob=1.0` a propósito: aquí no importa la probabilidad real de
    jugar, solo si hay cuerpos elegibles."""
    cands = [
        Candidate(sl.player, 1.0, 1.0)
        for sl in slots
        if sl.player.id != exclude_player_id and sl.player.position_id != 5 and sl.player.available
    ]
    _, eleven, _ = best_eleven(cands)
    return len(eleven) == 11


def flip_decision(buy_price: int, offer_amount: int, days_held: int, trend: Trend | None) -> tuple[str, str]:
    """Qué hacer con una oferta sobre un jugador comprado para revender rápido (unos días, no
    para quedárselo): con beneficio, aceptar siempre — no hay que ser codicioso con márgenes
    pequeños. Sin beneficio: si la tendencia sigue subiendo y aún quedan días de margen
    (<3), esperar una oferta mejor (el juego ofrece un precio distinto cada ciclo, no hay
    prisa). Pasados 3 días sin beneficio, priorizar liquidez: aceptar en cuanto cubra al
    menos lo pagado, e intentar no vender por debajo salvo que ya no quede alternativa."""
    gain = offer_amount - buy_price
    if gain > 0:
        pct = gain / buy_price * 100 if buy_price else 0.0
        return "accept", f"Beneficio: +{_fmt_m(gain)} ({pct:+.0f}%)"
    if days_held < 3 and trend and trend.d3 > 0:
        return "wait", f"Sin beneficio pero la tendencia sigue subiendo (+{trend.d3:.1f}% / 3d) — esperar mejor oferta"
    if offer_amount >= buy_price:
        return "accept", f"Día {days_held}: cubre al menos lo pagado, asegurar antes de que empeore"
    return "wait", f"Día {days_held}: por debajo de lo pagado — esperar si aún hay margen, evitar vender en pérdida"


# ---------- estrategia general (consejos) ------------------------------------
# Principios recogidos de guías de comunidad (Comuniate, FútbolFantasy, JornadaPerfecta) sobre
# el sistema de cláusulas de LaLiga Fantasy, no reglas propias inventadas. Se rotan uno al día
# cuando no hay nada más urgente que decir (ver `service.daily_advice_report`).
STRATEGY_TIPS = [
    "No fiches por impulso: mira primero la tendencia de precio de los últimos días y si el calendario del jugador es favorable — esas dos cosas juntas son la apuesta razonable, no el nombre bonito.",
    "No gastes todo el presupuesto pronto: cuanto más margen dejes, más rápido puedes reaccionar a un chollo o defenderte si te intentan pagar una cláusula.",
    "Si un rival te clausula a alguien, no le devuelvas el golpe por rabia: piensa primero qué puede hacer él con ese dinero antes de pagarle una cláusula suya — a veces le estás haciendo un favor.",
    "No blindes toda la plantilla: gasta la protección solo en tus 2-3 jugadores de verdad importantes y deja el resto como cebo — que un rival gaste de más clausulando a alguien sustituible.",
    "Los parones de selecciones suelen traer el mercado al alza: si vas a especular comprando barato, esas fechas dan más margen para recuperar la inversión antes de revender.",
    "Revisa el calendario de tus jugadores clave 2-3 jornadas por delante, no solo la próxima: un buen tramo de calendario vale más que acertar una sola jornada suelta.",
    "No sobrevalores una posición por miedo a quedarte corto: la profundidad de talento no es igual en todas — un delantero suplente de un grande suele rendir más que un titular de un equipo flojo.",
]
