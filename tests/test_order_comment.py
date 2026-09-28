"""MT5 order comments: always terminal-safe, and pre-send rejections are not 'unknown'."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import core.mt5_client as module
from core.mt5_client import MT5Client, MT5Config, safe_comment
from tools.scout_bot import scout_comment

SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")


class SafeComment(unittest.TestCase):
    def test_existing_bot_comments_unchanged(self):
        for c in ("gold_m5_bot", "ichimoku_m15", "orb_gold"):
            self.assertEqual(safe_comment(c), c)

    def test_spaces_plus_and_length(self):
        out = safe_comment("scout:cloud_break + kijun_pullback + pullback_resume")
        self.assertLessEqual(len(out), 25)
        self.assertTrue(set(out) <= SAFE, out)
        self.assertFalse(out.startswith("_") or out.endswith("_"))

    def test_empty_uses_fallback(self):
        self.assertEqual(safe_comment("", "ichimoku_m15"), "ichimoku_m15")
        self.assertEqual(safe_comment("+++", ""), "bot")
        self.assertEqual(safe_comment(None, "manual close"), "manual_close")


class ScoutComment(unittest.TestCase):
    def test_journal_failures(self):
        # the three setups that MT5 rejected (data/scout_journal_v2.csv, order_failed)
        cases = {
            "cloud_break + kijun_pullback + pullback_resume": "scout_cb_kp_pr",
            "pullback_resume + cloud_break + kijun_pullback": "scout_pr_cb_kp",
            "tk_cross + tenkan_momentum + range_break +1": "scout_tk_tm_rb",
        }
        for setup, want in cases.items():
            with self.subTest(setup=setup):
                self.assertEqual(scout_comment(setup), want)

    def test_single_and_unknown(self):
        self.assertEqual(scout_comment("tenkan_momentum"), "scout_tm")
        self.assertEqual(scout_comment("brand new setup"), "scout_bran")
        self.assertEqual(scout_comment(""), "scout")
        for s in ("a + b + c + d + e + f + g + h + i", "x" * 80):
            out = scout_comment(s)
            self.assertLessEqual(len(out), 25)
            self.assertTrue(set(out) <= SAFE)


class PreSendRejection(unittest.TestCase):
    def setUp(self):
        self.err = [(-2, 'Invalid "comment" argument')]
        self.api = NS(ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1, TRADE_ACTION_DEAL=1,
                      ORDER_TIME_GTC=0, TRADE_RETCODE_DONE=10009,
                      TRADE_RETCODE_REQUOTE=10004, TRADE_RETCODE_PRICE_CHANGED=10020,
                      TRADE_RETCODE_PRICE_OFF=10021, TRADE_RETCODE_INVALID_FILL=10030,
                      order_send=Mock(return_value=None), last_error=lambda: self.err[0])
        for p in (patch.object(module, "mt5", self.api), patch.object(module, "MT5_AVAILABLE", True),
                  patch.object(module.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)
        self.c = MT5Client(MT5Config())
        self.c.ensure_connected = Mock(return_value=True)
        self.c.symbol_info = Mock(return_value=NS(digits=2, volume_step=.01, volume_min=.01, volume_max=10))
        self.c.get_tick = Mock(return_value=NS(bid=100., ask=100.08))
        self.c._filling_modes = Mock(return_value=[0, 1, 2])

    def test_invalid_params_is_definite_not_uncertain(self):
        res = self.c.send_market_order("SELL", 0.01, sl=101.0, comment="scout:a + b")
        self.assertFalse(res.ok)
        self.assertFalse(res.uncertain)
        self.assertEqual(res.retcode, -2)
        self.assertEqual(self.api.order_send.call_count, 1)          # never resent
        sent = self.api.order_send.call_args[0][0]["comment"]
        self.assertTrue(set(sent) <= SAFE, sent)

    def test_other_errors_stay_uncertain(self):
        self.err[0] = (-10001, "IPC send failed")
        res = self.c.send_market_order("BUY", 0.01, sl=99.0)
        self.assertTrue(res.uncertain)
        self.assertEqual(self.api.order_send.call_count, 1)

    def test_exception_with_stale_minus2_stays_uncertain(self):
        self.api.order_send.side_effect = RuntimeError("IPC")
        res = self.c.send_market_order("BUY", 0.01, sl=99.0)
        self.assertTrue(res.uncertain)

    def test_default_comment_is_config_comment(self):
        self.api.order_send.return_value = NS(retcode=10009, comment="ok", order=1, deal=2,
                                              position=1, price=100.08, volume=0.01)
        res = self.c.send_market_order("BUY", 0.01, sl=99.0)
        self.assertTrue(res.ok)
        self.assertEqual(self.api.order_send.call_args[0][0]["comment"], MT5Config().comment)


class ModuleStructure(unittest.TestCase):
    def test_no_dead_code_and_import_warning_in_place(self):
        """Regression: the import-guard warning once ended up after safe_comment's return."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(module))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "safe_comment")
        self.assertIsInstance(fn.body[-1], ast.Return)
        guard = next(n for n in tree.body if isinstance(n, ast.Try))
        handler_src = ast.unparse(guard.handlers[0])
        self.assertIn("MetaTrader5 package unavailable", handler_src)


if __name__ == "__main__":
    unittest.main()
