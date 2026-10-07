"""پیام «اسکات روشن شد» تعداد پوزیشن‌های باز را درست می‌گوید (قبلاً قبل از بازیابی فرستاده می‌شد و «0» می‌نوشت)."""
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace as NS

from tests.test_scout_tracker import ENTRY, SOFT, make_scout, pos
from tools import scout_tracker as trk


class Startup(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = make_scout(Path(self._d.name))
        sc = self.sc
        sc.client.connect.return_value = True
        sc.client.account_info.return_value = NS(login=20277252, server="WMMarkets-Demo", balance=358.88,
                                                 trade_mode=0)
        sc.client.positions.return_value = [pos(tk=5, price=4178.0)]
        sc.tg.start = lambda: None
        sc.tg._call = unittest.mock.Mock()
        sc.paused, sc.selftest, sc.dash_enabled, sc.started = False, False, False, 0.0
        sc.expiry = 1800
        sc.beat = unittest.mock.Mock(side_effect=KeyboardInterrupt)    # بعد از راه‌اندازی، حلقه را تمام کن
        trk.save_state(sc.state_path, {5: {"setup": "engulfing", "side": "BUY", "entry": ENTRY, "sl": SOFT,
                                           "risk": 6.04, "hold": False}})

    def tearDown(self):
        self._d.cleanup()

    def test_startup_message_counts_restored_positions(self):
        self.sc.run()
        started = [t for t, _ in self.sc.tg.sent if "اسکات روشن شد" in t]
        self.assertEqual(len(started), 1)
        self.assertIn("پوزیشن باز: 1", started[0])
        self.assertIn(5, self.sc.open)

    def test_without_positions_still_zero(self):
        self.sc.state_path.unlink()
        self.sc.client.positions.return_value = []
        self.sc.run()
        started = [t for t, _ in self.sc.tg.sent if "اسکات روشن شد" in t]
        self.assertIn("پوزیشن باز: 0", started[0])


if __name__ == "__main__":
    unittest.main()
