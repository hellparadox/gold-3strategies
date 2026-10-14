"""سفارش دستی /buy و /sell: اول کارت تأیید؛ باز کردن فقط با «تأیید» و از همان مسیر decide (سقف‌ها، خلاف جهت،
ژورنال)؛ حد ضرر همیشه سقف تنظیمات؛ در آمار سیگنال‌ها و «اگر گرفته بودید» نمی‌آید."""
import csv
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from tests.test_scout_tracker import make_scout
from tools import scout_control as ctl
from tools import scout_dashboard as dash
from tools import scout_signal_audit as sa
from tools import scout_stats as sst


class Manual(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tmp = Path(self._d.name)
        self.sc = make_scout(self.tmp)
        self.sc.admins = {1}
        self.sc.client.get_tick.return_value = NS(time=0, bid=4099.8, ask=4100.1)
        self.sc.client.spread_points.return_value = 30
        self.sc.client.positions.return_value = []
        self.sc.client.send_market_order.return_value = NS(ok=True, position=77, order=77, deal=0,
                                                           price=4100.1, comment="")

    def tearDown(self):
        self._d.cleanup()

    def say(self, text, uid=1):
        self.sc.handle_update({"message": {"from": {"id": uid, "first_name": "Hamid"}, "text": text}})

    def tap(self, data, uid=1, mid=None):
        cq = {"from": {"id": uid, "first_name": "Hamid"}, "data": data, "id": "q"}
        if mid is not None:
            cq["message"] = {"message_id": mid}
        self.sc.handle_update({"callback_query": cq})

    def events(self):
        with self.sc.journal.open(encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def aid(self):
        return next(iter(self.sc.pending))

    # ---- کارت
    def test_buy_shows_card_and_does_not_open(self):
        self.say("/buy")
        text, buttons = self.sc.tg.sent[-1]
        self.assertIn("سفارش دستی", text)
        self.assertIn("BUY", text)
        self.assertIn("4100.10", text)                       # ask
        self.assertIn("4090.10", text)                       # حد ضرر = ask − سقف ۱۰
        self.assertIn("سقف تنظیمات", text)
        self.assertIn("$10.0", text)                         # ریسک ۱۰ نقطه × ۱۰۰ × ۰.۰۱
        self.sc.client.send_market_order.assert_not_called()
        aid = self.aid()
        self.assertEqual([b["callback_data"] for b in buttons[0]], [f"a|{aid}|y", f"a|{aid}|n"])
        self.assertEqual(len(buttons), 1)                    # بدون تحلیل و خلاف جهت

    def test_sell_uses_bid_and_sl_above(self):
        self.say("/sell")
        text, _ = self.sc.tg.sent[-1]
        self.assertIn("SELL", text)
        self.assertIn("4099.80", text)
        self.assertIn("4109.80", text)

    def test_extra_text_is_ignored_and_said(self):
        self.say("/buy 3 sl 2")
        self.assertIn("نادیده گرفته شد", self.sc.tg.sent[-1][0])
        self.assertIn("4090.10", self.sc.tg.sent[-1][0])     # باز هم سقف تنظیمات
        self.say("/buy")
        self.assertNotIn("نادیده", self.sc.tg.sent[-1][0])

    def test_non_admin_ignored(self):
        self.say("/buy", uid=99)
        self.assertEqual(self.sc.pending, {})
        self.assertEqual(self.sc.tg.sent, [])

    # ---- تأیید
    def test_approve_opens_via_decide(self):
        self.say("/buy")
        aid = self.aid()
        self.tap(f"a|{aid}|y", mid=self.sc.pending[aid]["mid"])
        args = self.sc.client.send_market_order.call_args
        self.assertEqual(args.args[0], "BUY")
        self.assertEqual(args.args[1], 0.01)
        info = self.sc.open[77]
        self.assertEqual((info["setup"], info["side"], info["by"]), ("manual", "BUY", "Hamid"))
        self.assertAlmostEqual(info["sl"], 4090.1, places=2)
        opened = [e for e in self.events() if e["event"] == "opened"][-1]
        self.assertEqual(opened["setup"], "manual")
        self.assertIn("by=Hamid", opened["note"])
        self.assertEqual(self.sc.client.send_market_order.call_args.kwargs["comment"].startswith("scout_man"), True)

    def test_approve_uses_price_at_approval(self):
        self.say("/sell")
        aid = self.aid()
        self.sc.client.get_tick.return_value = NS(time=0, bid=4101.0, ask=4101.3)
        self.tap(f"a|{aid}|y")
        self.assertAlmostEqual(self.sc.open[77]["sl"], 4111.0, places=2)    # فاصله ثابت، از قیمت لحظهٔ تأیید

    def test_reject_has_no_whatif(self):
        self.sc.whatif_enabled = True
        self.say("/buy")
        aid = self.aid()
        self.tap(f"a|{aid}|n")
        self.assertEqual(self.sc._whatif, [])
        self.assertIn("rejected", [e["event"] for e in self.events()])
        self.sc.client.send_market_order.assert_not_called()

    def test_expiry_five_minutes_no_whatif(self):
        self.sc.whatif_enabled = True
        self.say("/buy")
        aid = self.aid()
        self.assertEqual(self.sc.pending[aid]["exp"], 300)
        self.sc.pending[aid]["t"] = time.time() - 301
        self.sc.expire()
        self.assertEqual(self.sc.pending, {})
        self.assertEqual(self.sc._whatif, [])
        self.assertIn("منقضی", self.sc.tg.edits[-1][1])

    def test_new_card_replaces_old(self):
        self.say("/buy")
        old = self.aid()
        self.say("/sell")
        self.assertEqual(len(self.sc.pending), 1)
        self.assertNotIn(old, self.sc.pending)
        self.assertIn("جایگزین", self.sc.tg.edits[-1][1])

    def test_old_card_button_after_replace_is_invalid(self):
        self.say("/buy")
        old = self.aid()
        self.say("/sell")
        self.tap(f"a|{old}|y")
        self.sc.client.send_market_order.assert_not_called()

    def test_stale_button_from_other_message_ignored(self):
        self.say("/buy")
        aid = self.aid()
        self.tap(f"a|{aid}|y", mid=999999)
        self.sc.client.send_market_order.assert_not_called()
        self.assertIn(aid, self.sc.pending)

    # ---- بازار / داده / تنظیمات
    def test_market_closed(self):
        self.sc.market_open = lambda: False
        self.say("/buy")
        self.assertIn("بازار بسته", self.sc.tg.sent[-1][0])
        self.assertEqual(self.sc.pending, {})

    def test_no_tick(self):
        self.sc.client.get_tick.return_value = None
        self.say("/sell")
        self.assertIn("قیمت از MT5 نرسید", self.sc.tg.sent[-1][0])
        self.assertEqual(self.sc.pending, {})

    def test_cap_missing_refuses(self):
        self.sc.sl_cap = None
        self.say("/buy")
        self.assertIn("سقف حد ضرر", self.sc.tg.sent[-1][0])
        self.assertEqual(self.sc.pending, {})

    def test_works_when_alerts_paused(self):
        self.sc.paused = True
        self.say("/buy")
        self.assertEqual(len(self.sc.pending), 1)

    # ---- سقف‌ها و خلاف جهت از همان مسیر decide
    def test_opposite_position_asks_hedge_question(self):
        self.sc.open[5] = {"setup": "kijun_pullback", "side": "SELL", "entry": 4105.0, "sl": 4111.0, "risk": 6.0,
                           "lot": 0.01, "emerg": None, "hold": False}
        self.say("/buy")
        self.assertIn("خلاف جهت", self.sc.tg.sent[-1][0])
        aid = self.aid()
        self.tap(f"a|{aid}|y")
        self.sc.client.send_market_order.assert_not_called()
        self.assertIn("hedge_confirm", [e["event"] for e in self.events()])
        self.assertIn(aid, self.sc.pending)

    def test_max_open_asks_owner(self):
        for tk, side in ((5, "BUY"), (6, "BUY")):
            self.sc.open[tk] = {"setup": "x", "side": side, "entry": 4100.0, "sl": 4094.0, "risk": 6.0,
                                "lot": 0.01, "emerg": None, "hold": False}
        self.say("/buy")
        aid = self.aid()
        self.tap(f"a|{aid}|y")
        self.sc.client.send_market_order.assert_not_called()
        self.assertIn("limit_confirm", [e["event"] for e in self.events()])

    def test_dry_mode_places_nothing(self):
        self.sc.dry = True
        self.say("/buy")
        self.tap(f"a|{self.aid()}|y")
        self.sc.client.send_market_order.assert_not_called()
        self.assertIn("approved_dry", [e["event"] for e in self.events()])

    # ---- منو و راهنما
    def test_menu_buttons_and_commands(self):
        flat = [b["callback_data"] for row in self.sc.MENU_BUTTONS for b in row]
        self.assertIn("k|buy", flat)
        self.assertIn("k|sell", flat)
        cmds = [c["command"] for c in ctl.MENU_COMMANDS]
        self.assertIn("buy", cmds)
        self.assertIn("sell", cmds)
        self.assertIn("/buy", ctl.HELP_TEXT)

    def test_menu_button_creates_card(self):
        self.tap("k|sell")
        self.assertEqual(self.sc.pending[self.aid()]["row"].side, "SELL")

    # ---- آمار: سفارش دستی سیگنال نیست
    def test_manual_not_counted_as_alert_or_audit(self):
        self.say("/buy")
        aid = self.aid()
        self.tap(f"a|{aid}|y")
        ev = self.events()
        self.assertEqual([e["event"] for e in ev if e["event"] == "alert"], [])
        self.assertEqual(len(sa.read_alerts(self.sc.journal)), 0)
        self.assertEqual(len(sst.build(ev)), 0)

    def test_manual_does_not_hijack_stale_alert_with_same_number(self):
        """بعد از ری‌استارت شمارهٔ هشدار از ۱ شروع می‌شود؛ سفارش دستی نباید به هشدار بی‌جواب قبلی بچسبد."""
        self.sc.log(event="alert", alert="1", setup="rejection", side="BUY", price=4000, sl=3994, tp=4012)
        self.sc.n = 0
        self.say("/buy")
        self.tap(f"a|{self.aid()}|y")
        df = sa.read_alerts(self.sc.journal)
        self.assertEqual(list(df.decision), ["pending"])

    def test_trades_table_shows_manual_row(self):
        self.say("/buy")
        self.tap(f"a|{self.aid()}|y")
        self.sc.log(event="gone", alert="x", setup="manual", side="BUY", ticket=77, entry=4100.1, price=4101.0,
                    profit=0.9, note="exit_tp;by=Hamid;cby=Hamid")
        rows = dash.build_trades(self.events(), 0.01, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["setup"], "manual")
        self.assertIn("Hamid", rows[0]["opened_by"])
        self.assertIn("setupFa(x.setup)", dash.PAGE)                  # صفحه «manual» را «✋ دستی» نشان می‌دهد


if __name__ == "__main__":
    unittest.main()
