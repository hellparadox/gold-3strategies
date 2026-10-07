"""قیمت خروج پوزیشن نگه‌داشته («اگر به ۴۱۲۰ رسید ببند، اگر به ۴۰۹۵ رسید ببند») روی بروکر، و کارت پایان
معامله. هیچ‌کدام به سیگنال‌ها، تأیید، سقف‌ها یا پوزیشن نگه‌داشته‌نشده دست نمی‌زند."""
import csv
import tempfile
import unittest
import unittest.mock
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd

from tests.test_scout_tracker import make_scout
from tools import scout_followup as fu
from tools import scout_tracker as trk
from tools.scout_chart import render_trade_card

ENTRY, SOFT = 4117.18, 4111.18                    # BUY، ریسک ۶ → اضطراری 4099.18 (۱۸$)
EMERG = 4099.18
FRAME = pd.DataFrame({"close": [4106.0], "tenkan": [4110.0]})
T0 = 1_791_000_000                                 # زمان سرور (ثانیه)


def held():
    return {"setup": "cloud_break", "side": "BUY", "entry": ENTRY, "sl": SOFT, "risk": 6.0, "r1": False,
            "warned": True, "emerg": EMERG, "status_mid": None, "below": False, "hold": True, "alert": "1",
            "approach": False, "ai_next": 0.0, "ai_note": "", "status_t": 1e18}


def live(price=4105.4, sl=EMERG, tp=0.0):
    return NS(ticket=5, type=0, price_open=ENTRY, price_current=price, profit=(price - ENTRY), swap=0.0,
              sl=sl, tp=tp, time=T0, comment="scout_cb", magic=735777)


class Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tmp = Path(self._d.name)
        self.sc = make_scout(self.tmp)
        self.sc.admins = {1}
        self.sc.emerg_cap = 20.0                       # فاصلهٔ قیمت = ۲۰$ با ۰.۰۱ لات
        self.sc.client.get_tick.return_value = NS(time=T0 + 600, bid=4105.4, ask=4105.7)
        self.sc.client.positions.return_value = [live()]
        self.sc.open[5] = held()

    def tearDown(self):
        if self.sc._chart_pool:
            self.sc._chart_pool.shutdown(wait=True)
        self._d.cleanup()

    def tap(self, data, uid=1):
        self.sc.handle_update({"callback_query": {"from": {"id": uid, "first_name": "Hamid"}, "data": data,
                                                  "id": "q"}})

    def reply(self, text, mid=None, uid=1):
        mid = self.sc.open[5]["xprompt"] if mid is None else mid
        self.sc.handle_update({"message": {"from": {"id": uid}, "text": text,
                                           "reply_to_message": {"message_id": mid}}})

    def ask(self):
        self.tap("e|5")
        return self.sc.open[5]["xprompt"]

    def last(self):
        return self.sc.tg.sent[-1][0]

    def events(self):
        if not self.sc.journal.exists():
            return []
        with self.sc.journal.open(encoding="utf-8") as fh:
            return [r["event"] for r in csv.DictReader(fh)]


class Pure(unittest.TestCase):
    def test_parse_numbers(self):
        self.assertEqual(trk.parse_numbers("۴۱۲۰ و 4,095.5"), [4120.0, 4095.5])
        self.assertEqual(trk.parse_numbers("۴٬۱۲۰"), [4120.0])
        self.assertEqual(trk.parse_numbers("سلام"), [])

    def test_classify(self):
        self.assertEqual(trk.classify_exits("BUY", 4105.4, [4095, 4120]), (4120.0, 4095.0))
        self.assertEqual(trk.classify_exits("BUY", 4105.4, [4120]), (4120.0, None))
        self.assertEqual(trk.classify_exits("SELL", 4105.7, [4090, 4115]), (4090.0, 4115.0))
        for bad in ([], [4110, 4120], [4090, 4095], [1, 2, 3], [41.0]):
            with self.assertRaises(ValueError):
                trk.classify_exits("BUY", 4105.4, bad)

    def test_desired_sl_uses_owner_level_only_when_held(self):
        info = dict(held(), xsl=4095.0)
        self.assertEqual(trk.desired_broker_sl(info, "close"), 4095.0)
        self.assertEqual(trk.desired_broker_sl(dict(info, hold=False), "close"), SOFT)

    def test_state_keys_persist_exits(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            trk.save_state(p, {5: dict(held(), xtp=4120.0, xsl=4095.0, xprompt=77, tp_owned=True, by="Hamid")})
            got = trk.load_state(p)[5]
            self.assertEqual((got["xtp"], got["xsl"], got["xprompt"], got["tp_owned"], got["by"]),
                             (4120.0, 4095.0, 77, True, "Hamid"))


class Exits(Base):
    def test_button_only_on_held_position(self):
        self.assertEqual(self.sc._close_buttons(5)[1][0]["callback_data"], "e|5")
        self.sc.open[5]["hold"] = False
        self.assertEqual(len(self.sc._close_buttons(5)), 1)
        self.tap("e|5")                                           # کلیک قدیمی روی پوزیشن نگه‌داشته‌نشده
        self.assertIsNone(self.sc.open[5].get("xprompt"))

    def test_prompt_asks_for_reply(self):
        mid = self.ask()
        self.assertTrue(mid)
        self.assertTrue(self.sc.tg.keyboards[-1]["force_reply"])
        self.assertIn("4105.40", self.last())
        self.assertIn("ریپلای", self.last())

    def test_two_numbers_over_cap_asks_then_sets_on_broker(self):
        self.ask()
        self.reply("4120 4095")                                   # 4095 = ۲۲.۱۸$ ضرر > ۲۰$
        self.sc.client.modify_sltp.assert_not_called()
        self.assertIn("22.18$", self.last())
        self.assertEqual([b["callback_data"] for b in self.sc.tg.sent[-1][1][0]], ["e|5|y", "e|5|n"])
        self.assertIn("exit_confirm", self.events())
        self.tap("e|5|y")
        self.sc.client.modify_sltp.assert_called_once_with(5, sl=4095.0, tp=4120.0)
        info = self.sc.open[5]
        self.assertEqual((info["xtp"], info["xsl"], info["tp_owned"]), (4120.0, 4095.0, True))
        self.assertIn("ثبت شد روی بروکر", self.last())
        self.assertIn("+2.82$", self.last())
        self.assertIn("-22.18$", self.last())
        self.assertEqual(trk.load_state(self.sc.state_path)[5]["xsl"], 4095.0)   # بعد از ری‌استارت هم
        self.assertGreater(self.sc.open_risk_usd(), 22.0)          # در ریسک کل هم حساب می‌شود

    def test_over_cap_no_changes_nothing(self):
        self.ask()
        self.reply("4095 4120")
        self.tap("e|5|n")
        self.sc.client.modify_sltp.assert_not_called()
        self.assertIsNone(self.sc.open[5].get("xsl"))
        self.assertIn("عوض نشد", self.last())

    def test_within_cap_applies_directly_and_one_number(self):
        self.ask()
        self.reply("۴۱۰۰ ۴۱۲۰")
        self.sc.client.modify_sltp.assert_called_with(5, sl=4100.0, tp=4120.0)
        self.ask()
        self.reply("4120")                                         # فقط بالا؛ پایین = اضطراری
        self.sc.client.modify_sltp.assert_called_with(5, sl=EMERG, tp=4120.0)
        self.assertIsNone(self.sc.open[5]["xsl"])
        self.assertIn("حد ضرر اضطراری 4099.18", self.last())

    def test_invalid_input_sets_nothing(self):
        self.ask()
        for txt in ("4110 4120", "سلام", "4095 4096 4120"):
            self.reply(txt)
            self.assertIn("⚠️", self.last())
        self.sc.client.modify_sltp.assert_not_called()
        self.assertIsNone(self.sc.open[5].get("xtp"))

    def test_broker_reject_reverts(self):
        self.sc.client.modify_sltp.return_value = NS(ok=False, comment="Invalid stops")
        self.ask()
        self.reply("4106 4100")
        self.assertIsNone(self.sc.open[5]["xtp"])
        self.assertIn("بروکر قبول نکرد", self.last())
        self.assertIn("exit_failed", self.events())

    def test_market_closed_saved_then_synced_on_open(self):
        self.sc.market_open = lambda: False
        self.ask()
        self.reply("4120 4100")
        self.sc.client.modify_sltp.assert_not_called()
        self.assertIn("بازار بسته است", self.last())
        self.sc.market_open = lambda: True
        self.sc.monitor(FRAME)
        self.sc.client.modify_sltp.assert_called_once_with(5, sl=4100.0, tp=4120.0)
        self.assertTrue(self.sc.open[5]["tp_owned"])

    def test_gap_past_exit_while_tp_not_on_broker_closes(self):
        self.sc.market_open = lambda: False
        self.ask()
        self.reply("4110 4100")
        self.sc.market_open = lambda: True
        self.sc.client.positions.return_value = [live(price=4112.0)]   # بازار با گپ بالای 4110 باز شد
        self.sc.monitor(FRAME)
        self.sc.client.close_position.assert_called_once()
        self.assertEqual(self.sc.open[5]["why"], "exit_tp")

    def test_delete_and_unhold_remove_exits(self):
        self.ask()
        self.reply("4120 4100")
        self.tap("e|5|d")
        self.sc.client.modify_sltp.assert_called_with(5, sl=EMERG, tp=0.0)
        self.assertIsNone(self.sc.open[5]["xtp"])
        self.assertIn("حذف شد", self.last())
        self.ask()
        self.reply("4120 4100")
        self.tap("u|5")
        self.assertIn("قیمت‌های خروج شما هم حذف شد", self.last())
        self.assertIsNone(self.sc.open[5]["xtp"])
        self.sc.client.modify_sltp.reset_mock()
        self.sc.client.positions.return_value = [live(price=4112.0, sl=4100.0, tp=4120.0)]
        self.sc.monitor(FRAME)                                      # حد سود اسکات برداشته، حد ضرر سیگنال
        self.sc.client.modify_sltp.assert_called_once_with(5, sl=SOFT, tp=0.0)

    def test_manual_tp_of_unheld_position_is_never_touched(self):
        self.sc.open[5]["hold"] = False
        self.sc.client.positions.return_value = [live(price=4115.0, sl=SOFT, tp=4130.0)]
        self.sc.monitor(FRAME)
        self.sc.client.modify_sltp.assert_not_called()

    def test_closed_at_owner_exit_is_reported(self):
        self.ask()
        self.reply("4120 4100")
        self.sc.client.positions.return_value = []
        self.sc.client.deals_for_position.return_value = [
            NS(entry=0, price=ENTRY, profit=0.0, time=T0), NS(entry=1, price=4120.0, profit=2.82, time=T0 + 4000)]
        self.sc.monitor(FRAME)
        self.assertNotIn(5, self.sc.open)
        self.assertIn("به قیمت خروج شما (4120.00) رسید", self.last())

    def test_non_admin_and_unrelated_reply_ignored(self):
        mid = self.ask()
        self.tap("e|5|d", uid=99)
        self.reply("4120 4100", uid=99)
        self.reply("4120 4100", mid=mid + 50)                       # ریپلای به پیام دیگر
        self.sc.client.modify_sltp.assert_not_called()


class Card(Base):
    def m1(self, start, n):
        idx = pd.to_datetime(np.arange(n) * 60 + start, unit="s")
        c = 4117.0 + np.cumsum(np.random.default_rng(1).normal(0, 0.4, n))
        o = np.r_[c[0], c[:-1]]
        return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.2, "low": np.minimum(o, c) - 0.2,
                             "close": c}, index=idx)

    def test_caption(self):
        txt = fu.trade_card_caption(5, "BUY", "cloud_break", ENTRY, 4120.0, 2.82, 0.47, 4320, "exit_tp", "Hamid",
                                    date(2026, 10, 7))
        for part in ("کارت معامله #5", "✅ <b>+2.82$</b> (+0.47R)", "ورود 4117.18 ← خروج 4120.00",
                     "1 ساعت و 12 دقیقه", "🎯 قیمت خروج شما", "👤 تأیید: Hamid", "چهارشنبه 15 مهر 1405"):
            self.assertIn(part, txt)
        self.assertIn("بیرون از اسکات", fu.trade_card_caption(5, "SELL", "x", 1, 2, -1, None, None, None, None,
                                                               date(2026, 10, 7)))

    def test_render_png(self):
        bars = self.m1(T0 - 900, 120)
        for t_out in (T0 + 3000, T0 + 60):
            png = render_trade_card(bars, "BUY", ENTRY, 4120.0, pd.Timestamp(T0, unit="s"),
                                    pd.Timestamp(t_out, unit="s"), 2.82, 0.47, "cloud_break", 5,
                                    levels={"sl": SOFT, "xtp": 4120.0, "xsl": 4100.0})
            self.assertTrue(png.startswith(b"\x89PNG"))
        long = self.m1(T0, 3000)                                    # معاملهٔ طولانی: فشرده می‌شود
        self.assertTrue(render_trade_card(long, "SELL", 4117.0, 4110.0, long.index[5], long.index[-5], 7.0,
                                          None, "x", 6, timeframe="M15").startswith(b"\x89PNG"))

    def test_card_sent_after_close_in_background(self):
        self.sc.card_enabled = True
        self.sc.open[5]["by"] = "Hamid"
        self.ask()
        self.reply("4120 4100")
        self.sc.tg.send_photo = unittest.mock.Mock(return_value=1)
        self.sc.client.positions.return_value = []
        self.sc.client.deals_for_position.return_value = [
            NS(entry=0, price=ENTRY, profit=0.0, time=T0), NS(entry=1, price=4120.0, profit=2.82, time=T0 + 4000)]
        self.sc.client.get_rates.return_value = self.m1(T0 - 1200, 120)
        self.sc.monitor(FRAME)
        self.sc._chart_pool.shutdown(wait=True)
        self.sc._chart_pool = None
        png, caption = self.sc.tg.send_photo.call_args.args[:2]
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertIn("🎯 قیمت خروج شما (بالا)", caption)
        self.assertIn("👤 تأیید: Hamid", caption)
        self.assertIn("+0.47R", caption)
        self.assertEqual(self.sc.client.get_rates.call_args.args[0], "M1")

    def test_card_failure_is_quiet(self):
        self.sc.card_enabled = True
        self.sc.tg.send_photo = unittest.mock.Mock()
        self.sc.client.positions.return_value = []
        self.sc.client.deals_for_position.return_value = [
            NS(entry=0, price=ENTRY, profit=0.0, time=T0), NS(entry=1, price=4110.0, profit=-7.18, time=T0 + 60)]
        self.sc.client.get_rates.side_effect = RuntimeError("no history")
        self.sc.monitor(FRAME)
        self.assertIn("بسته شد", self.last())
        self.sc.tg.send_photo.assert_not_called()

    def test_approver_name_saved(self):
        self.sc.open.clear()
        self.sc.client.positions.return_value = []
        self.sc.client.send_market_order.return_value = NS(ok=True, position=9, order=9, deal=0, price=4105.7,
                                                           comment="")
        row = pd.Series({"setup": "cloud_break", "side": "BUY", "ref_close": 4105.7, "sl_dist": 6.0,
                         "tp_dist": 12.0, "atr": 5.0, "bar": pd.Timestamp("2026-10-07 09:00")})
        self.sc.alert(row, 27.0)
        self.tap(f"a|{self.sc.n}|y")
        self.assertEqual(self.sc.open[9]["by"], "Hamid")


if __name__ == "__main__":
    unittest.main()
