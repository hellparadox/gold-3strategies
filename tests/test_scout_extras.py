"""نمودار هشدار، «اگر گرفته بودید…»، جدول امتیاز شبانه، هشدار قیمت — هیچ‌کدام سیگنالی را حذف نمی‌کند."""
import tempfile
import time
import unittest
import unittest.mock
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd

from tests.test_scout_tracker import make_scout
from tools import scout_followup as fu
from tools.scout_setups import indicators

BAR = pd.Timestamp("2026-10-05 10:00")          # کندل هشدار؛ پیگیری از 10:15 (بسته شدن کندل)


def m1(path, start="2026-10-05 10:00"):
    """کندل‌های ۱ دقیقه‌ای از مسیر قیمت‌ها (هر عدد = close؛ high/low کمی دورتر)."""
    idx = pd.date_range(start, periods=len(path), freq="1min")
    c = np.array(path, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 0.1, "low": c - 0.1, "close": c}, index=idx)


def item(side="BUY", decision="rejected", ctx=None):
    if side == "BUY":
        return fu.new_whatif("3", "cloud_break", "BUY", 4150.0, 4144.0, 4162.0, BAR, decision, 6.0, ctx or {})
    return fu.new_whatif("4", "exhaustion", "SELL", 4150.0, 4156.0, 4138.0, BAR, decision, 6.0, ctx or {})


def frame(n=300, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-10-02 06:00", periods=n, freq="15min")
    c = 4120 + np.cumsum(rng.normal(0.1, 2.0, n))
    o = np.r_[c[0], c[:-1]]
    return indicators(pd.DataFrame({"open": o, "high": np.maximum(o, c) + 1, "low": np.minimum(o, c) - 1,
                                    "close": c}, index=idx), None)


class Jalali(unittest.TestCase):
    def test_known_dates(self):
        self.assertEqual(fu.to_jalali(date(2026, 10, 3)), (1405, 7, 11))
        self.assertEqual(fu.to_jalali(date(2026, 3, 21)), (1405, 1, 1))
        self.assertEqual(fu.to_jalali(date(2025, 3, 20)), (1403, 12, 30))
        self.assertEqual(fu.jalali_label(date(2026, 10, 3)), "شنبه 11 مهر 1405")


class WhatIf(unittest.TestCase):
    def test_ignores_bars_before_alert_candle_closes(self):
        bars = m1([4140.0] * 15 + [4151.0] * 5)                 # افت تا 4140 قبل از 10:15 حساب نیست
        self.assertIsNone(fu.evaluate(item(), bars))

    def test_stop_after_favourable_run_and_reason(self):
        path = [4150.0] * 15 + [4152, 4155, 4155.5, 4150, 4146, 4143.5]
        res = fu.evaluate(item(ctx={"h1_with": False, "cloud": "inside"}), m1(path))
        self.assertEqual(res["exit"], "SL")
        self.assertEqual(res["R"], -1.0)
        self.assertGreaterEqual(res["mfe"], 0.9)
        msg = fu.whatif_message(item(ctx={"h1_with": False, "cloud": "inside"}), res, (30, -0.31))
        self.assertIn("تصمیم درستی بود", msg)
        self.assertIn("خلاف روند یک‌ساعته", msg)
        self.assertIn("داخل ابر", msg)
        self.assertIn("اول تا ‎+0.9R", msg)
        self.assertIn("کارنامهٔ این ستاپ در 30 نمونهٔ آخر", msg)
        self.assertIn("‎-6.00$", msg)

    def test_target_and_sell_side(self):
        res = fu.evaluate(item("SELL"), m1([4150.0] * 15 + [4149, 4145, 4141, 4137.5]))
        self.assertEqual((res["exit"], res["R"]), ("TP", 2.0))
        msg = fu.whatif_message(item("SELL", "expired"), res)
        self.assertIn("جواب می‌داد", msg)
        self.assertIn("منقضی شد", msg)
        self.assertIn("‎+12.00$", msg)

    def test_same_bar_both_is_stop_and_horizon_open(self):
        both = m1([4150.0] * 15)
        both.loc[pd.Timestamp("2026-10-05 10:16")] = [4150, 4163, 4143, 4150]
        self.assertEqual(fu.evaluate(item(), both.sort_index())["exit"], "SL")
        flat = m1([4151.0] * (15 + 24 * 60 + 5))
        res = fu.evaluate(item(), flat)
        self.assertEqual(res["exit"], "OPEN")
        self.assertIn("در ۲۴ ساعت نه به هدف رسید", fu.whatif_message(item(), res))

    def test_context_flags(self):
        f = frame()
        flags = fu.context_flags(f, "BUY")
        self.assertIn(flags.get("cloud"), ("with", "inside", "against"))
        self.assertIn("ema_with", flags)


class PriceAlertsAndScoreboard(unittest.TestCase):
    def test_parse_and_trigger(self):
        self.assertEqual(fu.parse_alert("/alert ۴۱۵۰ مقاومت روزانه"), (4150.0, "مقاومت روزانه"))
        self.assertEqual(fu.parse_alert("/alert 4150.5"), (4150.5, ""))
        self.assertIsNone(fu.parse_alert("/alert"))
        self.assertIsNone(fu.parse_alert("/alert abc"))
        al = []
        up = fu.add_price_alert(al, 4150, 4140)
        dn = fu.add_price_alert(al, 4120, 4140)
        self.assertEqual((up["dir"], dn["dir"], dn["id"]), ("up", "down", 2))
        self.assertEqual(fu.due_price_alerts(al, 4145), [])
        self.assertEqual([a["id"] for a in fu.due_price_alerts(al, 4150.2)], [1])
        self.assertEqual([a["id"] for a in fu.due_price_alerts(al, 4119)], [2])

    def test_scoreboard_text(self):
        today = [{"name": "شما + اسکات", "me": True, "n": 3, "win": 67, "pnl": 14.2, "avg_r": 0.7, "sum_r": 2.1},
                 {"name": "ORB", "me": False, "n": 2, "win": 0, "pnl": -12.0, "avg_r": -0.5, "sum_r": -1.0},
                 {"name": "ایچیموکو", "me": False, "n": 0, "win": None, "pnl": 0, "avg_r": None, "sum_r": None}]
        txt = fu.scoreboard_text(date(2026, 10, 5), today, today, 12, 3, {"good_pass": 3, "missed": 1})
        self.assertIn("دوشنبه 13 مهر 1405", txt)
        self.assertIn("🏆 <b>شما + اسکات</b>: +2.1R", txt)
        self.assertIn("ایچیموکو: بدون معامله", txt)
        self.assertIn("3 بار رد درست بود · 1 بار سود می‌داد", txt)


class BotWiring(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tmp = Path(self._d.name)
        self.sc = make_scout(self.tmp)
        self.sc.admins = {1}
        self.sc.flags_path = self.tmp / "flags.json"
        self.sc.client.get_tick.return_value = NS(time=0, bid=4140.0, ask=4140.3)
        self.sc.client.positions.return_value = []

    def tearDown(self):
        if self.sc._chart_pool:
            self.sc._chart_pool.shutdown(wait=True)
        self._d.cleanup()

    def row(self):
        return pd.Series({"setup": "cloud_break", "side": "BUY", "ref_close": 4150.0, "sl_dist": 6.0,
                          "tp_dist": 12.0, "atr": 5.0, "bar": BAR})

    def test_chart_sent_as_reply_in_background(self):
        self.sc.chart_enabled = True
        self.sc._frame = frame()
        self.sc.tg.send_photo = unittest.mock.Mock(return_value=999)
        self.sc.alert(self.row(), 27.0)
        mid = self.sc.pending[str(self.sc.n)]["mid"]
        self.sc._chart_pool.shutdown(wait=True)
        self.sc._chart_pool = None
        png, = self.sc.tg.send_photo.call_args.args[:1]
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(self.sc.tg.send_photo.call_args.kwargs["reply_to"], mid)
        self.assertIn("محدودهٔ خنثی", self.sc.tg.send_photo.call_args.args[1])

    def test_reject_is_followed_and_reported_with_reason(self):
        self.sc.whatif_enabled = True
        self.sc.alert(self.row(), 27.0)
        aid = str(self.sc.n)
        mid = self.sc.pending[aid]["mid"]
        self.sc.decide(aid, False, "cb")
        self.assertEqual(len(self.sc._whatif), 1)
        self.assertTrue(self.sc.whatif_path.exists())               # بعد از ری‌استارت ادامه دارد
        self.sc.client.get_rates.return_value = m1([4150.0] * 15 + [4151, 4146, 4143.5])
        self.sc.whatif_tick(now=1000.0)
        self.assertEqual(self.sc._whatif, [])
        self.assertIn("تصمیم درستی بود", self.sc.tg.sent[-1][0])
        self.assertEqual(self.sc.tg.replies[-1], mid)
        self.sc.whatif_tick(now=1010.0)                               # چیزی برای پیگیری نیست

    def test_expired_is_followed(self):
        self.sc.whatif_enabled = True
        self.sc.alert(self.row(), 27.0)
        self.sc.pending[str(self.sc.n)]["t"] -= 10_000
        self.sc.expire()
        self.assertEqual(self.sc._whatif[0]["decision"], "expired")

    def test_price_alert_flow(self):
        self.sc.handle_update({"message": {"from": {"id": 1}, "text": "/alert 4150 مقاومت"}})
        self.assertEqual(self.sc._palerts[0]["dir"], "up")
        self.assertIn("ثبت شد", self.sc.tg.sent[-1][0])
        self.sc.price_alert_tick()                                    # 4140: هنوز نه
        self.assertEqual(len(self.sc._palerts), 1)
        self.sc.client.get_tick.return_value = NS(time=0, bid=4150.3, ask=4150.6)
        self.sc.price_alert_tick()
        self.assertEqual(self.sc._palerts, [])
        self.assertIn("به <b>4150.00</b> رسید", self.sc.tg.sent[-1][0])
        self.assertIn("مقاومت", self.sc.tg.sent[-1][0])
        self.sc.handle_update({"message": {"from": {"id": 1}, "text": "/alert 4100"}})
        aid = self.sc._palerts[0]["id"]
        self.sc.handle_update({"callback_query": {"from": {"id": 1}, "data": f"p|{aid}", "id": "q"}})
        self.assertEqual(self.sc._palerts, [])

    def test_scoreboard_once_per_day_and_quiet_without_activity(self):
        self.sc.score_enabled, self.sc.score_time = True, "00:00"
        now = time.time()
        self.sc.scoreboard_tick(now=now)                              # روز بی‌فعالیت: پیامی نیست
        self.assertFalse(any("جدول امتیاز" in t for t, _ in self.sc.tg.sent))
        self.sc.flags_path.unlink()
        self.sc.log(event="alert", alert="1", setup="cloud_break", side="BUY")
        self.sc.scoreboard_tick(now=now)
        self.sc.scoreboard_tick(now=now + 60)
        self.assertEqual(sum("جدول امتیاز امروز" in t for t, _ in self.sc.tg.sent), 1)

    def test_menu_has_new_buttons(self):
        labels = [b["text"] for row in self.sc.MENU_BUTTONS for b in row]
        self.assertIn("🔔 هشدار قیمت", labels)
        self.assertIn("🏁 جدول امتیاز", labels)


if __name__ == "__main__":
    unittest.main()
