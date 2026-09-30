"""Scout soft stop + live status: signal SL only warns; closing is always the owner's call."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pandas as pd

from tools import scout_tracker as trk
from tools.scout_bot import Scout


class Pure(unittest.TestCase):
    def test_emergency_sl(self):
        self.assertEqual(trk.emergency_sl("BUY", 4175.69, 4169.65, 3.0), 4157.57)
        self.assertEqual(trk.emergency_sl("SELL", 4100.0, 4110.0, 3.0), 4130.0)
        self.assertEqual(trk.emergency_sl("BUY", 100.0, 99.0, 0.5), 99.0)   # never tighter than signal SL

    def test_beyond_and_recovered(self):
        self.assertTrue(trk.beyond_soft_sl("BUY", 99.0, 99.0))
        self.assertFalse(trk.beyond_soft_sl("BUY", 99.01, 99.0))
        self.assertTrue(trk.beyond_soft_sl("SELL", 101.0, 101.0))
        self.assertFalse(trk.recovered("BUY", 99.2, 99.0, 1.0))
        self.assertTrue(trk.recovered("BUY", 99.25, 99.0, 1.0))
        self.assertTrue(trk.recovered("SELL", 100.75, 101.0, 1.0))

    def test_status_text(self):
        info = {"side": "BUY", "entry": 4175.69, "sl": 4169.65, "risk": 6.04, "setup": "kijun_pullback",
                "emerg": 4157.57}
        t = trk.status_text(111558632, info, 4178.71, 3.02, -0.12, 5700, 1759300000)
        for part in ("#111558632", "4175.69", "4178.71", "+3.02$", "+0.50R", "سواپ -0.12$",
                     "4169.65", "4157.57", "1:35", "✅ سالم"):
            self.assertIn(part, t)
        t2 = trk.status_text(1, dict(info, hold=True), 4160.0, -15.69, 0.0, None, None)
        self.assertIn("نگه داشته‌اید", t2)
        self.assertIn("-15.69$", t2)

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            trk.save_state(p, {7: {"setup": "x", "side": "BUY", "entry": 1.0, "sl": 0.5, "risk": 0.5,
                                   "hold": True, "status_mid": 42, "junk": object()}})
            back = trk.load_state(p)
            self.assertEqual(back[7]["status_mid"], 42)
            self.assertTrue(back[7]["hold"])
            self.assertNotIn("junk", back[7])
            self.assertEqual(trk.load_state(Path(d) / "missing.json"), {})


class FakeTG:
    def __init__(self):
        self.sent, self.edits, self.acks = [], [], []
        self._mid = 100

    def send(self, text, buttons=None):
        self._mid += 1
        self.sent.append((text, buttons))
        return self._mid

    def edit(self, mid, text, buttons=None):
        self.edits.append((mid, text, buttons))
        return True

    def ack(self, cb, text=""):
        self.acks.append(text)


def make_scout(tmp: Path) -> Scout:
    sc = Scout.__new__(Scout)
    sc.tg = FakeTG()
    sc.client = Mock()
    sc.client.get_tick.return_value = NS(time=1_759_300_000, bid=4170.0, ask=4170.3)
    sc.client.modify_sltp.return_value = NS(ok=True, comment="")
    sc.client.deals_for_position.return_value = []
    sc.open, sc.pending = {}, {}
    sc.dry = False
    sc.soft_stop, sc.emerg_mult = True, 3.0
    sc.status_every, sc.remind_every = 300.0, 300.0
    sc.state_path = tmp / "scout_state.json"
    sc.journal = tmp / "journal.csv"
    sc.lot = 0.01
    return sc


def pos(tk=5, price=4178.0, profit=2.3, sl=4157.57, ptype=0, open_=4175.69):
    return NS(ticket=tk, type=ptype, price_open=open_, price_current=price, profit=profit, swap=0.0,
              sl=sl, time=1_759_300_000 - 3600, comment="scout_kp", magic=735777)


FRAME = pd.DataFrame({"close": [4180.0], "tenkan": [4170.0]})      # structure intact for BUY


class Behaviour(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tmp = Path(self._d.name)
        self.sc = make_scout(self.tmp)
        self.sc.open[5] = {"setup": "kijun_pullback", "side": "BUY", "entry": 4175.69, "sl": 4169.65,
                           "risk": 6.04, "r1": False, "warned": False, "emerg": 4157.57,
                           "status_mid": None, "below": False, "hold": False, "alert": "1"}

    def tearDown(self):
        self._d.cleanup()

    def run_at(self, t, price, profit=-1.0):
        self.sc.client.positions.return_value = [pos(price=price, profit=profit)]
        with unittest.mock.patch("tools.scout_bot.time.time", return_value=t):
            self.sc.monitor(FRAME)

    def test_status_sent_then_edited_every_5_min(self):
        self.run_at(1000.0, 4178.0)
        self.assertEqual(len(self.sc.tg.sent), 1)
        self.assertIn("📊", self.sc.tg.sent[0][0])
        mid = self.sc.open[5]["status_mid"]
        self.run_at(1200.0, 4179.0)                     # < 5 min: nothing
        self.assertEqual(self.sc.tg.edits, [])
        self.run_at(1301.0, 4179.0)                     # >= 5 min: edit the same message
        self.assertEqual(self.sc.tg.edits[-1][0], mid)
        self.assertEqual(len(self.sc.tg.sent), 1)

    def test_soft_stop_never_closes_and_reminds(self):
        self.run_at(1000.0, 4169.0)                     # crosses the signal SL
        alerts = [s for s in self.sc.tg.sent if "حد ضرر سیگنال رسید" in s[0]]
        self.assertEqual(len(alerts), 1)
        self.assertEqual([b["callback_data"] for b in alerts[0][1][0]], ["c|5", "h|5"])
        self.run_at(1100.0, 4168.0)                     # no spam inside the reminder window
        self.run_at(1301.0, 4168.0)                     # reminder after 5 min
        self.assertEqual(sum("⏰" in s[0] for s in self.sc.tg.sent), 1)
        self.sc.hold(5)                                 # owner: keep it
        self.run_at(1700.0, 4165.0)
        self.run_at(2100.0, 4160.0)
        self.assertEqual(sum("⏰" in s[0] for s in self.sc.tg.sent), 1)
        self.sc.client.close_position.assert_not_called()
        self.sc.client.modify_sltp.assert_not_called()
        self.assertIn(5, self.sc.open)

    def test_recovery_rearms_alarm(self):
        self.run_at(1000.0, 4169.0)
        self.sc.hold(5)
        self.run_at(1010.0, 4171.5)                     # back above SL + 0.25R -> re-armed
        self.assertFalse(self.sc.open[5]["below"])
        self.assertFalse(self.sc.open[5]["hold"])
        self.run_at(1020.0, 4169.5)                     # crosses again -> new alert
        self.assertEqual(sum("حد ضرر سیگنال رسید" in s[0] for s in self.sc.tg.sent), 2)

    def test_state_persisted_and_restored(self):
        self.run_at(1000.0, 4169.0)
        self.sc.hold(5)
        sc2 = make_scout(self.tmp)
        sc2.client.positions.return_value = [pos()]
        sc2.restore()
        self.assertTrue(sc2.open[5]["hold"])
        self.assertTrue(sc2.open[5]["below"])
        self.assertEqual(sc2.open[5]["status_mid"], self.sc.open[5]["status_mid"])
        sc2.client.modify_sltp.assert_not_called()      # already tracked -> broker SL untouched

    def test_restore_adopts_untracked_position_with_emergency_sl(self):
        sc = make_scout(self.tmp)
        sc.client.positions.return_value = [pos(tk=111558632, sl=4169.65)]
        sc.restore()
        sc.client.modify_sltp.assert_called_once_with(111558632, sl=4157.57)
        info = sc.open[111558632]
        self.assertEqual(info["sl"], 4169.65)           # signal SL becomes the soft stop
        self.assertEqual(info["emerg"], 4157.57)
        self.assertTrue(info["adopted"])

    def test_emergency_close_reported(self):
        self.sc.client.positions.return_value = []
        self.sc.realized = Mock(return_value=(-18.2, 4157.5))
        with unittest.mock.patch("tools.scout_bot.time.time", return_value=5000.0):
            self.sc.monitor(FRAME)
        self.assertNotIn(5, self.sc.open)
        self.assertIn("اضطراری", self.sc.tg.sent[-1][0])


class Decide(unittest.TestCase):
    def test_order_uses_emergency_sl_and_keeps_signal_sl_soft(self):
        with tempfile.TemporaryDirectory() as d:
            sc = make_scout(Path(d))
            sc.block_hedge = True
            sc.client.get_tick.return_value = NS(time=0, bid=4175.4, ask=4175.69)
            sc.client.send_market_order.return_value = NS(ok=True, position=9, order=9, price=4175.69,
                                                          comment="")
            row = pd.Series({"setup": "kijun_pullback", "side": "BUY", "ref_close": 4175.5})
            sc.pending["1"] = {"row": row, "sl": 4169.46, "mid": 1, "txt": "t", "t": 0}
            sc.decide("1", True, "cb")
            kw = sc.client.send_market_order.call_args.kwargs
            self.assertEqual(kw["sl"], trk.emergency_sl("BUY", 4175.69, 4175.69 - 6.04, 3.0))
            self.assertEqual(kw["comment"], "scout_kp")
            self.assertAlmostEqual(sc.open[9]["sl"], 4169.65, places=2)
            self.assertTrue((Path(d) / "scout_state.json").exists())


if __name__ == "__main__":
    unittest.main()
