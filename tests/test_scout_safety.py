"""چهار بهبود تأییدشدهٔ مالک (۲۰۲۶-۱۰-۰۳) — هیچ‌کدام سیگنالی را حذف یا متوقف نمی‌کند:
۱) بستن ناموفق در حد ضرر بدون سیل پیام، ۲) سفارش نامشخص + پیدا کردن پوزیشن،
۳) تحلیل با قیمت لحظهٔ زدن دکمه، ۴) خطوط کارنامه و خبر زیر هشدار."""
import csv
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd

from tests.test_scout_tracker import ENTRY, FRAME, SOFT, make_scout, pos


def row(side="BUY", ref=4175.5, sl_dist=6.0, setup="kijun_pullback"):
    return pd.Series({"setup": setup, "side": side, "ref_close": ref, "sl_dist": sl_dist,
                      "tp_dist": 2 * sl_dist, "atr": 5.0, "bar": pd.Timestamp("2026-10-02 15:15")})


def info():
    return {"setup": "kijun_pullback", "side": "BUY", "entry": ENTRY, "sl": SOFT, "risk": 6.04,
            "r1": False, "warned": False, "emerg": 4157.57, "status_mid": None, "below": False,
            "hold": False, "alert": "1", "approach": True, "ai_next": 0.0, "ai_note": ""}


class Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = make_scout(Path(self._d.name))

    def tearDown(self):
        self._d.cleanup()

    def texts(self):
        return [t for t, _ in self.sc.tg.sent]

    def run_at(self, t, price, broker_sl=SOFT):
        self.sc.client.positions.return_value = [pos(price=price, profit=-7.0, sl=broker_sl)]
        with unittest.mock.patch("tools.scout_bot.time.time", return_value=t):
            self.sc.monitor(FRAME)


class CloseFailure(Base):
    def test_failed_close_retries_quietly_and_notifies_rarely(self):
        self.sc.open[5] = info()
        self.sc.client.close_position.return_value = NS(ok=False, price=0.0, comment="off quotes")
        for i in range(30):                                     # یک دقیقه، حلقهٔ ۲ ثانیه‌ای
            self.run_at(1000.0 + 2 * i, SOFT - 1.0)
        notes = [t for t in self.texts() if "در حد ضرر انجام نشد" in t]
        self.assertEqual(len(notes), 1)                        # قبلاً ۳۰ پیام
        self.assertIn("پیام بعدی حداکثر هر 10 دقیقه", notes[0])
        self.assertEqual(self.sc.client.close_position.call_count, 4)   # هر ۱۵ ثانیه، نه هر حلقه
        self.run_at(1000.0 + 601, SOFT - 1.0)
        self.assertEqual(len([t for t in self.texts() if "در حد ضرر انجام نشد" in t]), 2)
        self.sc.client.close_position.return_value = NS(ok=True, price=SOFT - 1.0, comment="")
        self.run_at(1000.0 + 620, SOFT - 1.0)
        self.assertNotIn(5, self.sc._close_try_t)

    def test_close_works_first_time_like_before(self):
        self.sc.open[5] = info()
        self.sc.client.close_position.return_value = NS(ok=True, price=SOFT - 1.0, comment="")
        self.run_at(1000.0, SOFT - 1.0)
        self.sc.client.close_position.assert_called_once()
        self.assertFalse(any("انجام نشد" in t for t in self.texts()))

    def test_no_ai_message_once(self):
        self.sc.mode = "ai"
        self.sc._ai = False
        self.sc.open[5] = info()
        self.sc.client.close_position.return_value = NS(ok=False, price=0.0, comment="off quotes")
        for i in range(10):
            self.run_at(2000.0 + 2 * i, SOFT - 1.0, broker_sl=4157.57)
        self.assertEqual(len([t for t in self.texts() if "تنظیم نیست" in t]), 1)


class UncertainOrder(Base):
    def setUp(self):
        super().setUp()
        self.sc.client.get_tick.return_value = NS(time=0, bid=4175.4, ask=4175.69)
        self.sc.client.positions.return_value = []

    def test_uncertain_is_not_reported_as_failed_and_gets_adopted(self):
        self.sc.client.send_market_order.return_value = NS(ok=False, uncertain=True,
                                                           comment="order outcome unknown; do not resend")
        self.sc.alert(row(), 27.0)
        self.sc._recon_t = 9e9
        self.sc.decide(str(self.sc.n), True, "cb")
        last = self.sc.tg.edits[-1][1]
        self.assertIn("نامشخص", last)
        self.assertNotIn("ثبت نشد", last)
        self.assertEqual(self.sc._recon_t, 0.0)                # بررسی فوری در حلقهٔ بعد
        with self.sc.journal.open(encoding="utf-8") as fh:
            self.assertIn("order_uncertain", [r["event"] for r in csv.DictReader(fh)])
        self.sc.client.positions.return_value = [pos(tk=77, price=4176.0)]
        self.sc.reconcile(now=100.0)
        self.assertIn(77, self.sc.open)
        self.assertIn("🔎", self.texts()[-1])
        self.sc.reconcile(now=120.0)                           # کمتر از ۶۰ ثانیه: دوباره نه
        self.assertEqual(self.sc.client.positions.call_count, 1)

    def test_definite_failure_still_says_not_placed(self):
        self.sc.client.send_market_order.return_value = NS(ok=False, uncertain=False, comment="no money")
        self.sc.alert(row(), 27.0)
        self.sc.decide(str(self.sc.n), True, "cb")
        self.assertIn("ثبت نشد", self.sc.tg.edits[-1][1])

    def test_every_approval_still_opens_directly(self):
        self.sc.client.send_market_order.return_value = NS(ok=True, position=101, order=101,
                                                           price=4181.0, comment="")
        self.sc.client.get_tick.return_value = NS(time=0, bid=4180.7, ask=4181.0)   # +5.5 به نفع
        self.sc.alert(row(), 27.0)
        self.sc.decide(str(self.sc.n), True, "cb")
        self.sc.client.send_market_order.assert_called_once()  # بدون تأیید دوم یا فیلتر
        self.assertIn(101, self.sc.open)


class InfoLines(Base):
    def test_setup_record_line_and_warning(self):
        self.sc._virtual_rows = ([{"setup": "kijun_pullback", "R": -1.0}] * 20
                                 + [{"setup": "cloud_break", "R": 2.0}] * 12)
        self.sc.alert(row(setup="kijun_pullback"), 27.0)
        self.assertIn("⚠️ کارنامهٔ", self.texts()[-1])
        self.assertIn("-1.00R در 20 نمونهٔ آخر", self.texts()[-1])
        self.sc.alert(row(setup="cloud_break"), 27.0)
        self.assertIn("📈 کارنامهٔ", self.texts()[-1])
        self.sc.alert(row(setup="engulfing"), 27.0)              # کمتر از ۱۰ نمونه: بدون خط
        self.assertNotIn("کارنامهٔ", self.texts()[-1])
        self.assertEqual(len(self.sc.pending), 3)              # همهٔ هشدارها آمدند

    def test_news_line_never_blocks(self):
        self.sc._news = NS(is_news_active=lambda: (True, "⚠️ 12 mins before High-Impact USD: CPI"))
        self.sc.news_mode = "warn"
        self.sc.alert(row(), 27.0, news=self.sc.news_note())
        self.assertIn("خبر مهم نزدیک است", self.texts()[-1])
        self.assertEqual(len(self.sc.pending), 1)
        self.sc.news_mode = "off"
        self.assertEqual(self.sc.news_note(), "")

    def test_account_kind_is_display_only(self):
        from tools.scout_bot import Scout
        self.assertEqual(Scout.account_kind(NS(trade_mode=0)), "دمو")
        self.assertEqual(Scout.account_kind(NS(trade_mode=2)), "واقعی")
        self.assertEqual(Scout.account_kind(None), "نوع نامعلوم")


class FreshAnalysis(Base):
    def test_context_built_at_tap_time(self):
        self.sc.client.get_tick.return_value = NS(time=0, bid=4178.9, ask=4179.2)
        self.sc.client.spread_points.return_value = 30.0
        self.sc.client.positions.return_value = []
        self.sc.alert(row(), 27.0)
        aid = str(self.sc.n)
        ctx = self.sc.fresh_context(aid, self.sc.pending[aid])
        self.assertIn("price NOW 4179.20", ctx)
        self.assertIn("alert age", ctx)
