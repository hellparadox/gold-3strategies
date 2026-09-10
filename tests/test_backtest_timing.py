import unittest

import pandas as pd

from backtest.engine import BacktestConfig, BacktestEngine, BacktestTrade
from core import Settings
from core.risk_manager import RiskConfig, SymbolSpec
from strategies import build_from_settings


def engine(config="config/settings_ichimoku.yaml"):
    s = Settings.load(config)
    cfg = BacktestConfig.from_settings(s)
    cfg.news_filter_enabled = False  # isolate execution, never fetch network data
    return BacktestEngine(build_from_settings(s), RiskConfig.from_settings(s), cfg, SymbolSpec.gold_default())


class BacktestTiming(unittest.TestCase):
    def test_actual_orb_does_not_enter_before_previous_trade_exits(self):
        e = engine("config/settings.yaml")
        idx = pd.date_range("2024-01-01", periods=576, freq="5min")
        df = pd.DataFrame(dict(open=99.5, high=100., low=99., close=99.5, tick_volume=100), index=idx)
        mask = (idx >= "2024-01-02 02:00") & (idx < "2024-01-02 05:00")
        df.loc[mask, ["high", "low"]] = [99.8, 99.2]
        for tm, values in [("05:00", [99.5,100.1,99.4,99.8]), ("05:05", [99.8,99.9,99.5,99.7]),
                           ("05:10", [99.7,99.8,98.99,99.2]), ("05:15", [99.2,99.3,98.,98.3])]:
            df.loc["2024-01-02 " + tm, ["open","high","low","close"]] = values
        prepared = e.strategy.prepare_and_sign(df)
        self.assertEqual(prepared.loc[prepared.signal != 0, "signal"].tolist(), [1, -1])
        r = e.run(df)
        self.assertEqual(len(r.trades), 1)
        self.assertEqual(r.trades[0].side, "BUY")
        self.assertEqual(r.trades[0].exit_time, pd.Timestamp("2024-01-02 05:15"))

    def make_trade(self, e, side="BUY", lot=.03):
        levels = e.risk.build_levels(side, 100., 1., 533.)
        return BacktestTrade(ticket=1, side=side, lot=lot, entry_time=pd.Timestamp("2024-01-02"),
                             entry_price=100., sl=levels.sl, tp=levels.tp, atr=1.,
                             initial_sl_distance=abs(100.-levels.sl))

    def process(self, e, t, high, low):
        closed = e._process_open_bar(t, pd.Timestamp("2024-01-02 12:15"), high, low,
                                    (high+low)/2, 1., e._spread_price, e._slip_price)
        if closed:
            e._settle_trade(t, 533.)
        return closed

    def test_buy_partial_precedes_full_target(self):
        e = engine(); t = self.make_trade(e)
        self.assertTrue(self.process(e,t,103.,100.))
        self.assertTrue(t.partial_done)
        self.assertAlmostEqual(t.partial_lot,.01)
        self.assertAlmostEqual(t.net_profit,8.4)

    def test_sell_partial_uses_ask(self):
        e = engine(); t = self.make_trade(e,"SELL")
        self.assertTrue(self.process(e,t,100.,96.92))
        self.assertTrue(t.partial_done)
        self.assertAlmostEqual(t.net_profit,8.4)

    def test_sl_wins_ambiguous_bar(self):
        e = engine(); t = self.make_trade(e)
        self.assertTrue(self.process(e,t,103.,98.))
        self.assertFalse(t.partial_done)
        self.assertEqual(t.exit_reason,"sl")
        self.assertAlmostEqual(t.net_profit,-4.8)

    def test_disabled_partial_retains_full_target(self):
        e = engine(); e.partial_enabled = False; t = self.make_trade(e)
        self.process(e,t,103.,100.)
        self.assertFalse(t.partial_done)
        self.assertAlmostEqual(t.net_profit,9.)

    def test_minimum_volume_cannot_split(self):
        e = engine(); t = self.make_trade(e,lot=.01)
        self.process(e,t,103.,100.)
        self.assertFalse(t.partial_done)
        self.assertAlmostEqual(t.net_profit,3.)

    def test_partial_beyond_full_target_is_not_filled(self):
        e = engine(); e.partial_rr = 3.; t = self.make_trade(e)
        self.process(e,t,106.,100.)
        self.assertFalse(t.partial_done)
        self.assertAlmostEqual(t.net_profit,9.)


if __name__ == "__main__":
    unittest.main()
