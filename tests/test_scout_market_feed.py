"""/api/price و /api/candles: قیمت و کندل‌های خود بروکر برای ربات‌های ادمین (فقط خواندنی، با توکن داشبورد).
نخ وب MT5 را صدا نمی‌زند؛ حلقهٔ اصلی فقط وقتی کسی در ۵ دقیقهٔ اخیر خواسته، با فاصله‌های ثابت می‌خواند."""
import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import pandas as pd

from tests.test_scout_tracker import make_scout
from tools import scout_dashboard as dash

MARKET = {"symbol": "XAUUSD@", "market_open": True, "updated_utc": "2026-11-16T10:00:00Z", "server_offset_min": 120,
          "price": {"bid": 3890.1, "ask": 3890.31, "spread": 0.21, "time": "2026-11-16 12:00:00"},
          "candles": {tf: [[f"2026-11-16 1{i % 10}:00", 1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i, 10 + i] for i in range(600)]
                      for tf in ("M1", "M5", "H1")}}


class Endpoints(unittest.TestCase):
    def setUp(self):
        self.m = {"v": None}
        self.d = dash.ScoutDashboard(lambda: {"status": {"alive": True}}, lambda: {}, "s3cret", "127.0.0.1", 0,
                                     market=lambda: self.m["v"])
        self.assertTrue(self.d.start())
        self.addCleanup(self.d.stop)

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.d.port}{path}", timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            return e.code, (json.loads(body) if body.startswith("{") else body)

    def test_token_required(self):
        for p in ("/api/price", "/api/price?token=x", "/api/candles?tf=H1"):
            self.assertEqual(self.get(p)[0], 403)

    def test_warming_up_then_price(self):
        before = time.time()
        code, body = self.get("/api/price?token=s3cret")
        self.assertEqual((code, body["error"]), (503, "warming_up"))
        self.assertGreaterEqual(self.d.market_wanted_t, before)          # حلقهٔ اصلی می‌فهمد کسی خواسته
        self.m["v"] = MARKET
        code, body = self.get("/api/price?token=s3cret")
        self.assertEqual(code, 200)
        self.assertEqual(body["price"]["bid"], 3890.1)
        self.assertEqual(set(body), {"symbol", "price", "market_open", "updated_utc", "server_offset_min"})
        self.assertNotIn("candles", body)                                # قیمت سبک می‌ماند

    def test_candles_slicing_and_validation(self):
        self.m["v"] = MARKET
        code, body = self.get("/api/candles?token=s3cret&tf=H1&n=2")
        self.assertEqual(code, 200)
        self.assertEqual(list(body["candles"]), ["H1"])
        self.assertEqual(body["candles"]["H1"], MARKET["candles"]["H1"][-2:])   # آخرین‌ها
        self.assertTrue(body["forming_last"])
        self.assertEqual(body["columns"], ["time", "open", "high", "low", "close", "volume"])
        code, body = self.get("/api/candles?token=s3cret")
        self.assertEqual(sorted(body["candles"]), ["H1", "M1", "M5"])
        self.assertEqual(len(body["candles"]["M5"]), 100)                # پیش‌فرض
        code, body = self.get("/api/candles?token=s3cret&tf=M5&n=9999")
        self.assertEqual(len(body["candles"]["M5"]), 500)                # سقف
        self.assertEqual(self.get("/api/candles?token=s3cret&tf=D1")[0], 400)
        self.assertEqual(self.get("/api/candles?token=s3cret&n=abc")[0], 400)

    def test_candles_warming_until_all_requested_frames_exist(self):
        self.m["v"] = {**MARKET, "candles": {"M1": MARKET["candles"]["M1"]}}
        self.assertEqual(self.get("/api/candles?token=s3cret&tf=M1")[0], 200)
        self.assertEqual(self.get("/api/candles?token=s3cret&tf=M1,H1")[0], 503)

    def test_data_endpoint_unchanged(self):
        code, body = self.get("/api/data?token=s3cret")
        self.assertEqual(code, 200)
        self.assertTrue(body["status"]["alive"])


def rates(n=3, start="2026-11-16 10:00", freq="5min"):
    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame({"open": [1.0] * n, "high": [2.0] * n, "low": [0.5] * n, "close": [1.5] * n,
                         "volume": [7.0] * n}, index=idx)


class MainLoopCache(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = make_scout(Path(self._d.name))
        self.now = 1_800_000_000.0
        self.sc.dashboard = NS(market_wanted_t=self.now - 10)
        self.sc.client.symbol = "XAUUSD@"
        self.sc.client.get_tick.return_value = NS(bid=3890.1, ask=3890.31, time=int(self.now + 7200))
        self.sc.client.get_rates.side_effect = lambda tf, n: rates()

    def tearDown(self):
        self._d.cleanup()

    def test_builds_price_and_candles(self):
        self.sc.market_tick(self.now)
        m = self.sc._market
        self.assertEqual(m["symbol"], "XAUUSD@")
        self.assertEqual((m["price"]["bid"], m["price"]["spread"]), (3890.1, 0.21))
        self.assertEqual(m["server_offset_min"], 120)                     # ساعت سرور بروکر = UTC+2
        self.assertEqual(sorted(m["candles"]), ["H1", "M1", "M5"])
        self.assertEqual(m["candles"]["M5"][0], ["2026-11-16 10:00", 1.0, 2.0, 0.5, 1.5, 7])
        asked = [c.args[0] for c in self.sc.client.get_rates.call_args_list]
        self.assertEqual(sorted(asked), ["H1", "M1", "M5"])
        self.assertTrue(all(c.args[1] == 500 for c in self.sc.client.get_rates.call_args_list))

    def test_cadence(self):
        self.sc.market_tick(self.now)
        self.sc.client.get_rates.reset_mock()
        self.sc.client.get_tick.reset_mock()
        self.sc.market_tick(self.now + 1)                                 # هیچ‌چیز هنوز وقتش نیست
        self.sc.client.get_rates.assert_not_called()
        self.sc.client.get_tick.assert_not_called()
        self.sc.market_tick(self.now + 11)                                # تیک و M1
        self.assertEqual([c.args[0] for c in self.sc.client.get_rates.call_args_list], ["M1"])
        self.sc.client.get_tick.assert_called_once()

    def test_no_mt5_calls_when_nobody_asked(self):
        self.sc.dashboard = NS(market_wanted_t=self.now - 301)
        self.sc.market_tick(self.now)
        self.sc.client.get_tick.assert_not_called()
        self.sc.client.get_rates.assert_not_called()
        self.assertIsNone(getattr(self.sc, "_market", None))

    def test_no_dashboard_is_a_no_op(self):
        self.sc.dashboard = None
        self.sc.market_tick(self.now)
        self.sc.client.get_tick.assert_not_called()

    def test_mt5_error_never_raises(self):
        self.sc.client.get_rates.side_effect = RuntimeError("boom")
        with mock.patch("tools.scout_bot.logger") as lg:
            self.sc.market_tick(self.now)
            self.sc.market_tick(self.now + 61)
        self.assertEqual(lg.warning.call_count, 1)                        # حداکثر هر ۱۰ دقیقه یک هشدار


if __name__ == "__main__":
    unittest.main()
