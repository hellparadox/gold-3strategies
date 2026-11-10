"""نگهبان معاملات ربات (آلفا/Muse): حد ضرر سیگنال همیشه اجرا می‌شود (حتی در حالت «نگه دار»)، ربات نمی‌تواند
پوزیشن خودش را «نگه دار» کند، و بعد از سقف ضرر روزانه تا فردا معاملهٔ تازه باز نمی‌کند. آدم‌ها مثل قبل."""
import csv
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

from tests.test_scout_tracker import make_scout
from tools import scout_tracker as trk

BOT_ID, HUMAN_ID = 555, 1


class Guard(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = make_scout(Path(self._d.name), mode="hold")
        self.sc.admins = {HUMAN_ID, BOT_ID}
        self.sc.client.get_tick.return_value = NS(time=0, bid=4099.8, ask=4100.1)
        self.sc.client.spread_points.return_value = 30
        self.sc.client.positions.return_value = []
        self.sc.client.send_market_order.return_value = NS(ok=True, position=77, order=77, deal=0,
                                                           price=4100.1, comment="")
        self.sc.tg.default_reply = None

    def tearDown(self):
        self._d.cleanup()

    def bot(self, text, mid=900):
        self.sc.handle_update({"message": {"message_id": mid, "text": text,
                                           "from": {"id": BOT_ID, "is_bot": True, "first_name": "Alpha"}}})

    def human(self, text):
        self.sc.handle_update({"message": {"message_id": 5, "text": text,
                                           "from": {"id": HUMAN_ID, "first_name": "Hamid"}}})

    def events(self):
        if not self.sc.journal.exists():
            return []
        with self.sc.journal.open(encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def loss_today(self, usd, name="Alpha", days_ago=0):
        ts = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
        self.sc.log(ts=ts, event="gone", ticket=str(900 + len(self.events())), side="BUY",
                    profit=usd, note=f"stop;how=stop;by={name};cby=")

    # ── حد ضرر سیگنال برای ربات ──
    def test_bot_trade_sends_signal_stop_to_broker_even_in_hold_mode(self):
        self.bot("/buy")
        sl_sent = self.sc.client.send_market_order.call_args.kwargs["sl"]
        info = self.sc.open[77]
        self.assertTrue(info["bot"])
        self.assertAlmostEqual(sl_sent, info["sl"], places=2)               # ۱۰ دلار، نه اضطراری ۲۰
        self.assertAlmostEqual(info["sl"], 4100.1 - 10.0, places=2)
        self.assertEqual(trk.effective_policy(info, "hold"), "close")
        self.assertEqual(trk.effective_policy(info, "ai"), "close")
        self.assertAlmostEqual(trk.desired_broker_sl(info, "hold"), info["sl"], places=2)
        opened = [e for e in self.events() if e["event"] == "opened"][-1]
        self.assertIn("bot=1", opened["note"])
        self.assertIn("معاملهٔ ربات", "\n".join(m[0] for m in self.sc.tg.sent))

    def test_human_trade_unchanged_in_hold_mode(self):
        self.human("/buy")
        aid = next(iter(self.sc.pending))
        self.human(f"/yes {aid}")
        info = self.sc.open[77]
        self.assertFalse(info["bot"])
        sl_sent = self.sc.client.send_market_order.call_args.kwargs["sl"]
        self.assertAlmostEqual(sl_sent, info["emerg"], places=2)            # آدم: مثل قبل، سطح اضطراری
        self.assertEqual(trk.effective_policy(info, "hold"), "hold")

    def test_bot_flag_survives_restart(self):
        self.bot("/buy")
        back = trk.load_state(self.sc.state_path)
        self.assertTrue(back[77]["bot"])
        self.assertEqual(trk.effective_policy(back[77], "hold"), "close")

    def test_policy_rules(self):
        self.assertEqual(trk.effective_policy({"bot": True}, "hold"), "close")
        self.assertEqual(trk.effective_policy({"bot": True, "hold": True}, "close"), "hold")   # «نگه دار» آدم
        self.assertEqual(trk.effective_policy({}, "ai"), "ai")

    def test_force_off_by_setting(self):
        self.sc.bot_force_sl = False
        self.bot("/buy")
        self.assertFalse(self.sc.open[77]["bot"])

    # ── «نگه دار» ──
    def test_bot_cannot_hold_bot_trade_but_human_can(self):
        self.bot("/buy")
        self.bot("/hold 77")
        self.assertFalse(self.sc.open[77]["hold"])
        self.assertIn("فقط آدم‌ها", self.sc.tg.sent[-1][0])
        self.human("/hold 77")
        self.assertTrue(self.sc.open[77]["hold"])

    # ── سقف ضرر روزانه ──
    def test_daily_loss_blocks_new_bot_trades(self):
        self.loss_today(-9.0)
        self.loss_today(-7.5)
        self.bot("/sell")
        self.sc.client.send_market_order.assert_not_called()
        self.assertIn("سقف روزانهٔ ربات", self.sc.tg.sent[-1][0])
        self.assertIn("blocked_bot_loss", [e["event"] for e in self.events()])

    def test_under_limit_or_other_days_or_other_people_do_not_block(self):
        self.loss_today(-14.0)
        self.loss_today(-40.0, days_ago=2)
        self.loss_today(-50.0, name="Hamid")
        self.bot("/buy")
        self.sc.client.send_market_order.assert_called_once()

    def test_blocked_bot_yes_leaves_alert_for_humans(self):
        self.loss_today(-20.0)
        self.human("/buy")
        aid = next(iter(self.sc.pending))
        self.bot(f"/yes {aid}")
        self.sc.client.send_market_order.assert_not_called()
        self.assertIn(aid, self.sc.pending)
        self.human(f"/yes {aid}")
        self.sc.client.send_market_order.assert_called_once()

    def test_limit_zero_disables(self):
        self.sc.bot_daily_loss = 0
        self.loss_today(-100.0)
        self.bot("/buy")
        self.sc.client.send_market_order.assert_called_once()


    # ── سقف حد ضرر ربات مستقل از تنظیمات آدم‌ها ──
    def test_bot_stop_never_wider_than_bot_cap(self):
        self.sc.sl_cap = 20.0                                   # آدم‌ها سقف را ۲۰ دلار کرده‌اند
        self.bot("/sell")
        info = self.sc.open[77]
        self.assertAlmostEqual(info["sl"] - 4099.8, 10.0, places=2)   # ربات: باز هم ۱۰ دلار (فروش با bid)
        self.assertAlmostEqual(self.sc.client.send_market_order.call_args.kwargs["sl"], info["sl"], places=2)

    def test_human_keeps_own_cap(self):
        self.sc.sl_cap = 20.0
        self.human("/sell")
        self.human(f"/yes {next(iter(self.sc.pending))}")
        self.assertAlmostEqual(self.sc.open[77]["sl"] - 4099.8, 20.0, places=2)

    # ── دستورهای کهنه (وقتی اسکات خاموش بود فرستاده شده‌اند) ──
    def send_old(self, text, age, uid=BOT_ID, is_bot=True):
        import time as _t
        self.sc.handle_update({"message": {"message_id": 901, "text": text, "date": int(_t.time() - age),
                                           "from": {"id": uid, "is_bot": is_bot, "first_name": "Alpha"}}})

    def test_stale_trade_commands_are_dropped(self):
        self.send_old("/sell", age=4 * 3600)
        self.send_old("/buy", age=4 * 3600 - 5)
        self.sc.client.send_market_order.assert_not_called()
        self.assertEqual(sum("اجرا نشد" in m[0] for m in self.sc.tg.sent), 1)    # فقط یک خبر برای انبوه
        self.assertEqual([e["event"] for e in self.events()].count("stale_cmd"), 2)

    def test_fresh_and_read_only_commands_still_work(self):
        self.send_old("/sell", age=10)
        self.sc.client.send_market_order.assert_called_once()
        self.send_old("/cmds", age=4 * 3600)
        self.assertNotIn("stale_cmd", [e["event"] for e in self.events()])


if __name__ == "__main__":
    unittest.main()
