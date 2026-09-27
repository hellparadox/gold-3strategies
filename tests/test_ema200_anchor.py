import unittest
import numpy as np
import pandas as pd
from strategies.ichimoku_m15 import IchimokuM15Strategy


def long_setup(ema_200: float) -> pd.DataFrame:
    """7 bars; the last one satisfies every Kijun-pullback AND Tenkan-momentum long rule."""
    n = 7
    idx = pd.date_range("2026-09-01 12:00", periods=n, freq="15min")
    f = pd.DataFrame(index=idx)
    f["atr"] = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.5]          # rising ATR for the tenkan layer
    f["kijun"], f["tenkan"] = 100.0, 100.6
    f["cloud_top"], f["cloud_bottom"] = 99.0, 98.0
    f["kijun_slope"] = 0.5
    f["h1_trend_bull"], f["h1_trend_bear"] = True, False
    f["open"], f["high"], f["low"], f["close"] = 100.5, 101.4, 100.1, 101.2
    f["ema_200"] = ema_200
    return f


class EMA200Anchor(unittest.TestCase):
    def layers(self, frame):
        s = IchimokuM15Strategy()
        return bool(s._base_layer(frame)[0].iloc[-1]), bool(s._tenkan_layer(frame)[0].iloc[-1])

    def test_fixture_is_a_valid_long(self):
        self.assertEqual(self.layers(long_setup(ema_200=90.0)), (True, True))

    def test_ema200_blocks_longs_below_it(self):
        self.assertEqual(self.layers(long_setup(ema_200=110.0)), (False, False))

    def test_nan_ema_blocks(self):
        self.assertEqual(self.layers(long_setup(ema_200=np.nan)), (False, False))

    def test_dead_key_removed(self):   # option B guard: nobody may re-add a key the code ignores
        self.assertNotIn("enable_trend_ema", IchimokuM15Strategy.default_params())


if __name__ == "__main__":
    unittest.main()


class ParameterHygiene(unittest.TestCase):
    def test_dead_kijun_switch_removed(self):
        self.assertNotIn("enable_kijun", IchimokuM15Strategy.default_params())

    def test_every_default_param_is_read_somewhere(self):
        import os
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = "".join(open(os.path.join(root, *p), encoding="utf-8").read() for p in (
            ("strategies", "kijun_pullback.py"), ("strategies", "ichimoku_m15.py"),
            ("strategies", "base.py"), ("backtest", "engine.py")))
        dead = [k for k in IchimokuM15Strategy.default_params()
                if not re.search(r'(?:pb|pf|pi|p|get|getp|params\.get)\(\s*["\']%s["\']' % re.escape(k), src)]
        self.assertEqual(dead, [])

    def test_ignored_settings_are_reported(self):
        from loguru import logger
        seen = []
        sink = logger.add(lambda m: seen.append(str(m)), level="WARNING", format="{message}")
        try:
            IchimokuM15Strategy(params={"enable_trend_ema": False, "confirm_bars": 36, "h1_ema_period": 50})
        finally:
            logger.remove(sink)
        self.assertTrue(any("confirm_bars, enable_trend_ema" in s for s in seen), seen)
        self.assertFalse(any("h1_ema_period" in s for s in seen))

    def test_describe_reports_settings_in_force(self):
        s = IchimokuM15Strategy(params={"trade_start_hour": 0, "trade_end_hour": 21, "enable_kijun_short": False})
        text = s.describe()
        self.assertIn("EMA200 trend anchor", text)
        self.assertIn("entries 0-21", text)
        self.assertIn("Kijun pullback (long only)", text)
        self.assertNotIn("no trend-EMA", text)
