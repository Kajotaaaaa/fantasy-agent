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
            {"playerMaster": pm("m1", "Chollo", 3, 10_000_000, 60, 7.5), "salePrice": 9_000_000,
             "expirationDate": (NOW + timedelta(hours=10)).isoformat(), "numberOfBids": 1},
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
        self.assertIn("ONCE RECOMENDADO", report)

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
