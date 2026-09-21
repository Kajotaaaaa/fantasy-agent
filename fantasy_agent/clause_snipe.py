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

import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from . import models, notify, service
from .analysis import b, esc, i
from .api import FantasyAPI
from .config import Settings
from .http import HttpError
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


def _fresh(api: FantasyAPI, world: service.World, owner_team_id: str, owner_name: str, player_id: str):
    api.clear_cache()
    squad = models.parse_squad(api.team(world.league_id, owner_team_id), owner_team_id, owner_name)
    return next((sl for sl in squad if sl.player.id == player_id), None)


def _mine_now(api: FantasyAPI, world: service.World, player_id: str) -> bool:
    api.clear_cache()
    squad = models.parse_squad(api.team(world.league_id, world.my_team_id), world.my_team_id, "yo")
    return any(sl.player.id == player_id for sl in squad)


def run(api: FantasyAPI, s: Settings, player_id: str, dry: bool = False, unlock_in: float | None = None) -> None:
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
    cap = round(armed_clause * CAP_FACTOR)
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
