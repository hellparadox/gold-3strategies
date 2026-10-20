"""یک بات ادمین (مثل Alpha_muse_bot / Muse) مثل خود مالک معامله کند: /buy /sell بدون کارت تأیید (بات دکمه نمی‌زند)،
از همان مسیر decide (سقف‌ها، ژورنال، «چه کسی»)، و جواب‌ها ریپلای به پیام بات. آدم‌ها مثل قبل کارت تأیید می‌گیرند."""
import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from tests.test_scout_tracker import make_scout
from tools.scout_bot import TG

BOT_ID, HUMAN_ID = 555, 1


class BotTrader(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = make_scout(Path(self._d.name))
        self.sc.admins = {HUMAN_ID, BOT_ID}
        self.sc.client.get_tick.return_value = NS(time=0, bid=4099.8, ask=4100.1)
        self.sc.client.spread_points.return_value = 30
        self.sc.client.positions.return_value = []
        self.sc.client.send_market_order.return_value = NS(ok=True, position=77, order=77, deal=0,
                                                           price=4100.1, comment="")
        self.sc.tg.default_reply = None

    def tearDown(self):
        self._d.cleanup()

    def bot(self, text, uid=BOT_ID, mid=900):
        self.sc.handle_update({"message": {"message_id": mid, "text": text,
                                           "from": {"id": uid, "is_bot": True, "first_name": "Alpha Muse"}}})

    def human(self, text):
        self.sc.handle_update({"message": {"message_id": 5, "text": text,
                                           "from": {"id": HUMAN_ID, "first_name": "Hamid"}}})

    def events(self):
        with self.sc.journal.open(encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def last(self):
        return self.sc.tg.sent[-1][0]

    def test_bot_buy_opens_without_card_tap(self):
        self.bot("/buy@HellTreid_bot")
        args = self.sc.client.send_market_order.call_args
        self.assertEqual((args.args[0], args.args[1]), ("BUY", 0.01))
        info = self.sc.open[77]
        self.assertEqual((info["side"], info["setup"], info["by"]), ("BUY", "manual", "Alpha Muse"))
        self.assertAlmostEqual(info["sl"], 4100.1 - 10.0, places=2)             # همان سقف تنظیمات
        self.assertIn("✅ BUY باز شد #77", [m[0] for m in self.sc.tg.sent if "باز شد #77" in m[0] and m[0].startswith("✅")][0])
        self.assertEqual(self.sc.pending, {})
        opened = [e for e in self.events() if e["event"] == "opened"][-1]
        self.assertIn("by=Alpha Muse", opened["note"])

    def test_bot_sell(self):
        self.bot("/sell")
        self.assertEqual(self.sc.client.send_market_order.call_args.args[0], "SELL")

    def test_human_still_gets_card_only(self):
        self.human("/buy")
        self.sc.client.send_market_order.assert_not_called()
        self.assertEqual(len(self.sc.pending), 1)
        self.assertIn("✅ تأیید", str(self.sc.tg.sent[-1][1]))

    def test_unlisted_bot_is_ignored(self):
        self.bot("/buy", uid=999)
        self.sc.client.send_market_order.assert_not_called()
        self.assertEqual(self.sc.tg.sent, [])

    def test_opposite_position_blocks_and_explains(self):
        self.sc.open[5] = {"setup": "x", "side": "SELL", "entry": 4105.0, "sl": 4111.0, "risk": 6.0,
                           "lot": 0.01, "emerg": None, "hold": False}
        self.bot("/buy")
        self.sc.client.send_market_order.assert_not_called()
        self.assertIn("⛔ BUY باز نشد", self.last())
        self.assertEqual(self.sc.pending, {})
        self.assertIn("blocked_hedge", [e["event"] for e in self.events()])

    def test_max_open_blocks(self):
        for tk in (5, 6):
            self.sc.open[tk] = {"setup": "x", "side": "BUY", "entry": 4100.0, "sl": 4094.0, "risk": 6.0,
                                "lot": 0.01, "emerg": None, "hold": False}
        self.bot("/buy")
        self.sc.client.send_market_order.assert_not_called()
        self.assertIn("باز نشد", self.last())
        self.assertIn("blocked_limit", [e["event"] for e in self.events()])

    def test_market_closed_and_no_tick(self):
        self.sc.market_open = lambda: False
        self.bot("/buy")
        self.assertIn("بازار بسته", self.last())
        self.sc.market_open = lambda: True
        self.sc.client.get_tick.return_value = None
        self.bot("/sell")
        self.assertIn("قیمت از MT5 نرسید", self.last())
        self.sc.client.send_market_order.assert_not_called()

    def test_broker_reject_reports_failure(self):
        self.sc.client.send_market_order.return_value = NS(ok=False, comment="No money", price=0.0)
        self.bot("/buy")
        self.assertEqual(self.sc.open, {})
        self.assertIn("باز نشد", self.last())

    def test_dry_mode(self):
        self.sc.dry = True
        self.bot("/buy")
        self.sc.client.send_market_order.assert_not_called()
        self.assertIn("آزمایشی", self.last())

    def test_bot_can_close(self):
        self.bot("/buy")
        self.sc.client.close_position.return_value = NS(ok=True, price=4101.0, comment="")
        self.bot("/close 77")
        self.sc.client.close_position.assert_called_once()
        self.assertIn("بستن #77", self.last())
        self.assertEqual(self.sc.open[77]["cby"], "Alpha Muse")

    def test_reply_context_is_set_only_while_handling_bot_command(self):
        seen = []
        orig = self.sc.manual_start

        def spy(*a, **k):
            seen.append(self.sc.tg.default_reply)
            return orig(*a, **k)
        with mock.patch.object(self.sc, "manual_start", side_effect=spy):
            self.bot("/buy", mid=900)
            self.human("/buy")
        self.assertEqual(seen, [900, None])
        self.assertIsNone(self.sc.tg.default_reply)


class RealTG(unittest.TestCase):
    def tg(self):
        t = TG("123:abc", -100)
        t._call = mock.Mock(return_value={"message_id": 7})
        return t

    def test_replies_go_to_the_bot_message(self):
        t = self.tg()
        t.default_reply = 900
        t.send("hi")
        self.assertEqual(t._call.call_args.kwargs["reply_parameters"]["message_id"], 900)
        t.default_reply = None
        t.send("again")
        self.assertNotIn("reply_parameters", t._call.call_args.kwargs)

    def test_explicit_reply_wins_and_ack_without_id_is_noop(self):
        t = self.tg()
        t.default_reply = 900
        t.send("x", reply_to=5)
        self.assertEqual(t._call.call_args.kwargs["reply_parameters"]["message_id"], 5)
        t._call.reset_mock()
        t.ack("", "x")
        t.ack(None, "x")
        t._call.assert_not_called()
        t.ack("cb1", "ok")
        t._call.assert_called_once()


if __name__ == "__main__":
    unittest.main()
