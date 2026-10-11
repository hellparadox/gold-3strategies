"""تمیزکاری ایمنی (۰۰۳۱):
- خطای لحظه‌ای MT5 روی یک پوزیشن، مدیریت بقیه را از دور خارج نمی‌کند؛ دکمه‌ها هم جواب می‌گیرند.
- روشن نشدن اسکات (اتصال MT5) در تلگرام گفته می‌شود (نه بیشتر از هر ۳۰ دقیقه).
- توکن تلگرام در لاگ/خطا نمی‌ماند.
- soft_stop خاموش: لغو نگه داشتن و قیمت خروجِ بازار بسته هم روی بروکر می‌رود.
- «ببند و جهت را عوض کن»: اگر بعد از بستن هم سقف پر است، قبل از بستن می‌پرسد.
- ساعت تهران در کارنامه با ساعت زمستانی بروکر جابه‌جا می‌شود."""
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd

from tests.test_scout_tracker import ENTRY, FRAME, SOFT, make_scout
from tools import scout_bot
from tools import scout_signal_audit as sa


def info(**kw):
    d = {"setup": "kijun_pullback", "side": "BUY", "entry": ENTRY, "sl": SOFT, "risk": 6.04, "r1": False,
         "warned": False, "emerg": 4157.57, "status_mid": None, "below": False, "hold": False, "alert": "1",
         "approach": False, "ai_next": 0.0, "ai_note": ""}
    d.update(kw)
    return d


def pos(tk, price=4178.0, sl=SOFT, tp=0.0, ptype=0):
    return NS(ticket=tk, type=ptype, price_open=ENTRY, price_current=price, profit=1.0, swap=0.0,
              sl=sl, tp=tp, time=1_759_300_000 - 3600, comment="scout_kp", magic=735777)


class Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tmp = Path(self._d.name)
        self.sc = make_scout(self.tmp)
        self.sc.admins = {1}

    def tearDown(self):
        self._d.cleanup()

    def run_at(self, t, positions):
        self.sc.client.positions.return_value = positions
        with unittest.mock.patch("tools.scout_bot.time.time", return_value=t):
            self.sc.monitor(FRAME)


class MonitorIsolation(Base):
    def test_one_failing_position_does_not_stop_the_others(self):
        self.sc.open[5], self.sc.open[6] = info(), info(alert="2")
        orig = self.sc._monitor_one

        def flaky(tk, *a, **k):
            if tk == 5:
                raise RuntimeError("position 5 unavailable")
            return orig(tk, *a, **k)

        self.sc._monitor_one = flaky
        self.run_at(1000.0, [pos(5), pos(6)])
        self.assertTrue(any("📊" in t and "#6" in t for t, _ in self.sc.tg.sent))      # #6 مدیریت شد
        warn = [t for t, _ in self.sc.tg.sent if "مدیریت #5 یک لحظه انجام نشد" in t]
        self.assertEqual(len(warn), 1)
        self.assertIn("حد ضرر روی بروکر سر جایش است", warn[0])
        self.run_at(1100.0, [pos(5), pos(6)])                                           # کمتر از ۱۰ دقیقه
        self.assertEqual(sum("مدیریت #5" in t for t, _ in self.sc.tg.sent), 1)
        self.run_at(1700.0, [pos(5), pos(6)])
        self.assertEqual(sum("مدیریت #5" in t for t, _ in self.sc.tg.sent), 2)

    def test_close_button_survives_mt5_error(self):
        self.sc.open[5] = info()
        self.sc.client.close_position.side_effect = RuntimeError("position 5 unavailable")
        self.sc.close(5, "cb")
        self.assertIn("MT5 جواب نداد", self.sc.tg.acks[-1])
        self.assertNotIn(5, self.sc._closing_t)                 # بلافاصله می‌شود دوباره زد
        self.sc.client.close_position.side_effect = None
        self.sc.close(5, "cb")
        self.assertEqual(self.sc.tg.acks[-1], "بسته شد")

    def test_failing_update_gets_an_answer_and_loop_continues(self):
        self.sc.handle_update = unittest.mock.Mock(side_effect=RuntimeError("boom"))
        self.sc._safe_update({"callback_query": {"id": "q1", "data": "c|5", "from": {"id": 1}}})
        self.assertIn("دوباره بزنید", self.sc.tg.acks[-1])
        self.sc._safe_update({"message": {"text": "/status", "from": {"id": 1}}})    # بدون دکمه: بی‌صدا


class Startup(Base):
    def setUp(self):
        super().setUp()
        cfg = {"scout.startup_fail_file": str(self.tmp / "startup_fail.json")}
        self.sc.s = NS(get=lambda k, d=None: cfg.get(k, d))

    def test_notice_throttled(self):
        t0 = 1_760_000_000.0
        self.sc._startup_failed("اتصال به MT5 برقرار نشد", now=t0)
        self.sc._startup_failed("اتصال به MT5 برقرار نشد", now=t0 + 600)        # تسک ۱۰ دقیقه بعد
        self.assertEqual(sum("اسکات روشن نشد" in t for t, _ in self.sc.tg.sent), 1)
        self.sc._startup_failed("اتصال به MT5 برقرار نشد", now=t0 + 1900)
        self.assertEqual(sum("اسکات روشن نشد" in t for t, _ in self.sc.tg.sent), 2)

    def test_run_tells_telegram_when_mt5_fails(self):
        self.sc.client.connect.return_value = False
        with self.assertRaises(SystemExit):
            self.sc.run()
        self.assertIn("اسکات روشن نشد", self.sc.tg.sent[-1][0])
        self.assertIn("حد ضرر روی بروکر", self.sc.tg.sent[-1][0])


class TokenInLogs(unittest.TestCase):
    def test_token_masked(self):
        tg = scout_bot.TG("123456:SECRETtoken", 1)
        err = Exception("Max retries: https://api.telegram.org/bot123456:SECRETtoken/sendMessage")
        with unittest.mock.patch("tools.scout_bot.requests.post", side_effect=err), \
                unittest.mock.patch("tools.scout_bot.logger") as log:
            self.assertIsNone(tg._call("sendMessage", chat_id=1, text="x"))
            self.assertIsNone(tg.send_photo(b"png"))
        self.assertNotIn("SECRET", tg.last_error)
        self.assertIn("bot***/sendMessage", tg.last_error)
        for c in log.warning.call_args_list:
            self.assertNotIn("SECRET", " ".join(str(a) for a in c.args))


class PlainStopExits(Base):
    """soft_stop خاموش: قیمت‌های خروج مالک هم باید روی بروکر بروند و برداشته شوند."""

    def setUp(self):
        super().setUp()
        self.sc.soft_stop = False

    def test_unhold_removes_exits_from_broker(self):
        self.sc.open[5] = info(hold=True, xtp=4190.0, xsl=4165.0, tp_owned=True, emerg=None)
        self.sc.hold(5, on=False)
        self.assertTrue(self.sc.open[5]["xdirty"])
        self.run_at(1000.0, [pos(5, sl=4165.0, tp=4190.0)])
        self.sc.client.modify_sltp.assert_called_with(5, sl=SOFT, tp=0.0)
        self.assertFalse(self.sc.open[5].get("xdirty"))
        self.assertFalse(self.sc.open[5]["tp_owned"])
        self.sc.client.modify_sltp.reset_mock()
        self.run_at(1010.0, [pos(5, sl=SOFT, tp=0.0)])                              # دیگر کاری نیست
        self.sc.client.modify_sltp.assert_not_called()

    def test_exits_saved_while_closed_go_to_broker_on_open(self):
        self.sc.open[5] = info(hold=True, emerg=None)
        self.sc.market_open = lambda: False
        self.assertTrue(self.sc._apply_exits(5, 4190.0, 4165.0))
        self.sc.client.modify_sltp.assert_not_called()
        self.assertTrue(self.sc.open[5]["xdirty"])
        self.run_at(1000.0, [pos(5)])                                                # هنوز بسته
        self.sc.client.modify_sltp.assert_not_called()
        self.sc.market_open = lambda: True
        self.run_at(1100.0, [pos(5)])
        self.sc.client.modify_sltp.assert_called_with(5, sl=4165.0, tp=4190.0)
        self.assertFalse(self.sc.open[5].get("xdirty"))

    def test_one_shot_then_manual_mt5_change_is_kept(self):
        self.sc.open[5] = info(hold=True, emerg=None)
        self.sc.market_open = lambda: False
        self.sc._apply_exits(5, 4190.0, 4175.69)                                    # بی‌ضرر، با بازار بسته
        self.sc.market_open = lambda: True
        self.run_at(1000.0, [pos(5)])
        self.assertEqual(self.sc.client.modify_sltp.call_count, 1)
        self.sc.client.modify_sltp.reset_mock()
        for t in (1010.0, 1100.0, 2000.0):                                           # مالک در MT5 حد ضرر را بالا برد
            self.run_at(t, [pos(5, price=4185.0, sl=4180.0, tp=4190.0)])
        self.sc.client.modify_sltp.assert_not_called()

    def test_failed_sync_retried_after_a_minute(self):
        self.sc.open[5] = info(hold=True, xsl=4165.0, emerg=None)
        self.sc.hold(5, on=False)
        self.sc.client.modify_sltp.return_value = NS(ok=False, comment="market busy")
        self.run_at(1000.0, [pos(5, sl=4165.0)])
        self.assertTrue(self.sc.open[5]["xdirty"])
        self.assertEqual(sum("گذاشته نشد" in t for t, _ in self.sc.tg.sent), 1)
        self.run_at(1002.0, [pos(5, sl=4165.0)])
        self.assertEqual(self.sc.client.modify_sltp.call_count, 1)                  # هنوز یک دقیقه نشده
        self.sc.client.modify_sltp.return_value = NS(ok=True, comment="")
        self.run_at(1062.0, [pos(5, sl=4165.0)])
        self.sc.client.modify_sltp.assert_called_with(5, sl=SOFT)
        self.assertFalse(self.sc.open[5].get("xdirty"))
        self.assertEqual(sum("گذاشته نشد" in t for t, _ in self.sc.tg.sent), 1)

    def test_never_closes_at_market_for_the_signal_stop(self):
        self.sc.open[5] = info(hold=True, xtp=4190.0, tp_owned=True, emerg=None)
        self.sc.hold(5, on=False)
        self.run_at(1000.0, [pos(5, price=4160.0, sl=4150.0, tp=4190.0)])           # قیمت زیر حد ضرر سیگنال
        self.sc.client.close_position.assert_not_called()
        self.sc.client.modify_sltp.assert_called_with(5, sl=4150.0, tp=0.0)         # فقط حد سود برداشته شد
        self.assertTrue(any("حد ضرر بروکر همان قبلی" in t for t, _ in self.sc.tg.sent))

    def test_owner_exit_crossed_while_closed_closes_on_open(self):
        self.sc.open[5] = info(hold=True, emerg=None)
        self.sc.market_open = lambda: False
        self.sc._apply_exits(5, 4190.0, 4170.0)
        self.sc.market_open = lambda: True
        self.run_at(1000.0, [pos(5, price=4166.0, sl=SOFT)])                         # با فاصله زیر قیمت خروج شما باز شد
        self.sc.client.close_position.assert_called_once()
        self.assertEqual(self.sc.open[5]["why"], "exit_sl")

    def test_plain_position_untouched(self):
        self.sc.open[5] = info(emerg=None)
        self.run_at(1000.0, [pos(5, sl=SOFT - 3.0)])                                # مالک در MT5 عوضش کرده
        self.sc.client.modify_sltp.assert_not_called()


class ReverseAsksFirst(Base):
    def setUp(self):
        super().setUp()
        self.sc.soft_stop = False
        self.sc.client.get_tick.return_value = NS(time=0, bid=4099.8, ask=4100.1)
        self.sc.client.send_market_order.return_value = NS(ok=True, position=77, order=77, deal=0,
                                                           price=4100.1, comment="")
        self.sc.open[8] = info(side="SELL", entry=4105.0, sl=4111.0, risk=6.0)
        self.sc.client.positions.return_value = [pos(8, price=4100.0, sl=4111.0, ptype=1)]

    def tap(self, data):
        self.sc.handle_update({"callback_query": {"from": {"id": 1}, "data": data, "id": "q"}})

    def alert(self):
        r = pd.Series({"setup": "rejection", "side": "BUY", "ref_close": 4100.0, "sl_dist": 8.0,
                       "tp_dist": 16.0, "atr": 5.0, "bar": pd.Timestamp("2026-10-06 09:15")})
        self.sc.alert(r, 27.0)
        return str(self.sc.n)

    def test_cap_question_before_closing(self):
        self.sc.max_total_risk = 5.0                       # حتی بعد از بستن #8، ریسک ۸ دلار بیشتر از سقف است
        aid = self.alert()
        self.tap(f"a|{aid}|y")
        self.assertIn(f"a|{aid}|r", str(self.sc.tg.edits[-1][2]))                  # سؤال پوزیشن مخالف
        self.tap(f"a|{aid}|r")
        self.sc.client.close_position.assert_not_called()                            # هنوز چیزی بسته نشد
        self.sc.client.send_market_order.assert_not_called()
        mid, text, buttons = self.sc.tg.edits[-1]
        self.assertIn("حتی بعد از بستن #8", text)
        self.assertIn("هنوز چیزی بسته نشده", text)
        self.assertEqual([b["callback_data"] for b in buttons[0]], [f"a|{aid}|x", f"a|{aid}|n"])
        self.assertIn(8, self.sc.open)
        self.assertNotIn("closing", self.sc.open[8])                                 # فرض موقت پاک شد
        self.tap(f"a|{aid}|n")                                                       # رد: هر دو دست نخورد
        self.sc.client.close_position.assert_not_called()
        self.sc.client.send_market_order.assert_not_called()

    def test_confirm_closes_then_opens(self):
        self.sc.max_total_risk = 5.0
        aid = self.alert()
        self.tap(f"a|{aid}|y")
        self.tap(f"a|{aid}|r")
        self.tap(f"a|{aid}|x")
        self.sc.client.close_position.assert_called_once()
        self.assertEqual(self.sc.client.close_position.call_args.args[0], 8)
        self.assertEqual(self.sc.client.send_market_order.call_args.args[0], "BUY")

    def test_no_cap_problem_reverses_as_before(self):
        aid = self.alert()                                 # سقف ۲۰ دلار: بعد از بستن #8 جا هست
        self.tap(f"a|{aid}|y")
        self.tap(f"a|{aid}|r")
        self.sc.client.close_position.assert_called_once()
        self.assertEqual(self.sc.client.send_market_order.call_args.args[0], "BUY")


class TehranOffset(unittest.TestCase):
    def test_summer_and_winter(self):
        self.assertEqual(sa.server_to_tehran_minutes("2026-10-06 09:15:00"), 30)
        self.assertEqual(sa.server_to_tehran_minutes("2026-11-10 09:15:00"), 90)
        self.assertEqual(sa.server_to_tehran_minutes("2027-03-20 09:15:00"), 30)

    def test_tehran_hour_auto_and_explicit(self):
        self.assertEqual(sa.tehran_hour("2026-10-06 09:15:00"), 10)        # 09:30 سرور + 30 = 10:00
        self.assertEqual(sa.tehran_hour("2026-11-10 09:15:00"), 11)        # 09:30 سرور + 90 = 11:00
        self.assertEqual(sa.tehran_hour("2026-11-10 09:15:00", 30), 10)    # عدد دستی هنوز کار می‌کند
        self.assertIsNone(sa.tehran_hour("not a time"))


if __name__ == "__main__":
    unittest.main()
