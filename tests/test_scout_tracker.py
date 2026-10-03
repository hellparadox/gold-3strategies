"""Scout stop policy: capped signal SL; never past it unless «hold»; away modes close/hold/ai."""
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pandas as pd

from tools import scout_ai
from tools import scout_tracker as trk
from tools.scout_bot import Scout

ENTRY, SOFT = 4175.69, 4169.65          # BUY, risk 6.04 (< $10 cap)


class Pure(unittest.TestCase):
    def test_emergency_sl_with_cap(self):
        self.assertEqual(trk.emergency_sl("BUY", ENTRY, SOFT, 3.0), 4157.57)
        self.assertEqual(trk.emergency_sl("BUY", ENTRY, SOFT, 3.0, cap_dist=20.0), 4157.57)   # 18.12 < 20
        self.assertEqual(trk.emergency_sl("SELL", 4171.23, 4187.06, 3.0, cap_dist=20.0), 4191.23)
        self.assertEqual(trk.emergency_sl("BUY", 100.0, 99.0, 0.5), 99.0)   # never tighter than signal SL

    def test_capped_sl(self):
        self.assertEqual(trk.capped_sl("SELL", 4171.23, 4187.06, 10.0), 4181.23)
        self.assertEqual(trk.capped_sl("BUY", ENTRY, SOFT, 10.0), SOFT)

    def test_policy_and_desired_sl(self):
        info = {"sl": SOFT, "emerg": 4157.57, "hold": False}
        self.assertEqual(trk.desired_broker_sl(info, "close"), SOFT)
        self.assertEqual(trk.desired_broker_sl(info, "ai"), 4157.57)
        self.assertEqual(trk.desired_broker_sl(info, "hold"), 4157.57)
        self.assertEqual(trk.desired_broker_sl(dict(info, hold=True), "close"), 4157.57)
        self.assertEqual(trk.effective_policy(info, "bogus"), "close")

    def test_adverse_fraction(self):
        self.assertAlmostEqual(trk.adverse_fraction("BUY", 100.0, 99.3, 1.0), 0.7)
        self.assertAlmostEqual(trk.adverse_fraction("SELL", 100.0, 100.5, 1.0), 0.5)

    def test_status_text_shows_policy(self):
        info = {"side": "BUY", "entry": ENTRY, "sl": SOFT, "risk": 6.04, "setup": "kijun_pullback",
                "emerg": 4157.57}
        t = trk.status_text(1, info, 4178.71, 3.02, -0.12, 5700, 1759300000, "close")
        for part in ("4178.71", "+3.02$", "+0.50R", "سواپ -0.12$", "1:35", "در حد ضرر بسته شود"):
            self.assertIn(part, t)
        self.assertIn("نگه دار (دستی)", trk.status_text(1, dict(info, hold=True), 4170.0, -5, 0, None, None, "close"))

    def test_mode_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            self.assertEqual(trk.load_mode(p), "close")
            trk.save_mode(p, "ai")
            self.assertEqual(trk.load_mode(p), "ai")
            with self.assertRaises(ValueError):
                trk.save_mode(p, "x")

    def test_ai_parse_is_fail_safe(self):
        self.assertTrue(scout_ai.parse('ok {"decision":"HOLD","confidence":75,"reason":"r"}').hold)
        for bad in ("", "no json", '{"decision":"MAYBE"}', "{oops"):
            self.assertEqual(scout_ai.parse(bad).decision, "CLOSE")


class FakeTG:
    def __init__(self):
        self.sent, self.edits, self.acks, self.deleted, self.keyboards = [], [], [], [], []
        self._mid = 100
        self.last_mid = None

    def send(self, text, buttons=None, keyboard=None, reply_to=None):
        self._mid += 1
        self.sent.append((text, buttons))
        self.replies = getattr(self, "replies", []) + [reply_to]
        if keyboard:
            self.keyboards.append(keyboard)
        self.last_mid = self._mid
        return self._mid

    def delete(self, mid):
        self.deleted.append(mid)

    def edit(self, mid, text, buttons=None):
        self.edits.append((mid, text, buttons))
        return True

    def ack(self, cb, text=""):
        self.acks.append(text)


def make_scout(tmp: Path, mode="close") -> Scout:
    sc = Scout.__new__(Scout)
    sc.tg = FakeTG()
    sc.client = Mock()
    sc.client.get_tick.return_value = NS(time=1_759_300_000, bid=4170.0, ask=4170.3)
    sc.client.modify_sltp.return_value = NS(ok=True, comment="")
    sc.client.close_position.return_value = NS(ok=True, price=4169.6, comment="")
    sc.client.deals_for_position.return_value = []
    sc.open, sc.pending = {}, {}
    sc.dry = False
    sc.soft_stop, sc.emerg_mult = True, 3.0
    sc.sl_cap, sc.emerg_cap = 10.0, 20.0
    sc.approach_frac = 0.7
    sc.status_every = 300.0
    sc.state_path = tmp / "scout_state.json"
    sc.mode_path = tmp / "scout_mode.json"
    sc.mode = mode
    sc.ai_recheck, sc.ai_min_conf, sc.ai_timeout = 900.0, 60, 60.0
    sc._ai, sc._ai_pool, sc._ai_futs, sc._sl_fail_t = None, None, {}, {}
    sc.journal = tmp / "journal.csv"
    sc.lot = 0.01
    sc.block_hedge = True
    sc.alert_while_open, sc.max_open, sc.max_total_risk = True, 2, 20.0
    sc.expiry, sc.n = 1800, 0
    sc._last_alert_t = None
    sc._an_pool, sc._an_futs, sc._an_cache, sc._frame, sc._closing_t = None, {}, {}, None, {}
    sc.market_open = lambda: True
    sc._live, sc._an_short, sc.dashboard = {}, {}, None
    sc._virtual_rows, sc._actual_map = None, {}
    return sc


def pos(tk=5, price=4178.0, profit=2.3, sl=SOFT, ptype=0, open_=ENTRY):
    return NS(ticket=tk, type=ptype, price_open=open_, price_current=price, profit=profit, swap=0.0,
              sl=sl, time=1_759_300_000 - 3600, comment="scout_kp", magic=735777)


FRAME = pd.DataFrame({"close": [4180.0], "tenkan": [4170.0]})      # structure intact for BUY


class Behaviour(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tmp = Path(self._d.name)
        self.sc = make_scout(self.tmp)
        self.sc.open[5] = {"setup": "kijun_pullback", "side": "BUY", "entry": ENTRY, "sl": SOFT,
                           "risk": 6.04, "r1": False, "warned": False, "emerg": 4157.57,
                           "status_mid": None, "below": False, "hold": False, "alert": "1",
                           "approach": False, "ai_next": 0.0, "ai_note": ""}

    def tearDown(self):
        if self.sc._ai_pool:
            self.sc._ai_pool.shutdown(wait=True)
        self._d.cleanup()

    def run_at(self, t, price, profit=-1.0, broker_sl=SOFT):
        self.sc.client.positions.return_value = [pos(price=price, profit=profit, sl=broker_sl)]
        with unittest.mock.patch("tools.scout_bot.time.time", return_value=t):
            self.sc.monitor(FRAME)

    def test_status_sent_then_edited_every_5_min(self):
        self.run_at(1000.0, 4178.0)
        self.assertEqual(sum("📊" in s[0] for s in self.sc.tg.sent), 1)
        mid = self.sc.open[5]["status_mid"]
        self.run_at(1200.0, 4179.0)
        self.assertEqual(self.sc.tg.edits, [])
        self.run_at(1301.0, 4179.0)
        self.assertEqual(self.sc.tg.edits[-1][0], mid)

    def test_status_moves_to_bottom_when_buried(self):
        self.run_at(1000.0, 4178.0)
        old = self.sc.open[5]["status_mid"]
        self.sc.tg.send("another message")              # status is no longer the last message
        self.run_at(1301.0, 4179.0)
        new = self.sc.open[5]["status_mid"]
        self.assertNotEqual(new, old)
        self.assertEqual(self.sc.tg.deleted, [old])
        self.assertEqual(self.sc.tg.edits, [])
        texts = [t for t, b in self.sc.tg.sent if "📊" in t]
        self.assertEqual(len(texts), 2)
        self.assertEqual([b["callback_data"] for b in self.sc.tg.sent[-1][1][0]], ["c|5", "h|5"])

    def test_close_mode_never_passes_the_stop(self):
        self.run_at(1000.0, 4169.5)                     # beyond the signal SL, no hold
        self.sc.client.close_position.assert_called_once()
        self.sc.client.modify_sltp.assert_not_called()

    def test_approach_alert_once_then_hold_moves_broker_sl_out(self):
        self.run_at(1000.0, 4171.4)                     # 71% of risk lost
        self.run_at(1010.0, 4171.3)
        self.assertEqual(sum("نزدیک حد ضرر" in s[0] for s in self.sc.tg.sent), 1)
        self.sc.hold(5)
        self.run_at(1020.0, 4169.0, broker_sl=SOFT)     # beyond, but held
        self.sc.client.close_position.assert_not_called()
        self.sc.client.modify_sltp.assert_called_with(5, sl=4157.57)
        for t in (1030.0, 1400.0, 1800.0):              # no repeated stop alerts while held
            self.run_at(t, 4168.0, broker_sl=4157.57)
        self.assertFalse(any("حد ضرر سیگنال رسید" in s[0] or "⏰" in s[0] for s in self.sc.tg.sent))
        self.assertEqual(self.sc.client.modify_sltp.call_count, 1)
        self.sc.hold(5, on=False)                       # owner cancels hold while beyond -> close
        self.run_at(1900.0, 4168.0, broker_sl=4157.57)
        self.sc.client.close_position.assert_called_once()

    def test_hold_mode_keeps_and_emergency_breach_closes(self):
        self.sc.mode = "hold"
        self.run_at(1000.0, 4165.0, broker_sl=SOFT)
        self.sc.client.modify_sltp.assert_called_once_with(5, sl=4157.57)
        self.sc.client.close_position.assert_not_called()
        self.run_at(2000.0, 4157.0, broker_sl=SOFT)     # broker SL lagging and emergency already passed
        self.sc.client.close_position.assert_called_once()

    def _ai_run(self, verdict):
        self.sc.mode = "ai"
        self.sc._ai = Mock()
        self.sc._ai.decide.return_value = verdict
        self.run_at(1000.0, 4169.0, broker_sl=4157.57)  # reaches SL -> asks
        self.sc._ai_pool.shutdown(wait=True)
        self.run_at(1002.0, 4169.0, broker_sl=4157.57)  # collects the verdict

    def test_ai_hold(self):
        self._ai_run(scout_ai.AIVerdict("HOLD", 80, "روند ساعتی هنوز صعودی است", "m"))
        self.sc.client.close_position.assert_not_called()
        self.assertTrue(any("هوش مصنوعی نگه داشت" in s[0] for s in self.sc.tg.sent))
        self.assertGreater(self.sc.open[5]["ai_next"], 1500)

    def test_ai_low_confidence_or_error_closes(self):
        self._ai_run(scout_ai.AIVerdict("HOLD", 40, "مطمئن نیستم", "m"))
        self.sc.client.close_position.assert_called_once()

    def test_ai_error_closes(self):
        self._ai_run(scout_ai.AIVerdict("CLOSE", 0, "", "", "HTTP 503"))
        self.sc.client.close_position.assert_called_once()
        self.assertTrue(any("جواب نداد" in s[0] for s in self.sc.tg.sent))

    def test_mode_switch_persisted_and_buttons(self):
        self.sc.set_mode("ai")
        self.assertEqual(trk.load_mode(self.sc.mode_path), "ai")
        self.assertEqual([b["callback_data"] for b in self.sc._close_buttons(5)[0]], ["c|5", "h|5"])
        self.sc.hold(5)
        self.assertEqual([b["callback_data"] for b in self.sc._close_buttons(5)[0]], ["c|5", "u|5"])

    def test_restore_applies_caps_to_old_positions(self):
        trk.save_state(self.sc.state_path, {7: {"setup": "tk_cross", "side": "SELL", "entry": 4171.23,
                                                "sl": 4187.06, "emerg": 4218.57, "risk": 15.83,
                                                "hold": True}})
        sc2 = make_scout(self.tmp)
        sc2.client.positions.return_value = [pos(tk=7, ptype=1, open_=4171.23, sl=4218.57)]
        sc2.restore()
        info = sc2.open[7]
        self.assertEqual((info["sl"], info["emerg"]), (4181.23, 4191.23))
        self.assertTrue(info["hold"])
        self.assertEqual(info["status_t"], 0.0)          # fresh status message with buttons right away

    def test_restore_adopts_untracked_position(self):
        sc = make_scout(self.tmp)
        sc.client.positions.return_value = [pos(tk=9, sl=SOFT)]
        sc.restore()
        self.assertEqual((sc.open[9]["sl"], sc.open[9]["emerg"]), (SOFT, 4157.57))
        self.assertTrue(sc.open[9]["adopted"])

    def test_close_button_on_gone_position_is_quiet(self):
        self.sc.client.positions.return_value = []
        self.sc.close(999, cb="cb")
        self.sc.client.close_position.assert_not_called()
        self.assertIn("قبلاً بسته شده", self.sc.tg.acks[-1])

    def test_stop_close_reported(self):
        self.sc.client.positions.return_value = []
        self.sc.realized = Mock(return_value=(-6.1, 4169.6))
        with unittest.mock.patch("tools.scout_bot.time.time", return_value=5000.0):
            self.sc.monitor(FRAME)
        self.assertIn("حد ضرر سیگنال بسته شد", self.sc.tg.sent[-1][0])


class Decide(unittest.TestCase):
    def _decide(self, mode, sl_dist=6.04):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sc = make_scout(Path(d.name), mode)
        sc.block_hedge = True
        sc.client.get_tick.return_value = NS(time=0, bid=4175.4, ask=4175.69)
        sc.client.send_market_order.return_value = NS(ok=True, position=9, order=9, price=4175.69,
                                                      comment="")
        row = pd.Series({"setup": "kijun_pullback", "side": "BUY", "ref_close": 4175.5})
        sc.pending["1"] = {"row": row, "sl": 4175.5 - sl_dist, "mid": 1, "txt": "t", "t": 0}
        sc.decide("1", True, "cb")
        return sc, sc.client.send_market_order.call_args.kwargs

    def test_close_mode_order_carries_signal_sl(self):
        sc, kw = self._decide("close")
        self.assertAlmostEqual(kw["sl"], 4169.65, places=2)
        self.assertEqual(kw["comment"], "scout_kp")
        self.assertAlmostEqual(sc.open[9]["emerg"], 4157.57, places=2)

    def test_ai_mode_order_carries_emergency_sl(self):
        sc, kw = self._decide("ai")
        self.assertAlmostEqual(kw["sl"], 4157.57, places=2)

    def test_signal_sl_cap_on_alert_rows(self):
        sc = make_scout(Path(tempfile.mkdtemp()))
        r = sc.cap_row(pd.Series({"sl_dist": 15.83, "tp_dist": 31.66}))
        self.assertEqual((r.sl_dist, r.tp_dist, r.sl_dist_orig), (10.0, 20.0, 15.83))
        self.assertEqual(sc.cap_row(pd.Series({"sl_dist": 6.0, "tp_dist": 12.0})).sl_dist, 6.0)


if __name__ == "__main__":
    unittest.main()
