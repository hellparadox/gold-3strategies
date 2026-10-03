"""بازار بسته: یک پیام «بازار بسته»، بدون پیام تکراری و بدون تلاش بیهوده برای تغییر حد ضرر."""
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace as NS

from tests.test_scout_tracker import ENTRY, FRAME, SOFT, make_scout, pos
from tools import scout_bot


class Weekend(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = make_scout(Path(self._d.name), mode="hold")
        self.sc.open[5] = {"setup": "engulfing", "side": "BUY", "entry": ENTRY, "sl": SOFT,
                           "risk": 6.04, "r1": False, "warned": False, "emerg": 4157.57,
                           "status_mid": None, "below": False, "hold": True, "alert": "1",
                           "approach": False, "ai_next": 0.0, "ai_note": ""}
        self.mkt = False
        self.sc.market_open = lambda: self.mkt

    def tearDown(self):
        self._d.cleanup()

    def run_at(self, t, price=4178.0, broker_sl=4157.62):
        self.sc.client.positions.return_value = [pos(price=price, profit=-1.0, sl=broker_sl)]
        with unittest.mock.patch("tools.scout_bot.time.time", return_value=t):
            self.sc.monitor(FRAME)

    def status_msgs(self):
        return [t for t, _ in self.sc.tg.sent if "📊" in t]

    def test_closed_market_one_note_then_silence(self):
        for i in range(12):                            # یک ساعت حلقه با بازار بسته
            self.sc.tg.send("another message")         # حتی وقتی پیام‌های دیگر وسط می‌آیند
            self.run_at(1000.0 + i * 301)
        msgs = self.status_msgs()
        self.assertEqual(len(msgs), 1)
        self.assertIn("بازار بسته", msgs[0])
        self.assertEqual(self.sc.tg.edits, [])
        self.sc.client.modify_sltp.assert_not_called()   # broker SL differs, but no retries

    def test_reopen_resumes_status_and_stop_sync(self):
        self.run_at(1000.0)
        self.mkt = True
        self.run_at(1060.0)                            # کمتر از ۵ دقیقه، ولی گزارش فوراً ادامه پیدا می‌کند
        self.assertEqual(len(self.status_msgs()) + len(self.sc.tg.edits), 2)
        self.assertNotIn("closed_note", self.sc.open[5])
        self.sc.client.modify_sltp.assert_called_once()

    def test_edit_not_modified_is_not_an_error(self):
        tg = scout_bot.TG.__new__(scout_bot.TG)
        tg.token, tg.chat = "x", 1
        resp = NS(json=lambda: {"ok": False, "description": "Bad Request: message is not modified"})
        with unittest.mock.patch("tools.scout_bot.requests.post", return_value=resp):
            self.assertTrue(tg.edit(7, "same text"))
        resp = NS(json=lambda: {"ok": False, "description": "Bad Request: message to edit not found"})
        with unittest.mock.patch("tools.scout_bot.requests.post", return_value=resp):
            self.assertFalse(tg.edit(7, "same text"))


if __name__ == "__main__":
    unittest.main()
