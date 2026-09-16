"""IMPROVE (2026-09-16) — H1 timing regression test (incident 106282597).

The live call path (main_live._on_new_closed_bar) used to strip the forming
H1 candle before passing the frame to the strategy, whose internal shift(1)
then skipped one MORE closed candle. Result: on :00/:15/:30 M15 bars the live
bot decided with an H1 candle up to one hour older than the backtest
alignment. The fix passes the H1 frame WITH its forming candle.

This test proves, on synthetic data with known H1 bull/bear candles:
  1. FIXED live path == backtest alignment at every bar offset;
  2. the decision reflects the LAST CLOSED H1 candle (hour(B)-1h), never the
     forming candle (no intra-hour leak);
  3. the OLD path (stripped frame) lagged one extra candle on :00/:15/:30 —
     documented so the regression cannot silently return.
"""
import unittest

import numpy as np
import pandas as pd

from core import Settings
from strategies import build_from_settings


def _frames():
    # ---- H1: 60 warmup candles at 100 (EMA ~100), then alternating 110/90 so
    # bull/c bear identity of every candle is unambiguous (margin ~10).
    h1_idx = pd.date_range("2026-01-01 06:00", periods=60, freq="h")
    closes = [100.0] * 60
    labels = [110.0, 90.0]  # 10:00=110(bull) 11:00=90(bear) 12:00=110 13:00=90...
    for i in range(10):
        h1_idx = h1_idx.append(pd.date_range(h1_idx[-1] + pd.Timedelta(hours=1), periods=1, freq="h"))
        closes.append(labels[i % 2])
    h1 = pd.DataFrame(
        {"open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
         "close": closes, "tick_volume": 100, "spread": 20},
        index=pd.DatetimeIndex(h1_idx),
    )
    # ---- M15: flat 100-ish prices (content irrelevant to h1_trend_bull).
    m15_idx = pd.date_range(h1.index[0], h1.index[-1] + pd.Timedelta(hours=2), freq="15min")
    n = len(m15_idx)
    m15 = pd.DataFrame(
        {"open": [100.0] * n, "high": [100.5] * n, "low": [99.5] * n, "close": [100.0] * n,
         "tick_volume": 50, "spread": 20},
        index=m15_idx,
    )
    return m15, h1


def _bull_of(h1, label):
    """Bull identity of the H1 candle labeled `label` (close vs span-20 EMA)."""
    ema = h1["close"].astype("float64").ewm(span=20, adjust=False).mean()
    return bool(h1.loc[label, "close"] >= ema.loc[label])


class T1H1TimingAlignment(unittest.TestCase):
    def test_fixed_live_path_matches_backtest_and_uses_last_closed_candle(self):
        m15, h1 = _frames()
        s = Settings.load("config/settings_ichimoku.yaml")
        strat = build_from_settings(s)

        # backtest reference: full frames (forming candles included)
        f_full = strat.prepare_and_sign(m15, m15, h1)

        # hour 02:00 bars: hour(B)-1h = 01:00 candle (bear, close 90),
        # hour(B) = 02:00 candle (bull, close 110 — the FORMING one at decision)
        for minute in (0, 15, 30, 45):
            B = pd.Timestamp("2026-01-04 02:00") + pd.Timedelta(minutes=minute)
            T = B + pd.Timedelta(minutes=15)
            self.assertIn(B, m15.index)

            # FIXED live path: H1 passed WITH the forming candle (label hour(T))
            h1_fix = h1.loc[: T.floor("h")]
            f_fix = strat.prepare_and_sign(m15.loc[:B], m15.loc[:B], h1_fix)
            got = bool(f_fix["h1_trend_bull"].iloc[-1])

            self.assertEqual(got, bool(f_full.loc[B, "h1_trend_bull"]),
                             f"fixed live != backtest alignment at {B}")
            self.assertEqual(got, _bull_of(h1, pd.Timestamp("2026-01-04 01:00")),
                             f"decision at {B} must use the 01:00 (last closed) candle")
            self.assertNotEqual(got, _bull_of(h1, pd.Timestamp("2026-01-04 02:00")),
                                f"decision at {B} leaked the FORMING 02:00 candle")

    def test_old_stripped_path_lags_one_extra_candle_on_hour_start_bars(self):
        m15, h1 = _frames()
        s = Settings.load("config/settings_ichimoku.yaml")
        strat = build_from_settings(s)

        # OLD path: forming candle stripped by the caller (the pre-fix bug).
        # :00/:15/:30 bars fell back to the 00:00 candle (one extra hour back);
        # :45 bars were correct (01:00).
        for minute, expected_candle in ((0, "00:00"), (15, "00:00"), (30, "00:00"), (45, "01:00")):
            B = pd.Timestamp("2026-01-04 02:00") + pd.Timedelta(minutes=minute)
            T = B + pd.Timedelta(minutes=15)
            h1_old = h1.loc[: T.floor("h") - pd.Timedelta(hours=1)]
            f_old = strat.prepare_and_sign(m15.loc[:B], m15.loc[:B], h1_old)
            got = bool(f_old["h1_trend_bull"].iloc[-1])
            self.assertEqual(
                got, _bull_of(h1, pd.Timestamp(f"2026-01-04 {expected_candle}")),
                f"old path should (buggily) use the {expected_candle} candle at {B}")


if __name__ == "__main__":
    unittest.main()
