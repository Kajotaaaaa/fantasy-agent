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
        self.assertEqual(plan.ceiling, 15_000_000)  # 7.5 pts / 0.5 pts-por-M
        # Sin subida no hay margen que justificar; sin referencia de mercado, ni techo.
        flat = analysis.bid_plan(10_000_000, 10_000_000, analysis.Trend(0, 0, 0), 7.5, 0, False)
        self.assertEqual((flat.margin, flat.expected, flat.ceiling), (None, None, None))
        # Un techo que no supera la puja anterior no aporta nada.
        low = analysis.bid_plan(10_000_000, 10_000_000, rising, 7.5, 0.74, False)
        self.assertIsNone(low.ceiling)
        # Y nunca pasa del doble del mínimo, aunque los puntos "digan" mucho más.
        cheap = analysis.bid_plan(1_000_000, 1_000_000, analysis.Trend(0, 0, 0), 3.5, 0.29, False)
        self.assertEqual(cheap.ceiling, 2_000_000)

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


if __name__ == "__main__":
    unittest.main()
