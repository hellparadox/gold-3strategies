"""داشبورد وب اسکات: آمار از ژورنال، توکن، و اینکه نخ وب MT5 را صدا نمی‌زند."""
import json
import tempfile
import unittest
import unittest.mock
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

from tests.test_scout_tracker import ENTRY, FRAME, SOFT, make_scout, pos
from tools import scout_dashboard as dash

NOW = datetime(2026, 10, 2, 20, 0, tzinfo=dash.TEHRAN)


def ev(event, ts, **kw):
    d = {"ts": ts, "event": event}
    d.update({k: str(v) for k, v in kw.items()})
    return d


def tehran_iso(h, m=0, day=2):
    """ts ژورنال ساعت محلی VPS است؛ برای تست مستقل از منطقهٔ زمانی ماشین، با منطقه می‌نویسیم."""
    return datetime(2026, 10, day, h, m, tzinfo=dash.TEHRAN).isoformat()


class Stats(unittest.TestCase):
    def test_trades_days_setups_and_recent(self):
        events = [
            ev("alert", tehran_iso(10), alert=1, setup="cloud_break", side="SELL", price=4150),
            ev("opened", tehran_iso(10, 5), alert=1, setup="cloud_break", side="SELL", ticket=11),
            ev("gone", tehran_iso(11), ticket=11, setup="cloud_break", side="SELL", profit=12.5),
            ev("alert", tehran_iso(12), alert=2, setup="tk_cross,rejection", side="BUY", price=4140),
            ev("rejected", tehran_iso(12, 3), alert=2, setup="tk_cross", side="BUY"),
            ev("gone", tehran_iso(9, day=1), ticket=7, setup="engulfing", side="BUY"),   # بدون سود → از MT5
            ev("suppressed", tehran_iso(13), setup="rejection", side="BUY"),
        ]
        virt = [{"setup": "cloud_break", "side": "SELL", "R": 2.0}] * 5 + \
               [{"setup": "tk_cross", "side": "BUY", "R": -1.0}] * 6
        st = dash.build_stats(events, now=NOW, virtual=virt, actual_by_ticket={"7": -4.0})
        self.assertEqual(st["today"], {"n": 1, "pnl": 12.5, "win": 100})
        self.assertEqual(st["all"]["n"], 2)
        self.assertEqual(st["all"]["pnl"], 8.5)
        self.assertEqual(st["alerts_today"], 2)
        self.assertEqual(len(st["days"]), 14)
        self.assertEqual(st["days"][-1], {"d": "2026-10-02", "pnl": 12.5, "n": 1})
        self.assertEqual(st["days"][-2]["pnl"], -4.0)
        by = {r["setup"]: r for r in st["setups"]}
        self.assertEqual(by["cloud_break"]["avg_R"], 2.0)
        self.assertEqual(by["cloud_break"]["taken"], 1)
        self.assertEqual(by["tk_cross"]["win_R"], 0)
        self.assertEqual(by["rejection"]["alerts"], 1)              # suppressed هم شمرده می‌شود
        self.assertIsNone(by["rejection"]["avg_R"])                  # کمتر از ۵ نمونه
        self.assertEqual(st["setups"][0]["setup"], "cloud_break")    # بهترین بالا
        self.assertEqual(st["sides"]["SELL"], {"n": 5, "sum_R": 10.0})
        self.assertTrue(st["recent"][0]["text"].startswith("ℹ️"))   # جدیدترین اول؛ suppressed پنهان
        self.assertTrue(all("بی‌صدا" not in r["text"] for r in st["recent"]))

    def test_empty_journal(self):
        st = dash.build_stats([], now=NOW)
        self.assertEqual(st["all"], {"n": 0, "pnl": 0, "win": None})
        self.assertFalse(st["has_virtual"])


class Server(unittest.TestCase):
    def get(self, d, path):
        url = f"http://127.0.0.1:{d.port}{path}"
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode("utf-8")

    def test_token_required_and_page_and_api(self):
        calls = []
        d = dash.ScoutDashboard(lambda: {"status": {"alive": True}},
                                lambda: calls.append(1) or {"all": {"n": 0}}, "s3cret", "127.0.0.1", 0)
        self.assertTrue(d.start())
        try:
            for bad in ("/", "/?token=wrong", "/api/data"):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    self.get(d, bad)
                self.assertEqual(cm.exception.code, 403)
            code, body = self.get(d, "/?token=s3cret")
            self.assertEqual(code, 200)
            self.assertIn("داشبورد اسکات", body)
            self.assertIn("راهنمای اسکات", body)                    # تب راهنما داخل صفحه
            self.assertIn("یک معامله از اول تا آخر", body)
            self.assertNotIn("<!--GUIDE-->", body)
            for name in ("kijun_pullback", "exhaustion", "حد ضرر اضطراری", "سؤال‌های رایج"):
                self.assertIn(name, body)
            code, body = self.get(d, "/api/data?token=s3cret")
            data = json.loads(body)
            self.assertTrue(data["status"]["alive"])
            self.assertEqual(data["stats"], {"all": {"n": 0}})
            self.get(d, "/api/data?token=s3cret")
            self.assertEqual(len(calls), 1)                         # آمار ۱۵ ثانیه کش می‌شود
        finally:
            d.stop()

    def test_never_public_without_token(self):
        d = dash.ScoutDashboard(dict, dict, "", "127.0.0.1", 0)
        self.assertFalse(d.start())


class BotWiring(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = make_scout(Path(self._d.name), mode="close")
        self.sc.open[5] = {"setup": "kijun_pullback", "side": "BUY", "entry": ENTRY, "sl": SOFT,
                           "risk": 6.04, "r1": False, "warned": False, "emerg": 4157.57,
                           "status_mid": None, "below": False, "hold": False, "alert": "1",
                           "approach": False, "ai_next": 0.0, "ai_note": ""}
        self.sc.client.account_info.return_value = NS(balance=391.06, equity=395.0, profit=3.94)
        self.sc.client.is_connected = True
        self.sc.paused, self.sc.started, self.sc.version = False, 0.0, "abc1234"
        self.sc.lot, self.sc.sl_cap, self.sc.emerg_cap = 0.01, 10.0, 20.0
        self.sc.dashboard = object()                                # روشن

    def tearDown(self):
        self._d.cleanup()

    def test_snapshot_built_in_main_loop_with_live_position(self):
        self.sc.client.positions.return_value = [pos(price=4178.0, profit=2.3)]
        with unittest.mock.patch("tools.scout_bot.time.time", return_value=1000.0):
            self.sc.monitor(FRAME)
        self.sc.refresh_virtual = unittest.mock.Mock()
        self.sc.stats_every = 3600.0
        self.sc._dash_t = self.sc._virt_t = 0.0
        self.sc.dash_tick(now=5000.0)
        self.sc.stats_tick(now=5000.0)                             # جدا از داشبورد (کارنامهٔ هشدار هم لازمش دارد)
        snap = self.sc._dash_snap
        self.assertEqual(snap["account"]["balance"], 391.06)
        self.assertEqual(snap["open"][0]["ticket"], 5)
        self.assertEqual(snap["open"][0]["price"], 4178.0)
        self.assertEqual(snap["open"][0]["broker_sl"], SOFT)
        self.assertEqual(snap["rules"]["max_open"], 2)
        self.sc.refresh_virtual.assert_called_once()
        n = self.sc.client.account_info.call_count
        self.sc.dash_tick(now=5002.0)                              # کمتر از ۵ ثانیه: دوباره MT5 نه
        self.sc.stats_tick(now=5002.0)
        self.assertEqual(self.sc.client.account_info.call_count, n)
        self.sc.refresh_virtual.assert_called_once()

    def test_stats_from_web_thread_touch_no_mt5(self):
        self.sc.client.reset_mock()
        st = self.sc.dash_stats()
        self.assertIn("all", st)
        self.assertEqual(self.sc.client.method_calls, [])


if __name__ == "__main__":
    unittest.main()
