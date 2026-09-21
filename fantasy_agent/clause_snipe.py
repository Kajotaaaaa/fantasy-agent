"""Comprar una cláusula en el MISMO SEGUNDO en que se desbloquea.

La hora de desbloqueo de cada cláusula (`buyoutClauseLockedEndTime`) se conoce al segundo, y el
primero que paga se lleva al jugador. El vigilante de 30 min y el botón normal (Telegram ->
Worker -> runner de GitHub, 20-60 s) llegan tarde, así que aquí se ARMA la compra con
antelación: el usuario pulsa "🎯 Comprar al desbloquearse" y confirma (doble confirmación de
siempre); ese botón lanza este trabajo, que espera con la hora del servidor de LaLiga y dispara
el pago al desbloquearse, reintentando cada ~0.15 s.

AUTORIZACIÓN (explícita del usuario, 2026-09-21): al armar y confirmar, el bot puede gastar el
dinero necesario para clausular a ESE jugador. Topes de seguridad, aun así: nunca más de
`CAP_FACTOR` veces la cláusula que había al armar (si el dueño la sube más, se cancela y se
avisa), nunca sin saldo suficiente (no se puede pagar una cláusula endeudándose), y si el
desbloqueo cae dentro de la congelación de cláusulas de la liga se espera a que termine.

Un trabajo de GitHub dura como mucho 6 h, así que solo se acepta armar con menos de
`MAX_ARM` por delante. Durante el último minuto edita un mensaje de Telegram con la cuenta atrás."""
from __future__ import annotations

import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from . import models, notify, service
from .analysis import b, esc, i
from .api import FantasyAPI
from .config import Settings
from .http import HttpError, request_json
from .snipe import _server_offset

MAX_ARM = timedelta(hours=service.MAX_ARM_HOURS)
CAP_FACTOR = 1.25  # si la cláusula sube por encima de esto respecto a la del momento de armar, no se paga
COUNTDOWN_FROM = 60  # segundos antes del desbloqueo en que empieza el contador
COUNTDOWN_STEP = 10
PREFETCH_AT = 25  # segundos antes: se relee la plantilla del dueño (id de hueco y cláusula al día)
FIRE_WINDOW = timedelta(seconds=20)  # cuánto se insiste tras el desbloqueo antes de darlo por perdido
RETRY_EVERY = 0.15


def fire_time(
    unlock: datetime | None, freeze: tuple[datetime, datetime] | None, now: datetime,
) -> datetime | None:
    """Cuándo se puede pagar de verdad: el desbloqueo, o el final de la congelación de cláusulas
    de la liga si cae dentro de ella (nadie puede pagar durante la congelación). None = ya se
    puede pagar ahora."""
    t = unlock if unlock and unlock > now else None
    if freeze:
        start, end = freeze
        if t is None and start <= now < end:
            t = end
        elif t is not None and start <= t < end:
            t = end
    return t


def parse_wanted(text: str) -> dict[str, int | None]:
    """Lista de deseados, de la variable `CLAUSE_WANTED`: "Rodri:90, Yamal:150, 2206" ->
    {"rodri": 90_000_000, "yamal": 150_000_000, "2206": None}. Cada entrada es un nombre o un id y,
    opcional, el máximo en MILLONES que estás dispuesto a pagar por su cláusula (sin máximo, el
    tope de siempre: 1.25x la cláusula del momento)."""
    out: dict[str, int | None] = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, cap = part.rpartition(":")
        if sep and name.strip():
            try:
                out[name.strip().lower()] = round(float(cap) * 1_000_000)  # decimales con punto: "150.5"
                continue
            except ValueError:
                pass
        out[part.lower()] = None
    return out


def wanted_targets(
    slots: list[models.SquadSlot], wanted: dict[str, int | None], freeze: tuple[datetime, datetime] | None,
    now: datetime, cash: int | None,
) -> list[tuple[models.SquadSlot, int, datetime | None]]:
    """De la lista de deseados, a quién se puede armar YA: la cláusula cabe en lo que quieres
    pagar y en tu saldo, y falta menos de `MAX_ARM` para poder pagarla (o ya se puede).
    Devuelve (hueco, tope, cuándo se puede pagar; None = ya)."""
    out = []
    for sl in slots:
        key = next((k for k in (sl.player.id, sl.player.name.strip().lower()) if k in wanted), None)
        if key is None or sl.player.position_id == 5:
            continue
        cap = wanted[key] or round(sl.clause * CAP_FACTOR)
        if sl.clause > cap or (cash is not None and sl.clause > cash):
            continue
        when = fire_time(sl.clause_locked_until, freeze, now)
        if when is not None and when - now > MAX_ARM:
            continue
        out.append((sl, cap, when))
    return out


def _dispatch_arm(player_id: str, cap: int) -> None:
    """Lanza el trabajo de espera desde dentro de GitHub Actions (el `GITHUB_TOKEN` del propio
    workflow puede disparar `repository_dispatch`), igual que si hubieras pulsado el botón."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        raise RuntimeError("Sin GITHUB_TOKEN/GITHUB_REPOSITORY: solo funciona dentro de GitHub Actions")
    request_json(
        "POST", f"https://api.github.com/repos/{repo}/dispatches",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        json_body={"event_type": "fantasy-clause-snipe", "client_payload": {"action": f"a:{player_id}:{cap}"}},
        retries=0,
    )


def auto_arm(world: service.World, store, s: Settings) -> list[str]:
    """Arma solo, en cada vigilancia, la compra de los jugadores de `CLAUSE_WANTED` cuando ya se
    puede (falta menos de `MAX_ARM`): no hace falta estar pendiente de pulsar un botón. Una vez
    por jugador y desbloqueo (`armed:<id>:<hora>` en el Store)."""
    wanted = parse_wanted(os.environ.get("CLAUSE_WANTED", ""))
    if not wanted:
        return []
    out = []
    for sl, cap, when in wanted_targets(world.rival_slots, wanted, world.clause_freeze, datetime.now(timezone.utc), world.my_cash):
        key = f"armed:{sl.player.id}:{when.isoformat() if when else 'open'}"
        if store.get(key):
            continue
        store.set(key, "1")
        try:
            _dispatch_arm(sl.player.id, cap)
        except Exception as exc:
            store.set(key, "")  # que se reintente en la siguiente vigilancia
            notify.send_telegram(s, f"❌ {b('No pude armar')} la compra de {b(sl.player.name)}\n{esc(str(exc)[:300])}")
            continue
        when_txt = f"se desbloquea a las {when.strftime('%H:%M:%S')}" if when else "ya se puede pagar"
        notify.send_telegram(
            s, f"⭐ {b('Lista de deseados')}: armo la compra de {b(sl.player.name)} ({esc(sl.owner_name)}), "
               f"cláusula {b(service.m(sl.clause))}, {when_txt}. Pagaré hasta {b(service.m(cap))}.",
        )
        out.append(f"armado {sl.player.name}")
    return out


def _fresh(api: FantasyAPI, world: service.World, owner_team_id: str, owner_name: str, player_id: str):
    api.clear_cache()
    squad = models.parse_squad(api.team(world.league_id, owner_team_id), owner_team_id, owner_name)
    return next((sl for sl in squad if sl.player.id == player_id), None)


def _mine_now(api: FantasyAPI, world: service.World, player_id: str) -> bool:
    api.clear_cache()
    squad = models.parse_squad(api.team(world.league_id, world.my_team_id), world.my_team_id, "yo")
    return any(sl.player.id == player_id for sl in squad)


def run(
    api: FantasyAPI, s: Settings, player_id: str, dry: bool = False, unlock_in: float | None = None,
    max_price: int | None = None,
) -> None:
    """`dry` + `unlock_in` (segundos) SOLO para pruebas: simula un desbloqueo dentro de N segundos
    y hace todo igual (esperas, cuenta atrás, revalidación) menos el pago, que se da por hecho."""
    fast = FantasyAPI(replace(s, request_delay_s=0.0))  # sin el ritmo "humano": aquí cada décima cuenta
    tag = "(SIMULACRO) " if dry else ""
    league_id, _, cash = service.resolve_league(fast, s)
    world = service.build_world(fast, s, with_trends=False)
    slot = next((sl for sl in world.rival_slots if sl.player.id == player_id), None)
    if slot is None:
        notify.send_telegram(s, f"❌ {b('No armo la compra')}\nEse jugador ya no está en la plantilla de ningún rival.")
        return

    offset = _server_offset()
    now = lambda: datetime.now(timezone.utc) + offset  # noqa: E731
    name, owner = slot.player.name, slot.owner_name
    fire_at = fire_time(slot.clause_locked_until, world.clause_freeze, now())
    if unlock_in is not None:
        fire_at = now() + timedelta(seconds=unlock_in)
    armed_clause = slot.clause
    # `max_price` (de la lista de deseados: "Rodri:90") manda sobre el tope por defecto: si quieres
    # a alguien aunque cueste por encima de su valor, lo dices tú con el máximo.
    cap = max_price or round(armed_clause * CAP_FACTOR)
    if armed_clause > cap:
        notify.send_telegram(
            s, f"❌ {b('No armo la compra')}\nLa cláusula de {b(name)} ({service.m(armed_clause)}) ya pasa de "
               f"lo que quieres pagar ({service.m(cap)}).",
        )
        return
    if cash is not None and armed_clause > cash:
        notify.send_telegram(
            s, f"❌ {b('No armo la compra')}\nTu saldo ({service.m(cash)}) no llega a la cláusula de {b(name)} "
               f"({service.m(armed_clause)}); no se puede pagar una cláusula endeudándose.",
        )
        return
    if fire_at is not None and fire_at - now() > MAX_ARM:
        notify.send_telegram(
            s, f"❌ {b('No armo la compra')}\nFalta demasiado para el desbloqueo de {b(name)} "
               f"({fire_at.strftime('%d/%m %H:%M:%S')}): un trabajo de GitHub solo aguanta ~6 h. Arma más cerca.",
        )
        return

    when = f"a las {fire_at.strftime('%H:%M:%S')} (hora de la liga)" if fire_at else "ya"
    notify.send_telegram(
        s, f"🎯 {tag}{b('Compra armada')}\n{b(name)} de {esc(owner)}: cláusula {b(service.m(armed_clause))}, "
           f"la pagaré {when}.\n{i(f'Máximo que pagaré: {service.m(cap)} (si el dueño la sube más, cancelo).')}",
    )

    ptid, amount = slot.player_team_id, armed_clause
    counter_id = None
    last_edit = 0.0
    prefetched = False
    if fire_at is not None:
        while (left := (fire_at - now()).total_seconds()) > 0:
            if not prefetched and left <= PREFETCH_AT:
                prefetched = True
                fresh = _fresh(fast, world, slot.owner_team_id, owner, player_id)
                _, _, cash_now = service.resolve_league(fast, s)
                if fresh is None:
                    notify.send_telegram(s, f"❌ {b('Cancelada')}\n{b(name)} ya no está con {esc(owner)}.")
                    return
                if fresh.clause > cap:
                    notify.send_telegram(
                        s, f"❌ {b('Cancelada')}\n{esc(owner)} ha subido la cláusula de {b(name)} a "
                           f"{service.m(fresh.clause)} (mi tope era {service.m(cap)}).",
                    )
                    return
                if cash_now is not None and fresh.clause > cash_now:
                    notify.send_telegram(
                        s, f"❌ {b('Cancelada')}\nTu saldo ({service.m(cash_now)}) ya no llega a la cláusula "
                           f"({service.m(fresh.clause)}).",
                    )
                    return
                ptid, amount = fresh.player_team_id, fresh.clause
            if left <= COUNTDOWN_FROM:
                if counter_id is None or time.time() - last_edit >= COUNTDOWN_STEP - 0.5:
                    text = f"⏱️ {tag}{b(name)}: {int(left)} s para el desbloqueo · cláusula {service.m(amount)} 🎯 armada"
                    if counter_id is None:
                        counter_id = notify.send_message(s, text)
                    else:
                        notify.edit_message(s, counter_id, text)
                    last_edit = time.time()
                time.sleep(min(0.5, max(0.02, left - 0.05)))
            else:
                time.sleep(min(1.0, max(0.05, left - COUNTDOWN_FROM)))
    else:
        fresh = _fresh(fast, world, slot.owner_team_id, owner, player_id)
        if fresh is None or fresh.clause > cap:
            notify.send_telegram(s, f"❌ {b('Cancelada')}\nLa cláusula de {b(name)} ha cambiado o ya no está con {esc(owner)}.")
            return
        ptid, amount = fresh.player_team_id, fresh.clause
    if counter_id is not None:
        notify.edit_message(s, counter_id, f"⚡ {b(name)}: ¡desbloqueada! Pagando {service.m(amount)}…")

    # Disparo: se insiste cada RETRY_EVERY hasta que entra o pasa la ventana. Un error antes de
    # tiempo (aún bloqueada) o de importe desactualizado (409) se reintenta; si un intento cuelga
    # o el jugador ya aparece en tu plantilla, se da por pagado sin volver a pagar.
    started = now()
    deadline = started + FIRE_WINDOW
    last_error = ""
    attempts = 0
    paid = False
    while now() < deadline and not paid:
        attempts += 1
        try:
            if not dry:
                fast.pay_clause(league_id, ptid, amount)
            paid = True
        except HttpError as exc:
            last_error = str(exc)
            if exc.status == 409:  # "Buyout wanted to pay is not updated": la cláusula cambió
                fresh = _fresh(fast, world, slot.owner_team_id, owner, player_id)
                if fresh is None:
                    break
                if fresh.clause > cap:
                    last_error = f"la cláusula subió a {service.m(fresh.clause)}, por encima de mi tope"
                    break
                ptid, amount = fresh.player_team_id, fresh.clause
            time.sleep(RETRY_EVERY)
        except Exception as exc:  # cuelgue de red: puede haberse pagado igualmente
            last_error = str(exc)
            if _mine_now(fast, world, player_id):
                paid = True
    if not paid and not dry and _mine_now(fast, world, player_id):
        paid = True

    if paid:
        took = (now() - (fire_at or started)).total_seconds()
        msg = (
            f"✅ {tag}{b('¡Cláusula pagada!')}\n{b(name)} de {esc(owner)} por {b(service.m(amount))}\n"
            f"{i(f'Pagada {took:+.1f} s respecto al desbloqueo ({attempts} intentos).')}"
        )
    else:
        msg = (
            f"❌ {b('No conseguí la cláusula')} de {b(name)}\n{esc(last_error[:300] or 'sin respuesta')}\n"
            f"{i('Puede que alguien se adelantara; comprueba la plantilla del dueño.')}"
        )
    if counter_id is not None:
        notify.edit_message(s, counter_id, msg)
    notify.send_telegram(s, msg)
