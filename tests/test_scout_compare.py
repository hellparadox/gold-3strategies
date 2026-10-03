"""بخش «رقابت» داشبورد: شما + اسکات در برابر ORB و ایچیموکو (با R)، و انتخاب‌های شما در برابر همهٔ سیگنال‌ها."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_scout_tracker import make_scout
from tools import scout_dashboard as dash

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def make_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, ticket INTEGER, symbol TEXT, side TEXT, "
                "strategy TEXT, lot REAL, entry REAL, sl REAL, tp REAL, atr REAL, reason TEXT, "
                "created_at TEXT, closed_at TEXT, close_price REAL, profit REAL, outcome TEXT)")
    for lot, entry, sl, profit, closed in rows:
        con.execute("INSERT INTO signals (ticket, symbol, side, strategy, lot, entry, sl, tp, created_at, "
                    "closed_at, profit) VALUES (1,'XAUUSD','BUY','orb',?,?,?,0,'x',?,?)",
                    (lot, entry, sl, closed, profit))
    con.commit()
    con.close()


def ts(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


class Compare(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tmp = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_bot_trades_in_r_and_read_only(self):
        db = self.tmp / "orb.db"
        make_db(db, [(0.05, 4100.0, 4096.0, 40.0, ts(1)),     # ریسک 4×100×0.05 = 20$ → +2R
                     (0.05, 4100.0, 4096.0, -20.0, ts(20)),   # −1R
                     (0.05, 4100.0, 4096.0, None, None)])     # هنوز باز: حساب نمی‌شود
        tr = dash.bot_trades(db)
        self.assertEqual(sorted(t["r"] for t in tr), [-1.0, 2.0])
        self.assertEqual(dash.bot_trades(self.tmp / "missing.db"), [])

    def test_scout_trades_and_windows(self):
        def iso(days):
            return (NOW - timedelta(days=days)).astimezone(dash.TEHRAN).isoformat()
        ev = [{"ts": iso(10), "event": "opened", "ticket": "5", "price": "4150", "sl": "4140"},
              {"ts": iso(9), "event": "gone", "ticket": "5", "profit": "20"},     # ریسک 10$ → +2R
              {"ts": iso(2), "event": "opened", "ticket": "6", "price": "4150", "sl": "4144"},
              {"ts": iso(1), "event": "gone", "ticket": "6", "profit": "-6"}]     # −1R
        sc = dash.scout_trades(ev)
        self.assertEqual([t["r"] for t in sc], [2.0, -1.0])
        bots = {"ORB": [{"t": NOW - timedelta(days=3), "profit": -5.0, "r": -0.5}]}
        c = dash.build_compare(sc, bots, now=NOW)
        me7 = c["7d"][0]
        self.assertTrue(me7["me"])
        self.assertEqual((me7["n"], me7["sum_r"]), (1, -1.0))
        self.assertEqual((c["30d"][0]["n"], c["30d"][0]["sum_r"]), (2, 1.0))
        self.assertEqual(c["start"][0]["n"], 2)
        self.assertEqual(c["7d"][1], {"name": "ORB", "me": False, "n": 1, "win": 0, "pnl": -5.0,
                                       "avg_r": -0.5, "sum_r": -0.5})
        self.assertEqual(c["since"], (NOW - timedelta(days=9)).astimezone(dash.TEHRAN).strftime("%Y-%m-%d"))

    def test_picks_vs_all(self):
        v = ([{"status": "opened", "R": 2.0}] * 4 + [{"status": "rejected", "R": -1.0}] * 6
             + [{"status": "suppressed", "R": 0.5}] * 2 + [{"status": "expired", "R": float("nan")}])
        p = dash.build_picks(v)
        self.assertEqual(p["taken"], {"n": 4, "win": 100, "avg_r": 2.0})
        self.assertEqual(p["passed"], {"n": 6, "win": 0, "avg_r": -1.0})
        self.assertEqual(p["silent"]["n"], 2)
        self.assertEqual(p["all"]["n"], 12)
        self.assertIsNone(dash.build_picks(None))

    def test_bot_wiring_reads_other_dbs(self):
        sc = make_scout(self.tmp)
        db = self.tmp / "ichi.db"
        make_db(db, [(0.02, 4100.0, 4095.0, 10.0, ts(1))])   # ریسک 10$ → +1R
        sc.compare_bots = [{"name": "ایچیموکو", "db": str(db)}, {"name": "ORB", "db": str(self.tmp / "none.db")}]
        sc.client.reset_mock()
        st = sc.dash_stats()
        names = [r["name"] for r in st["compare"]["start"]]
        self.assertEqual(names, ["شما + اسکات", "ایچیموکو", "ORB"])
        self.assertEqual(st["compare"]["30d"][1]["sum_r"], 1.0)
        self.assertEqual(sc.client.method_calls, [])                 # نخ وب هیچ‌وقت MT5 را صدا نمی‌زند


if __name__ == "__main__":
    unittest.main()
