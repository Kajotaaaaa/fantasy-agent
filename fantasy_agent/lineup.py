"""Once ideal: mejor formación legal maximizando puntos esperados."""
from __future__ import annotations

from dataclasses import dataclass

from .models import Player

# Formaciones permitidas en LaLiga Fantasy (DEF-MED-DEL, siempre 1 portero).
FORMATIONS = [(3, 4, 3), (3, 5, 2), (4, 3, 3), (4, 4, 2), (4, 5, 1), (5, 3, 2), (5, 4, 1)]


@dataclass
class Candidate:
    player: Player
    start_prob: float  # 0..1
    xpts: float
    note: str = ""


def expected_points(player: Player, start_prob: float) -> float:
    if player.status.lower() in ("injured", "suspended", "lesionado", "sancionado"):
        return 0.0
    base = player.avg_points if player.avg_points > 0 else 2.0
    doubt = 0.55 if player.status.lower() in ("doubtful", "duda") else 1.0
    return round(base * start_prob * doubt, 2)


def best_eleven(cands: list[Candidate]) -> tuple[tuple[int, int, int] | None, list[Candidate], float]:
    by_pos: dict[int, list[Candidate]] = {1: [], 2: [], 3: [], 4: []}
    for c in cands:
        if c.player.position_id in by_pos:
            by_pos[c.player.position_id].append(c)
    for lst in by_pos.values():
        lst.sort(key=lambda c: c.xpts, reverse=True)

    best: tuple[tuple[int, int, int] | None, list[Candidate], float] = (None, [], -1.0)
    for d, m, f in FORMATIONS:
        pick = by_pos[1][:1] + by_pos[2][:d] + by_pos[3][:m] + by_pos[4][:f]
        total = sum(c.xpts for c in pick)
        complete = len(pick) == 11
        # Una formación completa siempre gana a una incompleta.
        if (complete, total) > (len(best[1]) == 11, best[2]):
            best = ((d, m, f), pick, round(total, 2))
    return best
