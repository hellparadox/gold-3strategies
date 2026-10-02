"""Alerts keep coming with an open position; up to max_open positions within max_total_risk_usd."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd

from tests.test_scout_tracker import make_scout


def row(side="BUY", ref=4175.5, sl_dist=6.0, setup="kijun_pullback"):
    return pd.Series({"setup": setup, "side": side, "ref_close": ref, "sl_dist": sl_dist,
                      "tp_dist": 2 * sl_dist, "atr": 5.0, "bar": pd.Timestamp("2026-10-02 15:15")})


class Multi(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = make_scout(Path(self._d.name))
        self.sc.client.get_tick.return_value = NS(time=0, bid=4175.4, ask=4175.69)
        self.sc.client.positions.return_value = []
        self.n_order = 0

        def order(side, lot, sl=None, tp=None, comment=""):
            self.n_order += 1
            return NS(ok=True, position=100 + self.n_order, order=100 + self.n_order,
                      price=4175.69 if side == "BUY" else 4175.4, comment="")
        self.sc.client.send_market_order.side_effect = order

    def tearDown(self):
        self._d.cleanup()

    def approve(self, r):
        self.sc.alert(r, 27.0)
        aid = str(self.sc.n)
        self.sc.decide(aid, True, "cb")
        return aid

    def test_alert_text_shows_open_position_and_second_opens(self):
        self.approve(row())
        self.assertEqual(len(self.sc.open), 1)
        self.sc.alert(row(), 27.0)
        text = self.sc.tg.sent[-1][0]
        self.assertIn("پوزیشن باز: #101 BUY", text)
        self.assertIn("پوزیشن دوم باز می‌شود", text)
        self.sc.decide(str(self.sc.n), True, "cb")
        self.assertEqual(len(self.sc.open), 2)

    def test_max_two_positions(self):
        self.approve(row(sl_dist=4.0))
        self.approve(row(sl_dist=4.0))
        self.approve(row(sl_dist=4.0))
        self.assertEqual(len(self.sc.open), 2)
        self.assertIn("سقف 2 پوزیشن", self.sc.tg.edits[-1][1])

    def test_total_risk_cap(self):
        self.approve(row(sl_dist=10.0))              # $10
        self.approve(row(sl_dist=10.0))              # $10 -> total $20 ok
        self.assertEqual(len(self.sc.open), 2)
        self.sc.max_open = 3
        self.approve(row(sl_dist=1.0))
        self.assertEqual(len(self.sc.open), 2)
        self.assertIn("سقف ریسک کل 20$", self.sc.tg.edits[-1][1])

    def test_held_position_counts_its_emergency_risk(self):
        self.approve(row(sl_dist=6.0))
        tk = next(iter(self.sc.open))
        self.sc.hold(tk)                             # broker stop -> emergency ($18.0)
        self.assertAlmostEqual(self.sc.open_risk_usd(), 18.0, places=1)
        self.approve(row(sl_dist=6.0))
        self.assertEqual(len(self.sc.open), 1)

    def test_opposite_side_shown_but_not_opened(self):
        self.approve(row(side="BUY"))
        self.sc.alert(row(side="SELL"), 27.0)
        self.assertIn("فعلاً قابل باز شدن نیست", self.sc.tg.sent[-1][0])
        self.sc.decide(str(self.sc.n), True, "cb")
        self.assertEqual(len(self.sc.open), 1)
        self.assertIn("خلاف جهت", self.sc.tg.edits[-1][1])

    def test_other_pending_alerts_not_cancelled_on_open(self):
        self.sc.alert(row(), 27.0)
        self.sc.alert(row(), 27.0)
        self.sc.decide("1", True, "cb")
        self.assertIn("2", self.sc.pending)

    def test_old_behaviour_switch(self):
        self.sc.alert_while_open = False
        self.sc.alert(row(), 27.0)
        self.sc.alert(row(), 27.0)
        self.sc.decide("1", True, "cb")
        self.assertEqual(self.sc.pending, {})        # cancelled like before


if __name__ == "__main__":
    unittest.main()
