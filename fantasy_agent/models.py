"""Normalización defensiva del JSON de la API a dataclasses.

La API no está documentada y cambia entre temporadas, así que cada campo prueba
varias claves. Si un campo sale vacío, mira `fantasy probe` y añade la clave aquí.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

POSITIONS = {1: "POR", 2: "DEF", 3: "MED", 4: "DEL", 5: "ENT"}


def pick(d: Any, *keys: str, default: Any = None) -> Any:
    """Primer valor no nulo entre varias claves; admite rutas con puntos ('team.name')."""
    if not isinstance(d, dict):
        return default
    for key in keys:
        cur: Any = d
        for part in key.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                break
        if cur is not None:
            return cur
    return default


def as_list(payload: Any, *keys: str) -> list:
    if isinstance(payload, list):
        return payload
    value = pick(payload, *keys, default=[])
    return value if isinstance(value, list) else []


def parse_dt(value: Any) -> datetime | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


@dataclass
class Player:
    id: str
    name: str
    position_id: int
    team: str
    team_id: str
    market_value: int
    points: int
    avg_points: float
    status: str  # ok | injured | doubtful | suspended | ...
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def position(self) -> str:
        return POSITIONS.get(self.position_id, "?")

    @property
    def available(self) -> bool:
        return self.status.lower() in ("ok", "available", "")


def parse_player(d: dict) -> Player:
    pm = pick(d, "playerMaster", default=d)
    return Player(
        id=str(pick(pm, "id", default="")),
        name=str(pick(pm, "nickname", "name", "playerName", default="?")),
        position_id=to_int(pick(pm, "positionId", "position.id")),
        team=str(pick(pm, "team.name", "team.shortName", "teamName", default="?")),
        team_id=str(pick(pm, "team.id", "teamId", default="")),
        market_value=to_int(pick(pm, "marketValue", "value")),
        points=to_int(pick(pm, "points", "totalPoints")),
        avg_points=float(pick(pm, "averagePoints", "avgPoints", default=0) or 0),
        status=str(pick(pm, "playerStatus", "status", default="ok")),
        raw=pm,
    )


def all_team_names(payload: Any) -> dict[str, str]:
    """Nombre de cada equipo real de LaLiga (team_id -> nombre), a partir del listado público
    de jugadores: cubre los 20 equipos aunque ninguno de sus jugadores esté en tu liga privada
    — a diferencia de construirlo solo a partir de las plantillas (`my_slots`/`rival_slots`),
    que deja huecos y hace salir "equipo #<id>" para el rival de la jornada de un club sin
    representación en la liga (2026-09-22, bug real reportado por el usuario)."""
    out: dict[str, str] = {}
    for item in as_list(payload, "players", "elements"):
        team_id = str(pick(item, "team.id", "teamId", default=""))
        name = str(pick(item, "team.name", "team.shortName", "teamName", default="") or "")
        if team_id and name:
            out.setdefault(team_id, name)
    return out


def team_strength(payload: Any) -> tuple[dict[str, float], dict[str, float]]:
    """Media de puntos por partido de portero+defensas (cuánto encaja el equipo real: más bajo
    = defensa floja) y de medios+delanteros (cuánto ataca), por equipo real de LaLiga, a partir
    del listado público de jugadores — para el factor de dificultad del rival de la jornada
    (`analysis.fixture_factor`)."""
    by_def: dict[str, list[float]] = {}
    by_att: dict[str, list[float]] = {}
    for item in as_list(payload, "players", "elements"):
        team_id = str(pick(item, "team.id", "teamId", default=""))
        pos = to_int(pick(item, "positionId", "position.id"))
        avg = float(pick(item, "averagePoints", "avgPoints", default=0) or 0)
        if not team_id or not avg or pos not in (1, 2, 3, 4):
            continue
        (by_def if pos in (1, 2) else by_att).setdefault(team_id, []).append(avg)
    return (
        {tid: sum(v) / len(v) for tid, v in by_def.items()},
        {tid: sum(v) / len(v) for tid, v in by_att.items()},
    )


def points_by_position(payload: Any) -> dict[int, list[tuple[str, int]]]:
    """id y puntos totales de cada jugador, agrupados por posición (para detectar TOP de liga)."""
    out: dict[int, list[tuple[str, int]]] = {}
    for item in as_list(payload, "players", "elements"):
        pid = str(pick(item, "id", default=""))
        pos = to_int(pick(item, "positionId", "position.id"))
        pts = to_int(pick(item, "points", "totalPoints"))
        if pid and pos:
            out.setdefault(pos, []).append((pid, pts))
    return out


def week_points_by_id(payload: Any) -> dict[str, list[tuple[int, int]]]:
    """id -> [(weekNumber, points), ...] a partir del listado público de jugadores."""
    out: dict[str, list[tuple[int, int]]] = {}
    for item in as_list(payload, "players", "elements"):
        pid = str(pick(item, "id", default=""))
        if not pid:
            continue
        weeks = [
            (to_int(pick(w, "weekNumber")), to_int(pick(w, "points")))
            for w in as_list(item, "weekPoints")
        ]
        out[pid] = weeks
    return out


@dataclass
class SquadSlot:
    player: Player
    owner_team_id: str
    owner_name: str
    clause: int
    clause_locked_until: datetime | None
    shielded_until: datetime | None = None
    player_team_id: str = ""  # id del hueco de plantilla (para pagar la cláusula), no el del jugador

    def clause_open(self, now: datetime) -> bool:
        if self.clause <= 0:
            return False
        if self.clause_locked_until and self.clause_locked_until > now:
            return False
        if self.shielded_until and self.shielded_until > now:
            return False
        return True


def parse_squad(team_payload: dict, team_id: str, owner_name: str) -> list[SquadSlot]:
    slots = []
    for item in as_list(team_payload, "players", "team.players"):
        shielded = pick(item, "isShielded", default=False)
        slots.append(
            SquadSlot(
                player=parse_player(item),
                owner_team_id=team_id,
                owner_name=owner_name,
                clause=to_int(pick(item, "buyoutClause", "clause")),
                clause_locked_until=parse_dt(pick(item, "buyoutClauseLockedEndTime")),
                shielded_until=parse_dt(pick(item, "shieldedEndDate")) if shielded else None,
                player_team_id=str(pick(item, "playerTeamId", default="")),
            )
        )
    return slots


@dataclass
class TeamStanding:
    team_id: str
    team_name: str
    manager_id: str
    manager_name: str
    points: int
    team_value: int


def parse_standing(payload: Any) -> list[TeamStanding]:
    rows = []
    for item in as_list(payload, "standing", "teams", "elements"):
        team = pick(item, "team", default=item)
        rows.append(
            TeamStanding(
                team_id=str(pick(team, "id", default="")),
                team_name=str(pick(team, "name", default="?")),
                manager_id=str(pick(team, "manager.id", "userId", default="")),
                manager_name=str(pick(team, "manager.managerName", "manager.name", "name", default="?")),
                points=to_int(pick(item, "points", "teamPoints", "team.teamPoints")),
                team_value=to_int(pick(team, "teamValue", default=pick(item, "teamValue"))),
            )
        )
    return rows


@dataclass
class MarketItem:
    listing_id: str  # id del anuncio (para pujar), distinto del id del jugador
    player: Player
    price: int
    expires: datetime | None
    seller: str  # "LaLiga" si lo pone el juego; nombre del mánager si es de un rival
    bids: int
    # Tu puja pendiente en este anuncio, si la hay: la API la incluye en el propio anuncio
    # (`bid: {id, money, status}`) solo para ti; sin ella no se puede cambiar una puja.
    my_bid_id: str = ""
    my_bid: int = 0


def parse_market(payload: Any) -> list[MarketItem]:
    items = []
    for item in as_list(payload, "market", "elements"):
        seller = pick(item, "sellerTeam.manager.managerName", "sellerTeam.name", default=None)
        pending = pick(item, "bid.status", default="") == "pending"
        items.append(
            MarketItem(
                listing_id=str(pick(item, "id", default="")),
                player=parse_player(item),
                price=to_int(pick(item, "salePrice", "price")),
                expires=parse_dt(pick(item, "expirationDate", "expirationTime")),
                seller=str(seller) if seller else "LaLiga",
                bids=to_int(pick(item, "numberOfBids", "bidsCount", "offersCount")),
                my_bid_id=str(pick(item, "bid.id", default="")) if pending else "",
                my_bid=to_int(pick(item, "bid.money", default=0)) if pending else 0,
            )
        )
    return items


@dataclass
class Fixture:
    team_id: str
    rival_id: str
    home: bool
    when: datetime | None


def parse_calendar(payload: Any) -> list[Fixture]:
    out = []
    for m in as_list(payload, "matches", "elements"):
        local = str(pick(m, "localId", default=""))
        visitor = str(pick(m, "visitorId", default=""))
        when = parse_dt(pick(m, "matchDate", "date"))
        if local:
            out.append(Fixture(local, visitor, True, when))
        if visitor:
            out.append(Fixture(visitor, local, False, when))
    return out


# activityTypeId conocidos del feed de actividad (identificados cruzando contra la plantilla
# real, ver `parse_activity`): 9 (te unes a la liga) y 10 (evento de liga) no mueven dinero.
ACTIVITY_CLAUSE = 1
ACTIVITY_SHIELD = 4
ACTIVITY_WEEKLY_BONUS = 6
ACTIVITY_BUY = 31
ACTIVITY_SELL = 33


@dataclass
class Activity:
    id: str
    type_id: int
    user1_id: str
    user2_id: str | None
    player_id: str | None
    amount: int
    when: datetime | None


def parse_activity(payload: Any) -> list[Activity]:
    """Feed de movimientos de la liga (`GET .../activity/{index}`). No documentado; los
    tipos se identificaron cruzando contra la plantilla real y el histórico de cláusulas:
    31 = compra en el mercado de LaLiga (solo user1, comprador), 33 = venta (solo user1,
    vendedor), 1 = cláusula pagada entre managers (user1 paga, user2 la sufre/cobra). Otros
    tipos (4 = blindaje, 6 = bono semanal) no son transacciones de jugador y no se exponen
    aquí como tales."""
    out = []
    for item in as_list(payload):
        user2 = pick(item, "user2Id")
        player = pick(item, "playerMasterId")
        out.append(Activity(
            id=str(pick(item, "id", default="")),
            type_id=to_int(pick(item, "activityTypeId")),
            user1_id=str(pick(item, "user1Id", default="")),
            user2_id=str(user2) if user2 is not None else None,
            player_id=str(player) if player is not None else None,
            amount=to_int(pick(item, "amount")),
            when=parse_dt(pick(item, "createdAt")),
        ))
    return out


def parse_value_history(payload: Any) -> list[tuple[datetime, int]]:
    out = []
    for item in as_list(payload, "marketValues", "values"):
        dt = parse_dt(pick(item, "date", "day"))
        val = to_int(pick(item, "marketValue", "value"))
        if dt and val:
            out.append((dt, val))
    out.sort(key=lambda t: t[0])
    return out
