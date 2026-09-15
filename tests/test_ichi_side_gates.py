"""IMPROVE (2026-09-15) — targeted side-gate test (the ONE test requested).

Property under test: switching the kijun short gate OFF must remove ONLY the
kijun-layer SELL signals. The RAW (pre-cooldown, pre-priority) outputs of
every other direction/layer must be bitwise unchanged. Downstream differences
in the final `signal` column (cooldown re-routing) are EXPECTED and legal.
"""
import unittest

import numpy as np
import pandas as pd

from core import Settings
from strategies import build_from_settings


def _phase(n, start, slope, amp, period, seed, base=2200.0):
    """Deterministic OHLC segment: linear trend + sinusoidal pullbacks."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq="15min")
    t = np.arange(n)
    close = base + slope * t + amp * np.sin(2 * np.pi * t / period) + rng.normal(0, 0.4, n)
    return pd.DataFrame(
        {
            "open": close + rng.normal(0, 0.2, n),
            "high": close + np.abs(rng.normal(0.8, 0.3, n)),
            "low": close - np.abs(rng.normal(0.8, 0.3, n)),
            "close": close,
            "tick_volume": rng.integers(10, 200, n),
        },
        index=idx,
    )


def _phase_h1(n, start, slope, amp, period, seed, base=2200.0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq="h")
    t = np.arange(n)
    close = base + slope * t + amp * np.sin(2 * np.pi * t / period) + rng.normal(0, 0.4, n)
    return pd.DataFrame(
        {
            "open": close + rng.normal(0, 0.2, n),
            "high": close + np.abs(rng.normal(0.8, 0.3, n)),
            "low": close - np.abs(rng.normal(0.8, 0.3, n)),
            "close": close,
            "tick_volume": rng.integers(10, 200, n),
        },
        index=idx,
    )


def _synth():
    """3-phase M15 series (bear -> recovery -> momentum ramp) + matching H1.
    Verified to fire kijun SHORT (33) and kijun LONG (18) raw signals with
    the gates at legacy ON — so the gate test below is non-vacuous."""
    p1 = _phase(700, "2024-01-02", -0.55, 7.0, 34, 11)
    p2 = _phase(500, p1.index[-1] + pd.Timedelta(minutes=15),
                0.85, 10.0, 44, 13, base=float(p1["close"].iloc[-1]))
    p3 = _phase(300, p2.index[-1] + pd.Timedelta(minutes=15),
                2.2, 3.0, 20, 17, base=float(p2["close"].iloc[-1]))
    m15 = pd.concat([p1, p2, p3])
    h1a = _phase_h1(176, "2024-01-02", -4.0, 25.0, 30, 5)
    h1b = _phase_h1(105, h1a.index[-1] + pd.Timedelta(hours=1),
                    4.5, 28.0, 30, 7, base=float(h1a["close"].iloc[-1]))
    h1c = _phase_h1(60, h1b.index[-1] + pd.Timedelta(hours=1),
                    12.0, 8.0, 20, 9, base=float(h1b["close"].iloc[-1]))
    return m15, pd.concat([h1a, h1b, h1c])


def _build(gate_on):
    s = Settings.load("config/settings_ichimoku.yaml")
    s.set("strategy.params.ichimoku_m15.enable_kijun_short", gate_on)
    s.set("strategy.params.ichimoku_m15.enable_tenkan_short", True)
    return build_from_settings(s)


class T1KijunShortGateIsolation(unittest.TestCase):
    def test_gate_off_removes_only_kijun_short_raw(self):
        m15, h1 = _synth()
        f_on = _build(True).prepare_and_sign(m15, m15, h1)
        f_off = _build(False).prepare_and_sign(m15, m15, h1)

        # precondition: the ungated series DOES fire kijun shorts (else vacuous)
        self.assertTrue(bool(f_on["base_short_raw"].any()),
                        "synthetic series fires no kijun SHORT — test is vacuous")
        # also fires kijun longs, so the unchanged-assertions are non-trivial
        self.assertTrue(bool(f_on["base_long_raw"].any()))

        # 1) the gate removes kijun-layer SELLs entirely
        self.assertFalse(bool(f_off["base_short_raw"].any()),
                         "kijun SHORT raw must be all-False with the gate off")

        # 2) raw outputs of every other direction/layer are bitwise unchanged
        self.assertTrue(f_on.index.equals(f_off.index))
        for col in ("base_long_raw", "tenkan_long_raw", "tenkan_short_raw",
                    "sp2l_long_raw", "sp2l_short_raw"):
            self.assertTrue(
                (f_on[col].astype(bool) == f_off[col].astype(bool)).all(),
                f"{col} changed when only the kijun short gate was toggled",
            )


if __name__ == "__main__":
    unittest.main()
