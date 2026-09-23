"""Comprar una cláusula en el MISMO SEGUNDO en que se desbloquea, por el precio EXACTO que viste.

La hora de desbloqueo de cada cláusula (`buyoutClauseLockedEndTime`) se conoce al segundo, y el
primero que paga se lleva al jugador. El vigilante de 30 min y el botón normal (Telegram ->
Worker -> runner de GitHub, 20-60 s) llegan tarde, así que aquí se ARMA la compra con
antelación: el usuario pulsa "🎯 Comprar al desbloquearse ... por X" y confirma (doble
confirmación de siempre); ese botón lanza este trabajo, que espera con la hora del servidor de
LaLiga y dispara el pago al desbloquearse (empieza medio segundo antes), reintentando cada 0.1 s.

REGLA DEL USUARIO (2026-09-21): lo que autoriza es comprar POR ESE IMPORTE EXACTO, y el armado
SE QUEDA PUESTO (no se elimina). Si el dueño cambia la cláusula (sube o baja) entre que armas y
el desbloqueo, el trabajo sigue esperando por el importe autorizado, te AVISA con el importe
nuevo (una vez por importe) y te pregunta con un botón si quieres dejarla cargada también por el
nuevo. Nunca paga un importe que no hayas confirmado: si al desbloquearse vale otra cosa y no
has confirmado el nuevo, no compra y te lo cuenta. Se vigila cada `CHECK_EVERY` s durante la
espera y una última vez a `PREFETCH_AT` s del desbloqueo. Además: nunca sin saldo suficiente (no
se puede pagar una cláusula endeudándose) y si el desbloqueo cae dentro de la congelación de
cláusulas de la liga se espera a que termine.

Un trabajo de GitHub dura como mucho 6 h, así que solo se acepta armar con menos de
`MAX_ARM` por delante. Durante el último minuto edita un mensaje de Telegram con la cuenta atrás."""
from __future__ import annotations

import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from . import analysis, models, notify, service
from .analysis import b, esc, i
from .api import FantasyAPI
from .config import Settings
from .http import HttpError
from .snipe import _server_offset

MAX_ARM = timedelta(hours=service.MAX_ARM_HOURS)
CHECK_EVERY = 45  # segundos entre comprobaciones de que la cláusula sigue igual mientras se espera
COUNTDOWN_FROM = 60  # segundos antes del desbloqueo en que empieza el contador
COUNTDOWN_STEP = 10
PREFETCH_AT = 25  # segundos antes: última comprobación (plantilla del dueño, importe y saldo al día)
FIRE_WINDOW = timedelta(seconds=20)  # cuánto se insiste tras el desbloqueo antes de darlo por perdido
RETRY_EVERY = 0.10  # pausa entre intentos de pago (cada uno tarda además ~0.3-0.6 s en ir y volver)
FIRE_LEAD = 0.5  # segundos ANTES del desbloqueo en que ya se empieza a intentar: la hora del servidor
# solo se lee con precisión de 1 s y un intento tarda en llegar; los que caen antes fallan sin
# consecuencias (aún bloqueada) y el primero después del desbloqueo entra en cuanto se puede.
RECHECK_EVERY = 2.0  # mínimo entre relecturas de la plantilla durante el disparo (cada una cuesta ~0.5 s)


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


def _reach(cash: int | None, clause: int) -> str:
    if cash is None:
        return ""
    return f"\nTu saldo: {b(service.m(cash))} — " + ("✅ te llega" if cash >= clause else "❌ no te llega")


def reminder_message(
    slot: models.SquadSlot, when: datetime | None, cash: int | None, now: datetime,
) -> tuple[str, dict]:
    """El aviso de que una cláusula de tu lista de seguimiento ya se puede armar: hora, importe
    frente al valor, tu saldo y la pregunta de si la dejas cargada por EXACTAMENTE ese importe."""
    p = slot.player
    ratio = f" (x{slot.clause / p.market_value:.2f} de su valor {service.m(p.market_value)})" if p.market_value else ""
    if when is None:
        text = (
            f"🔓 {b(p.name)} de {esc(slot.owner_name)} ya está abierta: cláusula {b(service.m(slot.clause))}{ratio}"
            f"{_reach(cash, slot.clause)}"
        )
        return text, service._clause_keyboard(p.id)
    left = when - now
    hours, minutes = int(left.total_seconds() // 3600), int(left.total_seconds() % 3600 // 60)
    text = (
        f"⏰ {b(p.name)} de {esc(slot.owner_name)}: se le acaba el bloqueo de la cláusula a las "
        f"{b(analysis.to_madrid(when).strftime('%H:%M:%S'))} (en {hours} h {minutes:02d} min).\n"
        f"Cláusula ahora: {b(service.m(slot.clause))}{ratio}{_reach(cash, slot.clause)}\n"
        f"{i(f'¿La dejas comprada ya por EXACTAMENTE {service.m(slot.clause)}? La pago en el segundo del desbloqueo. Si el dueño cambia el importe antes, no pago: te aviso y te vuelvo a preguntar.')}"
    )
    return text, service._keyboard([service._arm_row(p.name, p.id, when, slot.clause)])


def changed_message(
    name: str, owner: str, player_id: str, old: int, new: int, when: datetime | None, cash: int | None,
    armed: bool = True,
) -> tuple[str, dict]:
    """El dueño ha cambiado la cláusula. Si la compra estaba armada (`armed`), SIGUE puesta por el
    importe que autorizaste (no se elimina): solo pagará ese importe exacto. Se te pregunta si
    quieres dejarla cargada también por el nuevo (botón); sin respuesta, si en el desbloqueo
    vale el importe nuevo, no se compra nada."""
    sube = "subido" if new > old else "bajado"
    if armed:
        estado = (
            f"Tu compra armada {b('sigue puesta')} por {service.m(old)}: solo pagaré ese importe exacto."
        )
        pregunta = f"¿La dejas cargada también por {service.m(new)}? Si no pulsas nada, y en el desbloqueo vale {service.m(new)}, no compro."
    else:
        estado = f"No he armado nada: pediste {service.m(old)} y ya no vale eso."
        pregunta = f"¿La dejo cargada por {service.m(new)}? Si no pulsas nada, no se compra."
    text = (
        f"⚠️ {b(name)}: {esc(owner)} ha {sube} la cláusula, de {b(service.m(old))} a {b(service.m(new))}.\n"
        f"{estado}{_reach(cash, new)}\n{i(pregunta)}"
    )
    if when is None:
        return text, service._clause_keyboard(player_id)
    return text, service._keyboard([service._arm_row(name, player_id, when, new)])


def parse_wanted(text: str) -> set[str]:
    """Lista de seguimiento, de la variable `CLAUSE_WANTED`: "Rodri, Yamal, 2206" -> {"rodri",
    "yamal", "2206"} (nombres en minúsculas, o ids). Se admite un ":máximo" detrás de cada
    entrada por si venía de la versión anterior, pero se ignora: el importe lo decides tú al ver
    el aviso, nadie arma nada por su cuenta."""
    out = set()
    for part in (text or "").split(","):
        name, sep, tail = part.strip().rpartition(":")
        if not sep or not tail.replace(".", "", 1).isdigit():
            name = part.strip()  # sin ":máximo" (o el ":" es parte del nombre)
        if name.strip():
            out.add(name.strip().lower())
    return out


def wanted_targets(
    slots: list[models.SquadSlot], wanted: set[str], freeze: tuple[datetime, datetime] | None, now: datetime,
) -> list[tuple[models.SquadSlot, datetime | None]]:
    """De la lista de seguimiento, los que ya se pueden armar: faltan menos de `MAX_ARM` para
    poder pagarlos (o ya se puede). (hueco, cuándo se puede pagar; None = ya). No se filtra por
    precio ni por saldo: eso lo miras tú."""
    out = []
    for sl in slots:
        if sl.player.position_id == 5 or not ({sl.player.id, sl.player.name.strip().lower()} & wanted):
            continue
        when = fire_time(sl.clause_locked_until, freeze, now)
        if when is not None and when - now > MAX_ARM:
            continue
        out.append((sl, when))
    return out


def remind_wanted(world: service.World, store, s: Settings) -> list[str]:
    """Avisa (una vez por jugador y desbloqueo) de que un jugador de `CLAUSE_WANTED` ya se puede
    armar (`reminder_message`). NO arma nada: decides tú al verlo (decisión del usuario: prefiere
    ver el importe y su dinero en ese momento)."""
    wanted = parse_wanted(os.environ.get("CLAUSE_WANTED", ""))
    if not wanted:
        return []
    now = datetime.now(timezone.utc)
    out = []
    for sl, when in wanted_targets(world.rival_slots, wanted, world.clause_freeze, now):
        key = f"remind:{sl.player.id}:{when.isoformat() if when else 'open'}"
        if store.get(key):
            continue
        store.set(key, "1")
        text, buttons = reminder_message(sl, when, world.my_cash, now)
        notify.send_telegram(s, f"⭐ {b('Lista de seguimiento')}\n{text}", buttons=buttons)
        out.append(f"aviso {sl.player.name}")
    return out


def simulate(api: FantasyAPI, s: Settings) -> None:
    """SIMULACRO de los dos mensajes de la compra armada, con datos de mentira (Rodri, 80M de
    valor, 86M de cláusula, que se libera hoy a las 21:00:00) pero TU saldo real: el aviso
    "¿la dejas comprada por exactamente X?" y el "el dueño ha cambiado la cláusula, ¿por el
    nuevo importe?". Los botones apuntan a un jugador que no existe (id 99999999): si los pulsas
    el circuito completo se recorre y acaba en un ❌ inofensivo, sin comprar nada."""
    world = service.build_world(api, s, with_trends=False)
    base = next(sl for sl in world.rival_slots if sl.player.position_id != 5)
    now = datetime.now(timezone.utc)
    unlock = analysis.to_madrid(now).replace(hour=21, minute=0, second=0, microsecond=0)
    if unlock <= now:
        unlock += timedelta(days=1)
    fake = replace(
        base, player=replace(base.player, id="99999999", name="Rodri", market_value=80_000_000),
        clause=86_000_000, clause_locked_until=unlock,
    )
    text, buttons = reminder_message(fake, unlock, world.my_cash, now - timedelta(hours=max(0, 5 - (unlock - now).total_seconds() / 3600)))
    notify.send_telegram(s, f"🧪 {b('SIMULACRO')} — así te avisaría\n\n⭐ {b('Lista de seguimiento')}\n{text}", buttons=buttons)
    text2, buttons2 = changed_message("Rodri", base.owner_name, "99999999", 86_000_000, 92_000_000, unlock, world.my_cash)
    notify.send_telegram(
        s, f"🧪 {b('SIMULACRO')} — y si el dueño la cambia después de que la dejaras cargada\n\n{text2}", buttons=buttons2,
    )


def _fresh(api: FantasyAPI, world: service.World, owner_team_id: str, owner_name: str, player_id: str):
    api.clear_cache()
    squad = models.parse_squad(api.team(world.league_id, owner_team_id), owner_team_id, owner_name)
    return next((sl for sl in squad if sl.player.id == player_id), None)


def _mine_now(api: FantasyAPI, world: service.World, player_id: str) -> bool:
    api.clear_cache()
    squad = models.parse_squad(api.team(world.league_id, world.my_team_id), world.my_team_id, "yo")
    return any(sl.player.id == player_id for sl in squad)


def run(
    api: FantasyAPI, s: Settings, player_id: str, expected: int | None = None, dry: bool = False,
    unlock_in: float | None = None,
) -> None:
    """`expected`: el importe que viste y autorizaste (viaja en el botón; sin él, el de ahora).
    `dry` + `unlock_in` (segundos) SOLO para pruebas: simula un desbloqueo dentro de N segundos y
    hace todo igual (esperas, cuenta atrás, revalidaciones) menos el pago, que se da por hecho."""
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
    amount = expected or slot.clause
    if slot.clause != amount:  # cambió entre el aviso y tu confirmación: no se arma nada
        text, buttons = changed_message(name, owner, player_id, amount, slot.clause, fire_at, cash, armed=False)
        notify.send_telegram(s, text, buttons=buttons)
        return
    if cash is not None and amount > cash:
        notify.send_telegram(
            s, f"❌ {b('No armo la compra')}\nTu saldo ({service.m(cash)}) no llega a la cláusula de {b(name)} "
               f"({service.m(amount)}); no se puede pagar una cláusula endeudándose.",
        )
        return
    if fire_at is not None and fire_at - now() > MAX_ARM:
        notify.send_telegram(
            s, f"❌ {b('No armo la compra')}\nFalta demasiado para el desbloqueo de {b(name)} "
               f"({analysis.to_madrid(fire_at).strftime('%d/%m %H:%M:%S')}): un trabajo de GitHub solo aguanta ~6 h. Arma más cerca.",
        )
        return

    when = f"a las {analysis.to_madrid(fire_at).strftime('%H:%M:%S')} (hora de España)" if fire_at else "ya"
    notify.send_telegram(
        s, f"🎯 {tag}{b('Compra armada')}\n{b(name)} de {esc(owner)}: la pagaré {when} por exactamente "
           f"{b(service.m(amount))}.\n{i('Si el dueño cambia el importe antes, cancelo y te vuelvo a preguntar.')}",
    )

    warned: set[int] = set()  # importes nuevos de los que ya te he avisado (una vez por importe)
    known_cash: list[int | None] = [cash]  # último saldo leído (el camino crítico no hace llamadas de red)

    def check() -> models.SquadSlot | None:
        """Relee la plantilla del dueño. Si la cláusula ya no vale lo autorizado, AVISA (una vez
        por importe nuevo) y te pregunta, pero la compra armada SIGUE puesta: no se elimina.
        Devuelve la fila actual, o None si el jugador ya no está con ese dueño."""
        nonlocal ptid
        fresh = _fresh(fast, world, slot.owner_team_id, owner, player_id)
        if fresh is None:
            return None
        ptid = fresh.player_team_id
        known_cash[0] = service.resolve_league(fast, s)[2]
        if fresh.clause != amount and fresh.clause not in warned:
            warned.add(fresh.clause)
            text, buttons = changed_message(
                name, owner, player_id, amount, fresh.clause,
                fire_time(fresh.clause_locked_until, world.clause_freeze, now()), known_cash[0],
            )
            notify.send_telegram(s, text, buttons=buttons)
        return fresh

    def gone() -> None:
        notify.send_telegram(s, f"❌ {b('Compra armada sin efecto')}\n{b(name)} ya no está con {esc(owner)}.")

    def refuse(fresh: models.SquadSlot, cash_now: int | None) -> bool:
        """En el momento de pagar: ¿algo impide hacerlo por el importe autorizado? Si sí, lo
        cuenta y devuelve True (no se paga)."""
        if fresh.clause != amount:
            notify.send_telegram(
                s, f"🚫 {b('No he comprado')} a {b(name)}\nLlegó el desbloqueo con la cláusula en "
                   f"{b(service.m(fresh.clause))}, distinta de los {service.m(amount)} que autorizaste. "
                   f"{i('No pago un importe que no has confirmado.')}",
            )
            return True
        if cash_now is not None and amount > cash_now:
            notify.send_telegram(
                s, f"🚫 {b('No he comprado')} a {b(name)}\nTu saldo ({service.m(cash_now)}) no llega a "
                   f"{service.m(amount)}; no se puede pagar una cláusula endeudándose.",
            )
            return True
        return False

    ptid = slot.player_team_id
    counter_id = None
    last_edit = 0.0
    next_check = time.time() + CHECK_EVERY
    prefetched = False
    latest = slot
    if fire_at is not None:
        while (left := (fire_at - now()).total_seconds()) > FIRE_LEAD:
            if (not prefetched and left <= PREFETCH_AT) or (left > PREFETCH_AT + 5 and time.time() >= next_check):
                prefetched = prefetched or left <= PREFETCH_AT
                next_check = time.time() + CHECK_EVERY
                fresh = check()
                if fresh is None:
                    if counter_id is not None:
                        notify.edit_message(s, counter_id, "❌ Compra armada sin efecto.")
                    gone()
                    return
                latest = fresh
            if left <= COUNTDOWN_FROM:
                if counter_id is None or time.time() - last_edit >= COUNTDOWN_STEP - 0.5:
                    text = f"⏱️ {tag}{b(name)}: {int(left)} s para el desbloqueo · {service.m(amount)} 🎯 armada"
                    if counter_id is None:
                        counter_id = notify.send_message(s, text)
                    else:
                        notify.edit_message(s, counter_id, text)
                    last_edit = time.time()
                time.sleep(min(0.5, max(0.01, left - FIRE_LEAD)))
            else:
                time.sleep(min(1.0, max(0.05, left - COUNTDOWN_FROM), max(0.05, next_check - time.time())))
    else:
        fresh = check()
        if fresh is None:
            gone()
            return
        latest = fresh
    # Última puerta antes de pagar, SIN llamadas de red (cada una cuesta ~0.5 s justo cuando
    # cuenta): importe autorizado y saldo según la comprobación de hace unos segundos (T-25 s).
    if refuse(latest, known_cash[0]):
        if counter_id is not None:
            notify.edit_message(s, counter_id, "🚫 No he comprado: el importe ya no es el autorizado.")
        return

    # Disparo: desde FIRE_LEAD s antes del desbloqueo se insiste cada RETRY_EVERY hasta que entra
    # o pasa la ventana, SIEMPRE por el mismo importe. Un error antes de tiempo (aún bloqueada) se
    # reintenta sin más; un 409 ("importe no actualizado") DESPUÉS del desbloqueo significa que
    # el dueño cambió la cláusula: se comprueba (como mucho cada RECHECK_EVERY s) y, si es así,
    # se cancela y se pregunta; si un intento cuelga o el jugador ya aparece en tu plantilla, se
    # da por pagado sin volver a pagar. `already_owned`: no lo pagó ESTE trabajo (lo consiguió
    # otra ejecución armada en paralelo, o ya lo tenías) — se avisa distinto para no dar a
    # entender que hubo una compra nueva o que el otro aviso pudo fallar.
    started = now()
    deadline = started + FIRE_WINDOW
    last_error = ""
    attempts = 0
    paid = False
    already_owned = False
    last_recheck = 0.0
    while now() < deadline and not paid:
        attempts += 1
        try:
            if not dry:
                fast.pay_clause(league_id, ptid, amount)
            paid = True
        except HttpError as exc:
            last_error = str(exc)
            unlocked = fire_at is None or now() >= fire_at
            if exc.status == 409 and unlocked and time.time() - last_recheck >= RECHECK_EVERY:
                last_recheck = time.time()
                fresh = check()
                if fresh is None:
                    gone()
                    return
                if refuse(fresh, None):
                    return
            time.sleep(RETRY_EVERY)
        except Exception as exc:  # cuelgue de red: puede haberse pagado igualmente
            last_error = str(exc)
            if _mine_now(fast, world, player_id):
                paid = True
                already_owned = True
    if not paid and not dry and _mine_now(fast, world, player_id):
        paid = True
        already_owned = True

    if paid and not already_owned:
        took = (now() - (fire_at or started)).total_seconds()  # desde el desbloqueo (negativo = entró antes, no debería)
        msg = (
            f"✅ {tag}{b('¡Cláusula pagada!')}\n{b(name)} de {esc(owner)} por {b(service.m(amount))}\n"
            f"{i(f'Pagada {took:+.1f} s respecto al desbloqueo ({attempts} intentos).')}"
        )
    elif paid:  # already_owned: ya era tuyo cuando este trabajo fue a pagar; no ha hecho nada
        msg = (
            f"ℹ️ {tag}{b(name)} de {esc(owner)}: ya era tuyo al llegar el desbloqueo.\n"
            f"{i('Esta compra armada no ha pagado nada (se consiguió por otra vía justo antes).')}"
        )
    else:
        msg = (
            f"❌ {b('No conseguí la cláusula')} de {b(name)}\n{esc(last_error[:300] or 'sin respuesta')}\n"
            f"{i('Puede que alguien se adelantara; comprueba la plantilla del dueño.')}"
        )
    if counter_id is not None:
        notify.edit_message(s, counter_id, msg)
    else:
        notify.send_telegram(s, msg)
