"""«🔒 بی‌ضرر کن + هدف 2R» زیر پیام «به ۱ برابر ریسک رسید»: حد ضرر روی قیمت ورود و حد سود روی 2R، هر دو روی
بروکر. اگر قیمت زیر ورود یا بالای هدف باشد یا بروکر قبول نکند، هیچ چیز عوض نمی‌شود."""
import csv
import unittest
import unittest.mock
from datetime import date
from types import SimpleNamespace as NS

from tests.test_scout_exits import ENTRY, FRAME, SOFT, T0, Base, held, live
from tools import scout_followup as fu
from tools import scout_tracker as trk

TP2R = round(ENTRY + 12.0, 2)                        # ریسک ۶ → هدف 4129.18


class Lock(Base):
    def setUp(self):
        super().setUp()
        self.sc.open[5] = dict(held(), hold=False, warned=False, r1=False)
        self.price(4124.0)

    def price(self, bid):
        self.sc.client.get_tick.return_value = NS(time=T0 + 600, bid=bid, ask=bid + 0.3)
        self.sc.client.positions.return_value = [live(price=bid, sl=SOFT)]

    def lock(self, uid=1):
        self.tap("z|5", uid=uid)

    def test_one_r_message_has_button(self):
        self.sc.monitor(FRAME)
        text, buttons = [m for m in self.sc.tg.sent if "۱ برابر ریسک" in m[0]][-1]
        self.assertEqual(buttons[-1][0]["callback_data"], "z|5")
        self.assertEqual(buttons[0][0]["callback_data"], "c|5")             # بستن/نگه دار مثل قبل

    def test_lock_sets_breakeven_and_2r_on_broker(self):
        self.lock()
        self.sc.client.modify_sltp.assert_called_once_with(5, sl=ENTRY, tp=TP2R)
        info = self.sc.open[5]
        self.assertEqual((info["hold"], info["locked"], info["xsl"], info["xtp"]), (True, True, ENTRY, TP2R))
        self.assertIn("بی‌ضرر شد", self.last())
        self.assertIn("+12.00$", self.last())
        self.assertIn("lock_profit", self.events())
        saved = trk.load_state(self.sc.state_path)[5]                     # بعد از ری‌استارت هم
        self.assertEqual((saved["locked"], saved["xsl"], saved["xtp"]), (True, ENTRY, TP2R))
        self.assertEqual(self.sc.open_risk_usd(), 0.0)                     # دیگر ریسکی در سقف حساب نمی‌شود

    def test_below_entry_changes_nothing(self):
        self.price(4116.5)
        self.lock()
        self.sc.client.modify_sltp.assert_not_called()
        self.assertFalse(self.sc.open[5]["hold"])
        self.assertIn("نمی‌شود بی‌ضرر کرد", self.last())

    def test_past_target_offers_close(self):
        self.price(4130.0)
        self.lock()
        self.sc.client.modify_sltp.assert_not_called()
        self.assertIn("از هدف 2R", self.last())
        self.assertEqual(self.sc.tg.sent[-1][1][0][0]["callback_data"], "c|5")

    def test_broker_reject_restores_everything(self):
        self.sc.client.modify_sltp.return_value = NS(ok=False, comment="Invalid stops")
        self.lock()
        info = self.sc.open[5]
        self.assertEqual((info["hold"], info["xsl"], info["xtp"]), (False, None, None))
        self.assertFalse(info.get("locked"))
        self.assertIn("بروکر قبول نکرد", self.last())
        self.assertFalse(trk.load_state(self.sc.state_path)[5]["hold"])

    def test_sell_side(self):
        self.sc.open[5] = dict(held(), side="SELL", entry=4100.0, sl=4105.0, risk=5.0, emerg=4115.0, hold=False)
        self.sc.client.get_tick.return_value = NS(time=T0, bid=4093.7, ask=4094.0)
        self.lock()
        self.sc.client.modify_sltp.assert_called_once_with(5, sl=4100.0, tp=4090.0)

    def test_breakeven_close_is_reported_and_card_label(self):
        self.lock()
        self.sc.client.positions.return_value = []
        self.sc.client.deals_for_position.return_value = [
            NS(entry=0, price=ENTRY, profit=0.0, time=T0), NS(entry=1, price=ENTRY, profit=-0.05, time=T0 + 900)]
        self.sc.monitor(FRAME)
        self.assertIn("سر به سر بسته شد (بی‌ضرر)", self.last())
        cap = fu.trade_card_caption(5, "BUY", "x", ENTRY, ENTRY, -0.05, None, 900, "lock_sl", None, date(2026, 10, 8))
        self.assertIn("🔒 سر به سر (بی‌ضرر)", cap)

    def test_target_close_is_reported(self):
        self.lock()
        self.sc.client.positions.return_value = []
        self.sc.client.deals_for_position.return_value = [
            NS(entry=0, price=ENTRY, profit=0.0, time=T0), NS(entry=1, price=TP2R, profit=12.0, time=T0 + 900)]
        self.sc.monitor(FRAME)
        self.assertIn("به هدف 2R (4129.18) رسید", self.last())

    def test_unhold_clears_lock_and_non_admin_ignored(self):
        self.lock(uid=99)
        self.sc.client.modify_sltp.assert_not_called()
        self.lock()
        self.tap("u|5")
        self.assertFalse(self.sc.open[5]["locked"])
        self.assertIsNone(self.sc.open[5]["xsl"])


if __name__ == "__main__":
    unittest.main()
