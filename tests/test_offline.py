"""Tests sin red: API falsa con payloads con la forma documentada por la comunidad."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["FANTASY_DATA_DIR"] = tempfile.mkdtemp()

from fantasy_agent import analysis, auth, lineup, models, service  # noqa: E402
from fantasy_agent.config import load_settings  # noqa: E402
from fantasy_agent.storage import Store  # noqa: E402

NOW = datetime.now(timezone.utc)


def pm(pid, name, pos, value, points, avg, status="ok", team="Barcelona"):
    return {"id": pid, "nickname": name, "positionId": pos, "marketValue": value, "points": points,
            "averagePoints": avg, "playerStatus": status, "team": {"name": team}}


def squad(prefix, clause_lock=None, cheap_clause=False):
    players = []
    layout = [(1, 2), (2, 5), (3, 5), (4, 4)]
    n = 0
    for pos, count in layout:
        for i in range(count):
            n += 1
            value = 5_000_000 + n * 1_000_000
            clause = int(value * (1.1 if cheap_clause and n == 3 else 2.0))
            players.append({
                "playerTeamId": f"pt-{prefix}{n}",
                "playerMaster": pm(f"{prefix}{n}", f"{prefix}-J{n}", pos, value, 20 + n, 3 + n / 4),
                "buyoutClause": clause,
                "buyoutClauseLockedEndTime": clause_lock,
            })
    return {"players": players}


def history(start, pct_per_day, days=8):
    out, v = [], start
    for d in range(days):
        out.append({"date": (NOW - timedelta(days=days - d)).isoformat(), "marketValue": int(v)})
        v *= 1 + pct_per_day / 100
    return out


class FakeAPI:
    def leagues(self):
        return [{"id": "L1", "name": "Amigos", "team": {"id": "T1", "money": 60_000_000}}]

    def standing(self, lid):
        return [
            {"points": 120, "team": {"id": "T1", "name": "Mío", "teamValue": 200_000_000, "manager": {"id": "U1", "managerName": "Yo"}}},
            {"points": 140, "team": {"id": "T2", "name": "Pepe FC", "teamValue": 230_000_000, "manager": {"id": "U2", "managerName": "Pepe"}}},
            {"points": 100, "team": {"id": "T3", "name": "Lola FC", "teamValue": 190_000_000, "manager": {"id": "U3", "managerName": "Lola"}}},
        ]

    def team(self, lid, tid):
        if tid == "T1":
            return squad("me", cheap_clause=True)
        if tid == "T2":
            return squad("pepe", clause_lock=(NOW + timedelta(hours=5)).isoformat(), cheap_clause=True)
        return squad("lola", cheap_clause=True)

    def market(self, lid):
        return [
            {"id": "L100", "playerMaster": pm("m1", "Chollo", 3, 10_000_000, 60, 7.5), "salePrice": 9_000_000,
             "expirationDate": (NOW + timedelta(hours=10)).isoformat(), "numberOfBids": 1},
            {"id": "L200", "playerMaster": pm("me3", "me-J3", 2, 8_000_000, 25, 3.75), "salePrice": 8_000_000,
             "expirationDate": (NOW + timedelta(hours=10)).isoformat(),
             "sellerTeam": {"manager": {"managerName": "Yo"}}},
            {"playerMaster": pm("m2", "Lesionado", 4, 20_000_000, 40, 5, status="injured"), "salePrice": 21_000_000,
             "expirationDate": (NOW + timedelta(hours=10)).isoformat()},
            {"playerMaster": pm("m3", "NoEsPujable", 3, 10_000_000, 90, 9), "salePrice": 9_500_000,
             "expirationDate": (NOW + timedelta(hours=10)).isoformat(), "sellerTeam": {"manager": {"managerName": "Pepe"}}},
        ]

    def market_value_history(self, pid):
        return history(10_000_000, 2.0 if pid == "m1" else -1.5)

    def me(self):
        return {"id": "U1"}


class Tests(unittest.TestCase):
    def setUp(self):
        self.s = load_settings()

    def test_world_and_report(self):
        world = service.build_world(FakeAPI(), self.s)
        self.assertEqual(world.my_team_id, "T1")
        self.assertEqual(world.my_cash, 60_000_000)
        self.assertEqual(len(world.my_slots), 16)
        self.assertEqual(len(world.rival_slots), 32)
        report = service.full_report(world, self.s, news=None)
        print("\n" + report)
        self.assertIn("Chollo", report)
        self.assertIn("se libera", report)            # Pepe: bloqueadas 5h
        self.assertIn("pagable ya", report)           # Lola: cláusula barata abierta
        self.assertNotIn("(tuyo)", report)            # ya no avisamos de riesgo en jugadores propios
        self.assertIn("Once recomendado", report)

    def test_market_ranking(self):
        world = service.build_world(FakeAPI(), self.s)
        opps = service._opportunities(world)
        names = [o.item.player.name for o in opps]
        self.assertLess(names.index("Chollo"), names.index("Lesionado"))
        txt = service.market_report(world)
        self.assertIn("Chollo", txt)
        # Un jugador que "vende" otro entrenador de la liga no es pujable de verdad
        # (solo se consigue por cláusula) y no debe salir como oportunidad de mercado.
        self.assertNotIn("NoEsPujable", txt)

    def test_investment_picks_excludes_lineup_targets(self):
        # Petición del usuario (2026-09-22): el mismo jugador (Chollo) salía en "Mercado para
        # tu once" Y en "Inversión", con una puja "máxima" distinta para el MISMO anuncio (14
        # días vs 3 — fichaje vs flipeo). Ya era la regla que aplicaba `flip._buy` para no
        # comprar y revender a alguien que en realidad querías conservar; ahora vive en
        # `_investment_picks` para que los informes coincidan con el flipeo real.
        world = service.build_world(FakeAPI(), self.s)
        market_names = {item.player.name for item, _, _ in service._market_picks(world)}
        investment_names = {item.player.name for item, _ in service._investment_picks(world)}
        self.assertIn("Chollo", market_names)
        self.assertFalse(market_names & investment_names)
        self.assertNotIn("Chollo", investment_names)
        self.assertEqual(service.investment_report(world), "")  # nada que ofrecer, ya está en "el once"

    def test_action_buttons(self):
        world = service.build_world(FakeAPI(), self.s)
        codes = lambda kb: [row[0]["callback_data"] for row in kb["inline_keyboard"]] if kb else []  # noqa: E731

        # Pujar: solo anuncios de LaLiga, nunca lo que vende otro mánager ni tus propios anuncios.
        # Chollo pide 9M pero vale 10M: el mínimo es el mayor (el servidor rechaza pujar por
        # debajo del valor de mercado, error 030.01.01) y el botón enseña la cantidad real. Al
        # subir un 2%/día lleva también puja con margen, y por puntos (7.5 de media) un techo.
        kb = codes(service.market_keyboard(world))
        self.assertEqual(kb[0], "b:L100:10000000")
        self.assertTrue(all(c.startswith("b:L100:") for c in kb))
        self.assertGreater(len(kb), 1)
        amounts = [int(c.split(":")[2]) for c in kb]
        self.assertEqual(amounts, sorted(amounts))
        self.assertIn("Pujar Chollo · mínimo 10.00M", service.market_keyboard(world)["inline_keyboard"][0][0]["text"])

        # Retirar: tu jugador puesto a la venta (me3, anuncio L200) lleva su botón.
        text, kb = service.my_listings_report(world)
        self.assertIn("me-J3", text)
        self.assertEqual(codes(kb), ["w:me3"])

        # Vender: candidato por tendencia a la baja con precio de compra conocido, salvo si ya
        # está en venta (me3) — a ese solo se le marca "ya en venta".
        store = Store(Path(tempfile.mkdtemp()) / "t.sqlite3")
        store.set("buy_price:me3", "9000000")
        store.set("buy_price:me4", "9000000")
        self.assertEqual(codes(service.sell_keyboard(world, store)), ["s:me4"])
        report = service.sell_candidates_report(world, store)
        self.assertIn("ya en venta", report)

    def test_my_transactions_alerts_on_rival_sale(self):
        # El usuario pidió enterarse cuando un rival vende a alguien de su plantilla (le sube
        # el saldo, más margen para pagarle cláusulas o ganarle pujas) — mismo feed de
        # actividad que ya se pedía para tus propios movimientos, sin llamada extra a la API.
        class ActivityAPI(FakeAPI):
            def activity(self, lid, index=0):
                return [{
                    "id": "500", "activityTypeId": 33, "user1Id": "U2", "playerMasterId": "pepe3",
                    "amount": 12_000_000, "createdAt": NOW.isoformat(),
                }]

        api = ActivityAPI()
        world = service.build_world(api, self.s)
        store = Store(Path(tempfile.mkdtemp()) / "act.sqlite3")
        store.set("last_activity_id", "0")  # sin esto, la primera pasada es "cold start" (no avisa)
        tx = service.my_transactions(api, world, store)
        self.assertEqual(len(tx), 1)
        self.assertIn("Venta rival", tx[0])
        self.assertIn("Pepe", tx[0])
        self.assertIn("pepe-J3", tx[0])
        # No se repite en la siguiente pasada (marca de agua ya avanzada).
        self.assertEqual(service.my_transactions(api, world, store), [])

    def test_pending_bid_changes_instead_of_new_bid(self):
        from fantasy_agent import flip

        raw = {"id": "L900", "playerMaster": pm("g1", "Ya pujado", 3, 10_000_000, 60, 7.5), "salePrice": 10_000_000,
               "expirationDate": (NOW + timedelta(hours=10)).isoformat(),
               "bid": {"id": "B77", "money": 10_500_000, "status": "pending"}}
        item = models.parse_market([raw])[0]
        self.assertEqual((item.my_bid_id, item.my_bid), ("B77", 10_500_000))
        # Una puja ya resuelta no cuenta como pendiente.
        done = dict(raw, bid={"id": "B78", "money": 1, "status": "won"})
        self.assertEqual(models.parse_market([done])[0].my_bid_id, "")

        world = service.build_world(FakeAPI(), self.s)
        rows = service._bid_rows(world, item, analysis.Trend(0, 0, 0), False)
        self.assertEqual([r[0]["callback_data"] for r in rows], ["u:L900:B77:10000000"])  # bajar al mínimo
        self.assertIn("puja 10.50M → mínimo 10.00M", rows[0][0]["text"])
        # El flipeo no toca un anuncio donde ya hay una puja tuya.
        self.assertEqual(flip.plan_bids([(item, analysis.Trend(2.0, 4.0, 8.0))], 100_000_000, 0, [], set(), 0), [])

    def test_flip_breaker_and_offers_watch(self):
        from fantasy_agent import flip

        now = NOW
        at = lambda days: (now - timedelta(days=days)).isoformat()  # noqa: E731
        loss = lambda p, d: {"profit": p, "at": at(d)}  # noqa: E731
        m5 = 5_000_000
        self.assertIsNone(flip.breaker_reason([], now, m5))
        self.assertIn("3 flips seguidos", flip.breaker_reason([loss(-1, 3), loss(-1, 2), loss(-1, 1)], now, m5))
        # Una ganancia en medio corta la racha; la pérdida acumulada (-3.5M) queda por debajo del tope.
        self.assertIsNone(flip.breaker_reason([loss(-1_000_000, 3), loss(+500_000, 2), loss(-3_000_000, 1)], now, m5))
        # Pérdida acumulada por encima del tope en 14 días -> frena; fuera de la ventana no cuenta.
        self.assertIn("perdidos en 14 días", flip.breaker_reason([loss(-6_000_000, 5), loss(+100, 1)], now, m5))
        self.assertIsNone(flip.breaker_reason([loss(-6_000_000, 20), loss(+100, 1)], now, m5))

        class OfferAPI(FakeAPI):
            def player_offers(self, lid, player_team_id):
                return [{"id": "555", "money": 8_500_000, "isFromMarket": True}] if player_team_id == "pt-me3" else []

        sent = []
        original = flip.notify.send_telegram
        flip.notify.send_telegram = lambda settings, text, buttons=None: sent.append((text, buttons))
        try:
            world = service.build_world(OfferAPI(), self.s)
            store = Store(Path(tempfile.mkdtemp()) / "o.sqlite3")
            store.set("buy_price:me3", "8000000")
            self.assertEqual(flip.watch_offers(OfferAPI(), world, store, self.s), ["oferta me-J3"])
            self.assertEqual(flip.watch_offers(OfferAPI(), world, store, self.s), [])  # ya vista: no se repite
        finally:
            flip.notify.send_telegram = original
        self.assertEqual(len(sent), 1)
        text, buttons = sent[0]
        self.assertIn("aceptar", text)  # con beneficio, la regla dice aceptar
        rows = buttons["inline_keyboard"]
        self.assertEqual([r[0]["callback_data"] for r in rows], ["o:me3:555:8500000", "r:me3:555"])

    def test_accept_reject_offer_actions(self):
        from fantasy_agent import cli

        calls = []

        class WriteAPI(FakeAPI):
            def accept_offer(self, lid, market_id, offer_id, amount):
                calls.append(("accept", lid, market_id, offer_id, amount))
                return {"status": "ok"}

            def reject_offer(self, lid, market_id, offer_id):
                calls.append(("reject", lid, market_id, offer_id))
                return {"status": "ok"}

        api = WriteAPI()
        msg = cli._act_accept_offer(api, self.s, "me3:555:8500000")
        self.assertIn("Oferta aceptada", msg)
        self.assertEqual(calls, [("accept", "L1", "L200", "555", 8_500_000)])

        calls.clear()
        msg = cli._act_reject_offer(api, self.s, "me3:555")
        self.assertIn("Oferta rechazada", msg)
        self.assertEqual(calls, [("reject", "L1", "L200", "555")])

        # Un jugador que ya no está en venta (no aparece en el mercado con "sellerTeam": "Yo"):
        # no hay `market_id` con el que llamar a la API, así que no acepta nada.
        with self.assertRaises(RuntimeError):
            cli._act_accept_offer(api, self.s, "me1:555:9000000")

    def test_clause_arm_buttons_and_fire_time(self):
        from dataclasses import replace as dc_replace
        from fantasy_agent import clause_snipe

        world = service.build_world(FakeAPI(), self.s)
        messages, alerts = service.clauses_report(world, self.s, None)
        armed = [
            kb["inline_keyboard"][0][0] for _, kb in messages
            if kb and kb["inline_keyboard"][0][0]["callback_data"].startswith("a:")
        ]
        # Pepe tiene su cláusula bloqueada 5 h (< 5 h 45): se puede armar; el botón lleva la hora y
        # el IMPORTE EXACTO (en el texto y en el código): lo que confirmas es lo que se paga.
        slot = next(sl for sl in world.rival_slots if sl.player.id == "pepe3")
        self.assertEqual([b["callback_data"] for b in armed], [f"a:pepe3:{slot.clause}"])
        self.assertIn("Comprar pepe-J3 al desbloquearse (", armed[0]["text"])
        self.assertIn(f"por {service.m(slot.clause)}", armed[0]["text"])

        far = dc_replace(slot, clause_locked_until=NOW + timedelta(hours=8))
        self.assertIsNone(service.alert_keyboard(far, "unlock_soon", NOW))  # demasiado lejos para un trabajo de 6 h
        self.assertEqual(service.alert_keyboard(slot, "open_affordable", NOW)["inline_keyboard"][0][0]["callback_data"], "c:pepe3")

        unlock = NOW + timedelta(hours=2)
        self.assertEqual(clause_snipe.fire_time(unlock, None, NOW), unlock)
        self.assertIsNone(clause_snipe.fire_time(NOW - timedelta(minutes=1), None, NOW))  # ya abierta
        # Si el desbloqueo cae dentro de la congelación de cláusulas, se espera a que termine.
        freeze = (NOW + timedelta(hours=1), NOW + timedelta(hours=3))
        self.assertEqual(clause_snipe.fire_time(unlock, freeze, NOW), freeze[1])
        self.assertEqual(clause_snipe.fire_time(unlock, (NOW + timedelta(hours=3), NOW + timedelta(hours=4)), NOW), unlock)
        # Abierta pero en plena congelación: tampoco se puede pagar hasta que acabe.
        self.assertEqual(clause_snipe.fire_time(None, (NOW - timedelta(hours=1), NOW + timedelta(hours=1)), NOW),
                         NOW + timedelta(hours=1))

    def test_clause_wanted_list(self):
        import dataclasses

        from fantasy_agent import clause_snipe

        # El ":máximo" de la versión anterior se admite pero se ignora (el importe lo decides tú).
        self.assertEqual(
            clause_snipe.parse_wanted("Rodri:90, Yamal:150.5, 2206,  ,Lamine Yamal"),
            {"rodri", "yamal", "2206", "lamine yamal"},
        )
        world = service.build_world(FakeAPI(), self.s)
        now = datetime.now(timezone.utc)
        slots = world.rival_slots
        pepe3 = next(sl for sl in slots if sl.player.id == "pepe3")  # cláusula 8.8M, bloqueada ~5 h
        targets = lambda wanted: clause_snipe.wanted_targets(slots, wanted, None, now)  # noqa: E731

        # Por nombre (sin distinguir mayúsculas) o por id, sin filtrar por precio ni por saldo.
        self.assertEqual([sl.player.id for sl, _ in targets({"pepe-j3"})], ["pepe3"])
        self.assertEqual([sl.player.id for sl, _ in targets({"pepe3"})], ["pepe3"])
        self.assertIsNotNone(targets({"pepe3"})[0][1])  # todavía bloqueada: hay hora de desbloqueo
        # Si falta demasiado (más de lo que aguanta un trabajo de GitHub), aún no se avisa.
        far = dataclasses.replace(pepe3, clause_locked_until=now + timedelta(hours=9))
        self.assertEqual(clause_snipe.wanted_targets([far], {"pepe3"}, None, now), [])
        self.assertEqual(targets({"nadie"}), [])

        # El aviso NO arma nada: manda el mensaje con la hora, la cláusula, tu saldo y el botón.
        from fantasy_agent import clause_snipe as cs
        sent, original, env = [], cs.notify.send_telegram, os.environ.get("CLAUSE_WANTED")
        cs.notify.send_telegram = lambda settings, text, buttons=None: sent.append((text, buttons))
        os.environ["CLAUSE_WANTED"] = "pepe-J3"
        try:
            store = Store(Path(tempfile.mkdtemp()) / "w.sqlite3")
            self.assertEqual(cs.remind_wanted(world, store, self.s), ["aviso pepe-J3"])
            self.assertEqual(cs.remind_wanted(world, store, self.s), [])  # una vez por desbloqueo
        finally:
            cs.notify.send_telegram = original
            os.environ.pop("CLAUSE_WANTED") if env is None else os.environ.__setitem__("CLAUSE_WANTED", env)
        self.assertEqual(len(sent), 1)
        text, buttons = sent[0]
        self.assertIn("se le acaba el bloqueo de la cláusula a las", text)
        self.assertIn("Tu saldo", text)
        self.assertEqual(buttons["inline_keyboard"][0][0]["callback_data"], f"a:pepe3:{pepe3.clause}")
        self.assertIn("EXACTAMENTE", text)

        # Si el dueño cambia el importe: el armado SIGUE puesto (no se elimina) y se pregunta por el nuevo.
        armed_text, armed_kb = cs.changed_message("Rodri", "Pepe", "9", 86_000_000, 92_000_000, now + timedelta(hours=2), 140_000_000)
        self.assertIn("sigue puesta", armed_text)
        self.assertIn("92.00M", armed_text)
        self.assertEqual(armed_kb["inline_keyboard"][0][0]["callback_data"], "a:9:92000000")
        # Si el cambio llegó ANTES de armar, no hay nada armado que mantener.
        idle_text, _ = cs.changed_message("Rodri", "Pepe", "9", 86_000_000, 80_000_000, None, None, armed=False)
        self.assertIn("No he armado nada", idle_text)

    def test_snipe_plan(self):
        from fantasy_agent import snipe

        def listing(n, bids, my_bid, price=10_000_000, seller=None):
            raw = {"id": f"S{n}", "playerMaster": pm(f"s{n}", f"Snipe{n}", 3, 10_000_000, 60, 7.5), "salePrice": price,
                   "expirationDate": (NOW + timedelta(minutes=2)).isoformat(), "numberOfBids": bids}
            if my_bid:
                raw["bid"] = {"id": f"B{n}", "money": my_bid, "status": "pending"}
            if seller:
                raw["sellerTeam"] = {"manager": {"managerName": seller}}
            return models.parse_market([raw])[0]

        items = [
            listing(1, 1, 11_000_000),                 # solo yo, por encima del mínimo -> bajar a 10M
            listing(2, 2, 11_000_000),                 # alguien más pujó -> no tocar
            listing(3, 1, 10_000_000),                 # solo yo, ya en el mínimo -> nada
            listing(4, 0, 0),                          # sin pujas mías
            listing(5, 1, 12_000_000, seller="Pepe"),  # no es de LaLiga
        ]
        plan = snipe.plan_reductions(items)
        self.assertEqual([(it.listing_id, amount) for it, amount in plan], [("S1", 10_000_000)])
        # Protegidos: TOP de liga (por id) o lista manual (por nombre o id): se dejan como están.
        self.assertEqual(snipe.plan_reductions(items, top_ids={"s1"}), [])
        self.assertEqual(snipe.plan_reductions(items, skip={"snipe1"}), [])
        self.assertEqual(snipe.plan_reductions(items, skip={"s1"}), [])
        # El cierre real es 2 minutos antes del expirationDate (21:00 vs 21:02).
        self.assertEqual(snipe.close_time(items[0]), items[0].expires - timedelta(minutes=2))

    def test_bid_plan(self):
        rising = analysis.Trend(2.0, 6.0, 12.0)
        plan = analysis.bid_plan(10_000_000, 10_000_000, rising, 7.5, 0.5, False)
        self.assertEqual(plan.expected, 10_612_080)  # 10M al 2%/día (el menor de d1 y d3/3) durante 3 días
        self.assertEqual(plan.margin, 10_307_000)  # mínimo + la mitad de la ganancia esperada
        # Una subida que se frena (hoy 0.18%, media de 3 días 1.47%) proyecta con el ritmo de HOY.
        slowing = analysis.bid_plan(8_288_386, 8_288_386, analysis.Trend(0.18, 4.4, 23.0), 3.3, 0.0, False)
        self.assertIsNone(slowing.margin)
        self.assertEqual(plan.max_bid, 15_000_000)  # techo por puntos: 7.5 pts / 0.5 pts-por-M
        self.assertEqual(plan.max_reason, "ceiling")
        # Sin subida no hay margen que justificar; sin referencia de mercado, ni techo.
        flat = analysis.bid_plan(10_000_000, 10_000_000, analysis.Trend(0, 0, 0), 7.5, 0, False)
        self.assertEqual((flat.margin, flat.expected, flat.max_bid), (None, None, None))
        # Un techo por puntos que no supera la puja anterior no gana el "máximo" — pero el
        # valor completo proyectado (sin repartir con nadie, a diferencia de "con margen") sí
        # suele superarlo, así que el botón igualmente sale con ESE motivo.
        low = analysis.bid_plan(10_000_000, 10_000_000, rising, 7.5, 0.74, False)
        self.assertEqual(low.max_bid, 10_613_000)
        self.assertEqual(low.max_reason, "value")
        # Y nunca pasa del doble del mínimo, aunque los puntos "digan" mucho más.
        cheap = analysis.bid_plan(1_000_000, 1_000_000, analysis.Trend(0, 0, 0), 3.5, 0.29, False)
        self.assertEqual(cheap.max_bid, 2_000_000)

    def test_bid_plan_14_days_for_lineup_targets(self):
        # Petición del usuario (2026-09-22): un fichaje para el once se queda contigo 14 días
        # (hasta que se puede revender por cláusula), no 3 — con `days=14` la puja "con margen"
        # puede ofrecer más sin estar regalando dinero, porque el horizonte real es más largo.
        rising = analysis.Trend(2.0, 6.0, 12.0)
        plan_3d = analysis.bid_plan(10_000_000, 10_000_000, rising, 7.5, 0.5, False)
        plan_14d = analysis.bid_plan(10_000_000, 10_000_000, rising, 7.5, 0.5, False, days=14)
        self.assertEqual(plan_14d.expected, 13_194_788)  # ritmo MAYOR entre 7 días (1.71%) y 3 días (2.0%)
        self.assertEqual(plan_14d.margin, 11_598_000)
        self.assertGreater(plan_14d.margin, plan_3d.margin)
        # Igual que a 3 días: sin subida no hay margen que justificar, tampoco a 14.
        flat = analysis.bid_plan(10_000_000, 10_000_000, analysis.Trend(0, 0, 0), 7.5, 0, False, days=14)
        self.assertIsNone(flat.margin)

    def test_competition_bid_cushion(self):
        # Caso real (2026-09-21): perdimos a Yuri por solo 1M pujando justo el minimo, sin
        # ninguna senal de competencia visible en el propio boton. Sin pujas puestas ni rivales
        # con saldo de sobra, no hay motivo para regalar dinero de mas.
        self.assertIsNone(analysis.competition_bid(10_000_000, 0, 0))
        # Con 2 pujas ya puestas (senal fuerte: rivales confirmados, no solo con saldo) sube
        # el colchon; se redondea a miles al alza y nunca pasa del 8%.
        self.assertEqual(analysis.competition_bid(10_000_000, 2, 0), 10_400_000)
        self.assertEqual(analysis.competition_bid(10_000_000, 10, 10), 10_800_000)  # tope 8%
        # Con tendencia alcista, el colchón (10.2M) queda por debajo de la puja "con margen"
        # (10.307M), pero el valor completo proyectado (10.613M, sin repartir con nadie) sí la
        # supera: el "máximo" sale igualmente, con ESE motivo.
        rising = analysis.Trend(2.0, 6.0, 12.0)
        plan = analysis.bid_plan(10_000_000, 10_000_000, rising, 7.5, 0.0, False, existing_bids=1)
        self.assertEqual(plan.max_bid, 10_613_000)
        self.assertEqual(plan.max_reason, "value")
        # Sin tendencia (sin valor proyectado que ofrecer), el colchón por competencia es lo
        # único que puede justificar un "máximo".
        flat = analysis.bid_plan(10_000_000, 10_000_000, analysis.Trend(0, 0, 0), 7.5, 0.0, False, existing_bids=1)
        self.assertEqual(flat.max_bid, 10_200_000)
        self.assertEqual(flat.max_reason, "cushion")

    def test_buy_sections_one_message_per_player(self):
        # Pedido del usuario (2026-09-22): con varios candidatos, un solo mensaje con todos los
        # botones al final no dejaba ver cuál era de quién. Ahora cada jugador es su propio
        # (texto, teclado), con el título de la lista pegado al primero.
        world = service.build_world(FakeAPI(), self.s)
        sections = service.buy_sections(world)
        self.assertGreaterEqual(len(sections), 1)
        first_text, first_kb = sections[0]
        self.assertIn("Mercado para tu once", first_text)
        self.assertIn("Chollo", first_text)
        self.assertTrue(all(row[0]["callback_data"].startswith("b:L100:") for row in first_kb["inline_keyboard"]))
        # Cada sección lleva su propio teclado (o ninguno), nunca el de otro jugador.
        for text, kb in sections:
            if kb:
                ids = {row[0]["callback_data"].split(":")[1] for row in kb["inline_keyboard"]}
                self.assertEqual(len(ids), 1)

    def test_fixture_factor_rival_and_home_away(self):
        # Petición del usuario (2026-09-22): dos jugadores con la misma media no deberían
        # puntuar igual si uno juega contra una defensa floja y otro contra una fuerte.
        baseline_def, baseline_att = 4.0, 4.0
        # Delantero (posición 4) contra una defensa floja (2.0, la mitad de la media de liga):
        # sube el factor, tope +15%, y encima juega en casa: se suman los dos.
        weak_defense = analysis.fixture_factor(4, True, 2.0, None, baseline_def, baseline_att)
        self.assertEqual(weak_defense, round(1.15 * 1.06, 3))
        # El mismo delantero contra una defensa fuerte (8.0, el doble de la media) y fuera de
        # casa: baja por los dos lados.
        strong_defense = analysis.fixture_factor(4, False, 8.0, None, baseline_def, baseline_att)
        self.assertEqual(strong_defense, round(0.85 * 0.94, 3))
        self.assertLess(strong_defense, weak_defense)
        # Un defensa (posición 2) mira el ATAQUE rival, no su defensa.
        vs_weak_attack = analysis.fixture_factor(2, True, None, 2.0, baseline_def, baseline_att)
        self.assertEqual(vs_weak_attack, round(1.15 * 1.06, 3))
        # Sin dato de rival, solo cuenta casa/fuera.
        no_data = analysis.fixture_factor(4, True, None, None, baseline_def, baseline_att)
        self.assertEqual(no_data, 1.06)

    def test_market_verdict_recovering_scores_like_sustained_rally(self):
        # Bug real (2026-09-22): un jugador que venía cayendo pero ya recupera (trend.recovering)
        # puntuaba igual que uno en caida libre sin más — se quedaba sin el punto extra que sí
        # llevaba una racha sostenida (d7 >= 5), aunque ambas son señales de "sube ahora mismo".
        item = models.MarketItem(
            "L1", models.Player("p1", "Recupera", 3, "Equipo", "t1", 8_000_000, 40, 4.0, "ok"),
            7_600_000, None, "LaLiga", 0,
        )
        recovering = analysis.Trend(1.5, 2.0, -6.0)
        self.assertTrue(recovering.recovering)
        sustained = analysis.Trend(1.5, 2.0, 6.0)
        stars_rec, _, _, _ = analysis.market_verdict(item, recovering, None, False)
        stars_sus, _, _, _ = analysis.market_verdict(item, sustained, None, False)
        self.assertEqual(stars_rec, stars_sus)

    def test_flip_plan(self):
        from fantasy_agent import flip

        def listing(n, price, value=10_000_000):
            raw = {"id": f"F{n}", "playerMaster": pm(f"f{n}", f"Flip{n}", 3, value, 10, 5.0), "salePrice": price,
                   "expirationDate": (NOW + timedelta(hours=10)).isoformat()}
            return models.parse_market([raw])[0]

        rising = analysis.Trend(2.0, 4.0, 8.0)
        picks = [(listing(n, 10_000_000), rising) for n in (1, 2, 3)]

        # Tope: 25% de 100M = 25M comprometidos; caben dos de ~10.2M (con margen), la tercera no.
        plan = flip.plan_bids(picks, 100_000_000, 0, [], set(), 0)
        self.assertEqual([it.listing_id for it, _, _ in plan], ["F1", "F2"])
        self.assertTrue(all(10_000_000 <= amount <= 10_300_000 for _, amount, _ in plan))

        # Lo ya comprometido cuenta: con 90M en pujas pendientes no queda margen ni saldo libre.
        self.assertEqual(flip.plan_bids(picks, 100_000_000, 0, [90_000_000], set(), 1), [])
        # Máximo de flips abiertos a la vez, y no repetir un jugador que ya tienes en juego.
        self.assertEqual(flip.plan_bids(picks, 100_000_000, 0, [], set(), flip.MAX_OPEN), [])
        self.assertEqual(flip.plan_bids(picks, 100_000_000, 0, [], {"f1", "f2"}, 0)[0][0].listing_id, "F3")

        # Solo lo que sigue subiendo hoy, sin enfriarse, y sin pagar >3% sobre su valor.
        self.assertIsNone(flip.flip_amount(listing(4, 10_000_000), analysis.Trend(-1.0, 4.0, 8.0)))
        self.assertIsNone(flip.flip_amount(listing(5, 10_000_000), analysis.Trend(0.5, 0.5, 9.0)))  # cooling
        # Se frena: buena racha de 3 días pero hoy casi plano (el caso de Pablo García) -> no.
        self.assertIsNone(flip.flip_amount(listing(7, 10_000_000), analysis.Trend(0.18, 4.4, 23.0)))
        self.assertIsNone(flip.flip_amount(listing(6, 11_000_000), rising))  # pide 10% sobre su valor

    def test_team_label_known_and_unknown_club(self):
        # Sin escudos posibles en Telegram (sendMessage no admite imagenes inline), el usuario
        # pidio un emoji de colores fijo por club como identificador visual rapido.
        self.assertEqual(analysis.team_label("Real Madrid"), "⚪ Real Madrid")
        self.assertEqual(analysis.team_label("FC Barcelona"), "🔵🔴 FC Barcelona")
        # Un club no mapeado (o el "equipo #<id>" de respaldo) se queda tal cual, sin fallar.
        self.assertEqual(analysis.team_label("equipo #18"), "equipo #18")

    def test_initial_squad_and_common_value_estimate(self):
        def ev(n, type_id, u1, u2, pid, amount, day):
            return models.Activity(str(n), type_id, u1, u2, pid, amount, NOW - timedelta(days=30 - day))

        events = [
            ev(1, 33, "A", None, "p1", 10_000_000, 1),   # A vende p1: era de su plantilla inicial
            ev(2, 31, "A", None, "p9", 4_000_000, 2),    # A ficha p9
            ev(3, 33, "A", None, "p9", 6_000_000, 3),    # ...y lo vende: NO era inicial
            ev(4, 33, "B", None, "q1", 5_000_000, 1),    # B vende q1 (inicial)
        ]
        self.assertEqual(analysis.initial_squad_ids(events, "A", {"p2", "p9"}), {"p1", "p2"})
        self.assertEqual(analysis.initial_squad_ids(events, "B", set()), {"q1"})

        # A: flujo +12M, plantilla inicial 100M. B: flujo +5M, plantilla inicial 120M. Mismo valor
        # total al arrancar: B empezó con 20M MENOS de dinero que A. Mi saldo (A) hoy = 50M, luego
        # el de B = 50 + (100-120) + (5-12) = 23M. Sin plantillas iniciales, el supuesto simple
        # (mismo dinero inicial) daría 50 + (5-12) = 43M.
        teams = {"TA": "A", "TB": "B"}
        est = analysis.estimate_cash(events, "A", 50_000_000, teams, {"A": 100_000_000, "B": 120_000_000})
        self.assertEqual(est["TB"], 23_000_000)
        self.assertEqual(est["TA"], 50_000_000)
        simple = analysis.estimate_cash(events, "A", 50_000_000, teams)
        self.assertEqual(simple["TB"], 43_000_000)

    def test_to_madrid(self):
        utc = timezone.utc
        # Verano (CEST, +2): las 16:06:41 UTC son las 18:06:41 en España — la hora del "simulacro" que
        # salió con 2 h de menos porque el runner de GitHub Actions va en UTC.
        summer = analysis.to_madrid(datetime(2026, 9, 21, 16, 6, 41, tzinfo=utc))
        self.assertEqual(summer.strftime("%H:%M:%S"), "18:06:41")
        # Invierno (CET, +1) y el cambio de hora: último domingo de octubre a la 01:00 UTC (25/10/2026).
        self.assertEqual(analysis.to_madrid(datetime(2026, 12, 1, 10, 0, tzinfo=utc)).strftime("%H:%M"), "11:00")
        self.assertEqual(analysis.to_madrid(datetime(2026, 10, 25, 0, 30, tzinfo=utc)).strftime("%H:%M"), "02:30")
        self.assertEqual(analysis.to_madrid(datetime(2026, 10, 25, 1, 30, tzinfo=utc)).strftime("%H:%M"), "02:30")
        # Un datetime con otra zona (la +02:00 de la API) da la misma hora de España.
        api_time = datetime(2026, 9, 21, 21, 0, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(analysis.to_madrid(api_time).strftime("%H:%M"), "21:00")

    def test_can_bid(self):
        m40 = analysis.CASH_UNCERTAINTY
        self.assertEqual(analysis.can_bid(15_000_000, 15_000_000 + m40), "yes")
        self.assertEqual(analysis.can_bid(15_000_000, 15_000_000), "maybe")
        self.assertEqual(analysis.can_bid(15_000_000, 15_000_000 - m40), "maybe")
        self.assertEqual(analysis.can_bid(15_000_000, 15_000_000 - m40 - 1), "no")
        world = service.build_world(FakeAPI(), self.s)
        line = service._rivals_line(world, 15_000_000, {"T1": 1, "T2": 100_000_000, "T3": -30_000_000})
        self.assertIn("✅ 1 · ❔ 0 · ❌ 1", line[0])  # T1 (yo) no cuenta

    def test_trend(self):
        hist = models.parse_value_history(history(10_000_000, 1.0))
        t = analysis.trend_from_history(hist)
        self.assertAlmostEqual(t.d1, 1.0, places=1)
        self.assertGreater(t.d7, t.d3)

    def test_lineup_prefers_complete_and_skips_injured(self):
        players = [models.parse_player(pm(f"p{i}", f"P{i}", pos, 1, 1, 5)) for i, pos in
                   enumerate([1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4])]
        players.append(models.parse_player(pm("x", "Roto", 4, 1, 1, 9, status="injured")))
        cands = [lineup.Candidate(p, 0.9, lineup.expected_points(p, 0.9)) for p in players]
        formation, eleven, _ = lineup.best_eleven(cands)
        self.assertEqual(len(eleven), 11)
        self.assertNotIn("Roto", [c.player.name for c in eleven])
        self.assertIn(formation, lineup.FORMATIONS)

    def test_pkce_and_redirect(self):
        v, c = auth.make_pkce()
        self.assertTrue(43 <= len(v) <= 128 and c)
        url = auth.build_login_url(self.s)
        self.assertIn("code_challenge_method=S256", url)
        code, state = auth.parse_redirect("authredirect://com.lfp.laligafantasy/?state=abc&code=XYZ")
        self.assertEqual((code, state), ("XYZ", "abc"))

    def test_alert_dedup(self):
        store = Store(Path(self.s.data_dir) / "t.sqlite3")
        self.assertTrue(store.alert_is_new("k"))
        self.assertFalse(store.alert_is_new("k"))

    def test_start_probability_from_history(self):
        from fantasy_agent.attendance import estimate_start_probability
        from fantasy_agent.models import parse_player

        class FakeAttendanceAPI:
            def current_week(self):
                return {"weekNumber": 6}

            def players(self):
                return [
                    {"id": "1", "weekPoints": [
                        {"weekNumber": w, "points": p}
                        for w, p in [(1, 5), (2, 6), (3, 0), (4, 7), (5, 4)]
                    ]},
                    {"id": "2", "weekPoints": [
                        {"weekNumber": w, "points": 0} for w in range(1, 6)
                    ]},
                ]

        regular = parse_player({"id": "1", "nickname": "Regular"})
        bench = parse_player({"id": "2", "nickname": "Suplente"})
        new_signing = parse_player({"id": "3", "nickname": "Fichaje"})

        out = estimate_start_probability(FakeAttendanceAPI(), [regular, bench, new_signing])
        self.assertEqual(out["1"]["start_probability"], 80)  # jugó 4 de 5
        self.assertEqual(out["2"]["start_probability"], 10)  # suelo (0 de 5, con tope mínimo)
        self.assertEqual(out["3"]["start_probability"], 70)  # sin histórico -> valor por defecto

    def test_futbolfantasy_scraping_parsers(self):
        from fantasy_agent import futbolfantasy as ff
        from fantasy_agent.models import parse_player

        index_html = (
            '<a href="https://www.futbolfantasy.com/partidos/999-sevilla-barcelona" '
            'class="partido hideQtip" data-tooltip="Sevilla - Barcelona" ></a>'
        )
        # Estructura mínima real: contenedores campo-wrapper, bloque de jugador con sus
        # data-attributes, y el modal aparte (por id) con el nombre completo.
        match_html = (
            '<div class="campo-wrapper zoom-fix local liga">'
            '<div class="jugador_1 campo camiseta-wrapper" data-index="1">'
            '<a class="camiseta" data-probabilidad="80%" data-lesion="-1" data-onceFF="titular" '
            'href="#" data-toggle="modal" data-target="#opcion-jugador-501">x</a></div>'
            '<div class="jugador_2 campo camiseta-wrapper" data-index="2">'
            '<a class="camiseta" data-probabilidad="20%" data-lesion="3" data-onceFF="duda" '
            'href="#" data-toggle="modal" data-target="#opcion-jugador-502">x</a></div>'
            '</div>'
            '<div class="campo-wrapper multi-views suplentes"></div>'
            '<div id="opcion-jugador-501"><h5 class="modal-title">Fermín López</h5></div>'
            '<div id="opcion-jugador-502"><h5 class="modal-title">Isaac Romero</h5></div>'
        )

        self.assertEqual(ff._team_alias("Sevilla FC"), "sevilla")
        self.assertEqual(ff._team_alias("FC Barcelona"), "barcelona")
        self.assertEqual(ff._team_alias("Deportivo Alavés"), "alaves")  # no confundir con "RC Deportivo"
        self.assertEqual(ff._team_alias("RC Deportivo"), "deportivo")

        urls = ff._find_match_urls(index_html, {"sevilla", "barcelona"})
        self.assertEqual(urls["sevilla"], urls["barcelona"])
        self.assertIn("sevilla-barcelona", urls["sevilla"])

        names = ff._modal_names(match_html)
        self.assertEqual(names["501"], "Fermín López")

        rows = ff._player_rows(match_html)
        self.assertEqual(len(rows), 2)
        by_modal = {r["modal_id"]: r for r in rows}
        self.assertEqual(by_modal["501"]["prob"], 80)
        self.assertEqual(by_modal["502"]["lesion"], "3")

        p_fermin = parse_player({"id": "10", "nickname": "Fermín", "team": {"name": "Sevilla FC"}})
        match = ff._match_player("Fermín López", [p_fermin])
        self.assertEqual(match.id, "10")


class ClauseRaiseTests(unittest.TestCase):
    def test_not_in_danger_no_plan(self):
        # cláusula ya muy por encima del valor de mercado: no es un robo barato, no hace falta anzuelo
        self.assertIsNone(analysis.clause_raise_plan(20_000_000, 10_000_000, 50_000_000))

    def test_no_cash_no_plan(self):
        self.assertIsNone(analysis.clause_raise_plan(10_000_000, 10_000_000, 0))

    def test_bait_targets_logic_ratio_not_unreachable(self):
        # cláusula pegada al mercado (10M de 10M): el objetivo es 1.2x mercado = 12M, no más
        plan = analysis.clause_raise_plan(10_000_000, 10_000_000, available_cash=50_000_000)
        target_clause = round(10_000_000 * analysis.CLAUSE_BAIT_RATIO)
        needed_cost = -(-(target_clause - 10_000_000) // 2)
        expected_cost = (needed_cost // 10_000) * 10_000
        self.assertEqual(plan.cost, expected_cost)
        self.assertEqual(plan.raise_amount, expected_cost * 2)
        self.assertLessEqual(plan.new_clause, target_clause)
        self.assertTrue(plan.reaches_bait_ratio)
        self.assertEqual(plan.guaranteed_profit, plan.cost)
        # ni de lejos se funde la caja: el anzuelo cuesta un 10% del valor de mercado, no medio presupuesto
        self.assertLess(plan.cost, 10_000_000 * 0.15)

    def test_partial_bait_when_cash_is_short(self):
        plan = analysis.clause_raise_plan(10_000_000, 10_000_000, available_cash=500_000)
        self.assertEqual(plan.cost, 500_000)
        self.assertEqual(plan.raise_amount, 1_000_000)
        self.assertFalse(plan.reaches_bait_ratio)

    def _slot(self, player_id, market_value, clause, unlock_in_hours):
        player = models.Player(
            id=player_id, name="Vulnerable", position_id=4, team="Barcelona", team_id="4",
            market_value=market_value, points=50, avg_points=6.0, status="ok",
        )
        return models.SquadSlot(
            player=player, owner_team_id="T1", owner_name="Yo", clause=clause,
            clause_locked_until=NOW + timedelta(hours=unlock_in_hours),
        )

    def test_unlock_candidates_only_within_24h(self):
        soon = self._slot("p1", 10_000_000, 10_500_000, 5)
        far = self._slot("p2", 10_000_000, 10_500_000, 48)
        world = service.World("L1", "T1", 20_000_000, [], [soon, far], [], [])
        candidates = service.own_clause_unlock_candidates(world)
        self.assertEqual([sl.player.id for sl in candidates], ["p1"])

    def test_unlock_report_recommends_bait_for_cheap_clause(self):
        slot = self._slot("p1", 10_000_000, 10_200_000, 5)
        world = service.World("L1", "T1", 20_000_000, [], [slot], [], [])
        text = service.own_clause_unlock_report(world)
        self.assertIn("Vulnerable", text)
        self.assertIn("anzuelo", text)

    def test_unlock_report_empty_when_nothing_unlocks_soon(self):
        world = service.World("L1", "T1", 20_000_000, [], [], [], [])
        self.assertEqual(service.own_clause_unlock_report(world), "")

    def test_unlock_alerts_fire_once_per_player(self):
        slot = self._slot("p1", 10_000_000, 10_200_000, 5)
        world = service.World("L1", "T1", 20_000_000, [], [slot], [], [])
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        first = service.own_clause_unlock_alerts(world, store)
        second = service.own_clause_unlock_alerts(world, store)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])


if __name__ == "__main__":
    unittest.main()
