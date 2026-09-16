# _incident_replay.py — local replay of the 2026-09-16 05:00 incident window.
# Data: local MetaQuotes-Demo terminal (structure-level evidence; WMMarkets
# prices differ slightly — his recorded anchors validate alignment).
import json
from datetime import datetime

import MetaTrader5 as mt5
import numpy as np
import pandas as pd
from loguru import logger

logger.remove()
from core import Settings
from strategies import build_from_settings

OUT = "_ichi_out"

# ---------------------------------------------------------------- 1. data
assert mt5.initialize()
SYMBOL = "XAUUSD"


def rates(tf, minutes, days_back=8):
    now = datetime.now()
    r = mt5.copy_rates_range(SYMBOL, tf,
                             pd.Timestamp(now) - pd.Timedelta(days=days_back),
                             pd.Timestamp(now))
    df = pd.DataFrame(r)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df.set_index("time") [["open", "high", "low", "close", "tick_volume", "spread"]]
    df.index.name = None
    return df


m15 = rates(mt5.TIMEFRAME_M15, 15)
h1 = rates(mt5.TIMEFRAME_H1, 60)
m5 = rates(mt5.TIMEFRAME_M5, 5)
mt5.shutdown()
print("server clock check: last M1 bar would be now; last m15:", m15.index[-1],
      "| local now:", pd.Timestamp.now())
m15.to_csv(f"{OUT}/incident_m15_metaquotes.csv")
h1.to_csv(f"{OUT}/incident_h1_metaquotes.csv")
m5.to_csv(f"{OUT}/incident_m5_metaquotes.csv")

# ---------------------------------------------------------------- 2. locate rally by price
w = m15[(m15.index >= pd.Timestamp("2026-09-15 00:00")) & (m15.index <= pd.Timestamp("2026-09-16 23:59"))]
anchor = w[(w["close"] > 4270) & (w["close"] < 4350)]
print("\nwindow bars 04:00-08:30 (2026-09-16):")
win = w[(w.index >= pd.Timestamp("2026-09-16 04:00")) & (w.index <= pd.Timestamp("2026-09-16 08:30"))]
print(win[["open", "high", "low", "close"]].round(2).to_string())
