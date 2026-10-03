"""«🤖 تحلیل» button, «❓ راهنما» help, and the double-click guard on «بستن»."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pandas as pd

from tools import scout_ai
from tools import scout_control as ctl
from tests.test_scout_control import cb, msg, scout


def row(side="BUY"):
    return pd.Series({"setup": "cloud_break", "side": side, "ref_close": 4175.5, "sl_dist": 6.0,
                      "tp_dist": 12.0, "atr": 5.0, "bar": pd.Timestamp("2026-10-02 15:15")})


FRAME = pd.DataFrame({"open": [4170.0, 4172.0], "high": [4174.0, 4177.0], "low": [4169.0, 4171.0],
                      "close": [4172.0, 4175.5], "tenkan": [4171.0, 4172.5], "kijun": [4168.0, 4169.0],
                      "cloud_top": [4165.0, 4165.5], "cloud_bot": [4160.0, 4160.5], "ema200": [4150.0, 4150.2],
                      "h1_ema": [4166.0, 4166.4], "atr": [5.0, 5.0], "swing_hi": [4180.0, 4180.0],
                      "swing_lo": [4160.0, 4160.0]},
                     index=pd.to_datetime(["2026-10-02 15:00", "2026-10-02 15:15"]))

GOOD = ('{"decision":"TAKE","confidence":72,"reasons":["روند ساعتی صعودی است","قیمت ۰.۶ ATR بالای تنکان"],'
        '"tip":"در ۱ برابر ریسک نصف سود را بگیرید"}')


class Analysis(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = scout(Path(self._d.name))
        self.sc._frame = FRAME
        self.sc.alert(row(), 27.0)
        self.aid = str(self.sc.n)

    def tearDown(self):
        if self.sc._an_pool:
            self.sc._an_pool.shutdown(wait=True)
        self._d.cleanup()

    def provider(self, text=None, err=None):
        p = Mock()
        p.model = "gemini-x"
        if err:
            p.complete.side_effect = err
        else:
            p.complete.return_value = NS(text=text, model="gemini-x")
        return p

    def run_analysis(self):
        self.sc.handle_update(cb(f"x|{self.aid}"))
        self.sc._an_pool.shutdown(wait=True)
        self.sc._an_pool = None
        self.sc.poll_analyses()

    def test_alert_has_three_buttons_and_context(self):
        buttons = self.sc.tg.sent[-1][1][0]
        self.assertEqual([b["callback_data"] for b in buttons], ["a|1|y", "a|1|n", "x|1"])
        ctx = self.sc.pending[self.aid]["ctx"]
        for part in ("BUY cloud_break", "entry 4175.50", "close vs: tenkan", "20-bar swing high: 4180.00",
                     "last closed M15 bars"):
            self.assertIn(part, ctx)

    def test_analysis_reply_with_decision_buttons(self):
        self.sc._ai = scout_ai.ScoutAI([self.provider(GOOD)])
        self.run_analysis()
        text, buttons = self.sc.tg.sent[-1]
        for part in ("تحلیل هوش مصنوعی", "✅ ورود", "۷۲" if False else "72٪", "روند ساعتی صعودی است",
                     "💡", "فقط راهنماست"):
            self.assertIn(part, text)
        self.assertEqual([b["callback_data"] for b in buttons[0]], ["a|1|y", "a|1|n"])
        self.assertEqual(self.sc.tg.replies[-1], self.sc.pending[self.aid]["mid"])
        system = self.sc._ai.providers[0].complete.call_args.args[0]
        self.assertIn("TAKE", system)
        n = len(self.sc.tg.sent)
        self.sc.handle_update(cb(f"x|{self.aid}"))           # second press: cached, no new AI call
        self.assertEqual(self.sc._ai.providers[0].complete.call_count, 1)
        self.assertEqual(len(self.sc.tg.sent), n + 1)

    def test_fallback_model_used_and_error_message(self):
        bad = self.provider(err=RuntimeError("HTTP 503"))
        self.sc._ai = scout_ai.ScoutAI([bad, self.provider(GOOD)])
        self.run_analysis()
        self.assertIn("✅ ورود", self.sc.tg.sent[-1][0])
        self.sc._an_cache.clear()
        self.sc._ai = scout_ai.ScoutAI([self.provider(err=RuntimeError("HTTP 503"))])
        self.run_analysis()
        text, buttons = self.sc.tg.sent[-1]
        self.assertIn("در دسترس نیست", text)
        self.assertIn("x|1", [b["callback_data"] for b in buttons[0]])   # can retry

    def test_expired_alert(self):
        self.sc.pending.clear()
        self.sc.handle_update(cb("x|1"))
        self.assertIn("دیگر معتبر نیست", self.sc.tg.acks[-1])

    def test_no_ai_configured(self):
        self.sc._ai = False
        self.sc.handle_update(cb(f"x|{self.aid}"))
        self.assertIn("در دسترس نیست", self.sc.tg.sent[-1][0])

    def test_parse_entry(self):
        a = scout_ai.parse_entry("ok " + GOOD, "m")
        self.assertTrue(a.ok)
        self.assertEqual((a.decision, a.confidence, len(a.reasons)), ("TAKE", 72, 2))
        for bad in ("", "nope", '{"decision":"MAYBE"}'):
            self.assertFalse(scout_ai.parse_entry(bad).ok)


class HelpAndClose(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = scout(Path(self._d.name))

    def tearDown(self):
        self._d.cleanup()

    def test_help_from_keyboard_menu_and_command(self):
        for upd in (msg("❓ راهنما"), msg("/help"), cb("k|help")):
            self.sc.handle_update(upd)
            text = self.sc.tg.sent[-1][0]
            for part in ("راهنمای اسکات", "✅ <b>تأیید</b>", "🤖 <b>تحلیل</b>", "🔻 <b>بستن</b>",
                         "⏸ <b>نگه دار</b>", "📋 <b>پوزیشن‌ها</b>", "⚙️ <b>حالت حد ضرر</b>",
                         "حداکثر 2 پوزیشن", "ریسک کل حداکثر 20$", "حداکثر 10$"):
                self.assertIn(part, text)
        self.assertIn([{"text": "❓ راهنما"}], self.sc.KEYBOARD["keyboard"])
        self.assertIn("help", [c["command"] for c in ctl.MENU_COMMANDS])

    def test_double_click_close_ignored(self):
        self.sc.open[5] = {"setup": "x", "side": "BUY", "entry": 1.0, "sl": 0.5, "risk": 0.5}
        self.sc.client.close_position.return_value = NS(ok=True, price=1.1, comment="")
        for _ in range(6):
            self.sc.handle_update(cb("c|5"))
        self.sc.client.close_position.assert_called_once()
        self.assertIn("یک بار کافی است", self.sc.tg.acks[-1])


if __name__ == "__main__":
    unittest.main()
