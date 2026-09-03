"""Ichimoku (Kijun/Tenkan) strategy on the M15 frame — walk-forward tuned set.

The gold_m5_bot kijun_pullback logic executed on the M15 timeframe instead of
M5, with the parameter set validated by walk-forward on ~2y of XAUUSD data
(train PF 1.24 | holdout PF 1.26 | 3/4 folds pass — see Test_duoble_Tridebot).

Differences vs the M5 `kijun_pullback`:
    - Trading frame is the M15 data (signals/ATR/exits on M15 bars).
    - Trend EMA200 anchor disabled; H1 EMA50 confluence kept.
    - NY-session window 12:00-20:00 server time.
    - Cooldown 6 bars (M15 = 90 min).

Staged partial exit (bank partial_frac at +partial_tp_rr, then risk-free the
remainder at break-even) is simulated by the backtest engine when
``backtest.simulate_partial`` is enabled — enable it to measure the real
impact on PF before trusting it live.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd
from loguru import logger

from strategies.kijun_pullback import KijunPullbackStrategy
from strategies.base import Signal


class IchimokuM15Strategy(KijunPullbackStrategy):
    """Walk-forward-tuned Ichimoku pullback/momentum on XAUUSD M15."""

    name = "ichimoku_m15"
    oscillator = "none"
    bar_minutes = 15  # دقیقه‌های هر کندلِ استراتژی (برای برچسب cooldown)

    # شمارندهٔ سیگنال‌هایی که فیلتر کشش (/extension) واقعاً حذف کرده —
    # فقط در مسیر زنده (evaluate) incremented می‌شود؛ با ری‌استارت صفر می‌شود.
    extension_blocks = 0

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        params = super().default_params()
        params.update(
            {
                # walk-forward-validated M15 set
                "enable_trend_ema": False,
                "use_h1_filter": True,
                "h1_ema_period": 50,
                "enable_kijun": True,
                "enable_tenkan": True,
                "enable_sp2l": False,
                "kijun_near_atr": 0.20,
                "kijun_sl_atr": 1.6,
                "kijun_tp_sl_multiplier": 3.0,
                "tenkan_min_distance_atr": 0.40,
                "tenkan_tp_sl_multiplier": 3.0,
                "cooldown_bars": 6,
                "trade_start_hour": 12,
                "trade_end_hour": 20,
                "min_atr": 0.40,
                # staged exit: simulated by the backtest engine when
                # backtest.simulate_partial is enabled
                "partial_tp_rr": 1.5,
                "partial_frac": 0.5,
            }
        )
        return params

    def min_bars(self) -> int:
        return 150

    def describe(self) -> str:
        return (
            "XAUUSD M15 Ichimoku (walk-forward set): Kijun pullback + "
            "Tenkan momentum, H1 EMA50 confluence, NY session 12-20, "
            "cooldown 6 bars, no trend-EMA anchor"
        )

    # -------------------------------------------------------------- prepare

    def prepare(
        self,
        m5: pd.DataFrame,
        m15: Optional[pd.DataFrame] = None,
        h1: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """Run the kijun logic on the M15 frame (falls back to M5 if absent)."""
        frame = m15 if m15 is not None and not m15.empty else m5
        return super().prepare(frame, None, h1)

    # ----------------------------------------------------------- live entry

    def evaluate(
        self,
        m5: pd.DataFrame,
        m15: Optional[pd.DataFrame] = None,
        h1: Optional[pd.DataFrame] = None,
    ) -> Optional[Signal]:
        """Live evaluation on the last closed M15 bar (deduped per M15 bar)."""
        frame = m15 if m15 is not None and not m15.empty else m5
        if frame is None or len(frame) < 3:
            return None

        last_closed = frame.index[-1]  # main_live strips the forming candle
        if last_closed == getattr(self, "_last_eval_bar", None):
            return None

        prepared = self.prepare(m5, m15, h1)
        signal = self.generate_signal(prepared, index=-1)

        # فیلتر کشش: فقط وقتی بشمار که واقعاً سیگنالی حذف شده (هیچ لایهٔ
        # دیگری همان کندل سیگنال نداده) — و هر کندل فقط یک بار (dedup).
        if signal is None and len(prepared) > 0:
            last = prepared.iloc[-1]
            if bool(last.get("tenkan_ext_blocked", False)) and last_closed != getattr(
                self, "_last_block_bar", None
            ):
                self._last_block_bar = last_closed
                self.extension_blocks = int(getattr(self, "extension_blocks", 0)) + 1
                logger.warning(
                    "🚌 extension filter blocked a tenkan entry @ {} "
                    "(price extended > {:.1f}xATR from the session extreme)",
                    last_closed,
                    float(self.pf("extension_max_atr", 4.0)),
                )

        if signal is not None:
            self._last_eval_bar = last_closed
        return signal
