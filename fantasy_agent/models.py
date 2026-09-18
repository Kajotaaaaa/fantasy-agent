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

    def clause_open(self, now: datetime) -> bool:
        return self.clause > 0 and (self.clause_locked_until is None or self.clause_locked_until <= now)


def parse_squad(team_payload: dict, team_id: str, owner_name: str) -> list[SquadSlot]:
    slots = []
    for item in as_list(team_payload, "players", "team.players"):
        slots.append(
            SquadSlot(
                player=parse_player(item),
                owner_team_id=team_id,
                owner_name=owner_name,
                clause=to_int(pick(item, "buyoutClause", "clause")),
                clause_locked_until=parse_dt(pick(item, "buyoutClauseLockedEndTime")),
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
    player: Player
    price: int
    expires: datetime | None
    seller: str  # "LaLiga" si lo pone el juego; nombre del mánager si es de un rival
    bids: int


def parse_market(payload: Any) -> list[MarketItem]:
    items = []
    for item in as_list(payload, "market", "elements"):
        seller = pick(item, "sellerTeam.manager.managerName", "sellerTeam.name", default=None)
        items.append(
            MarketItem(
                player=parse_player(item),
                price=to_int(pick(item, "salePrice", "price")),
                expires=parse_dt(pick(item, "expirationDate", "expirationTime")),
                seller=str(seller) if seller else "LaLiga",
                bids=to_int(pick(item, "numberOfBids", "bidsCount", "offersCount")),
            )
        )
    return items


def parse_value_history(payload: Any) -> list[tuple[datetime, int]]:
    out = []
    for item in as_list(payload, "marketValues", "values"):
        dt = parse_dt(pick(item, "date", "day"))
        val = to_int(pick(item, "marketValue", "value"))
        if dt and val:
            out.append((dt, val))
    out.sort(key=lambda t: t[0])
    return out
