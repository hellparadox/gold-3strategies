# _incident_conditions.py — per-candle BUY condition autopsy for the incident.
# VPS settings (declared): h1_ema_period=20, sl 2.0/tp 3.0 (risk block, not
# needed for signal logic), cooldown 6, enable_kijun_short=false,
# enable_tenkan_short=true, extension off, trade hours 0-21.
import numpy as np
import pandas as pd
from loguru import logger

logger.remove()
from core import Settings
from strategies import build_from_settings

m15 = pd.read_csv("_ichi_out/incident_m15_metaquotes.csv", index_col=0, parse_dates=True)
h1 = pd.read_csv("_ichi_out/incident_h1_metaquotes.csv", index_col=0, parse_dates=True)

# VPS-declared settings on top of the committed yaml
s = Settings.load("config/settings_ichimoku.yaml")
s.set("strategy.params.ichimoku_m15.h1_ema_period", 20)
s.set("strategy.params.ichimoku_m15.enable_kijun_short", False)
s.set("strategy.params.ichimoku_m15.enable_tenkan_short", True)
strat = build_from_settings(s)

f = strat.prepare_and_sign(m15, m15, h1)

# ---- H1 EMA20 series (same math as the strategy: ema on close) ----
def ema(x, n):
    return x.ewm(span=n, adjust=False).mean()

h1e = h1.copy()
h1e["h1_ema"] = ema(h1e["close"].astype("float64"), 20)

# backtest-style bar B -> H1 candle labeled hour(B)-1h
# live-style    bar B -> H1 candle labeled hour(B)-2h (main_live drops the
# forming H1 row, then the code's shift(1) drops one more closed candle)
def h1_candle(label):
    if label in h1e.index:
        r = h1e.loc[label]
        return float(r["close"]), float(r["h1_ema"])
    return np.nan, np.nan

rows = []
for t, row in f.iterrows():
    if not (pd.Timestamp("2026-09-16 04:45") <= t <= pd.Timestamp("2026-09-16 08:00")):
        continue
    hr = t.hour
    c_bt, e_bt = h1_candle(pd.Timestamp(f"2026-09-16 {hr-1:02d}:00"))
    c_lv, e_lv = h1_candle(pd.Timestamp(f"2026-09-16 {hr-2:02d}:00"))
    bull_bt = bool(c_bt >= e_bt) if np.isfinite(c_bt) and np.isfinite(e_bt) else None
    bull_lv = bool(c_lv >= e_lv) if np.isfinite(c_lv) and np.isfinite(e_lv) else None
    atr = row.get("atr", np.nan)
    tenkan, kijun = row.get("tenkan", np.nan), row.get("kijun", np.nan)
    dist = (row["close"] - tenkan) / atr if atr and np.isfinite(atr) and np.isfinite(tenkan) else np.nan
    rows.append({
        "bar": t.strftime("%H:%M"),
        "close": round(row["close"], 2),
        "ema200": round(row.get("ema_200", np.nan), 2) if np.isfinite(row.get("ema_200", np.nan)) else None,
        "cloud_top": round(row.get("cloud_top", np.nan), 2) if np.isfinite(row.get("cloud_top", np.nan)) else None,
        "tenkan": round(tenkan, 2) if np.isfinite(tenkan) else None,
        "kijun": round(kijun, 2) if np.isfinite(kijun) else None,
        "atr": round(atr, 2) if np.isfinite(atr) else None,
        "dist/ATR": round(dist, 2) if np.isfinite(dist) else None,
        "h1bull_bt": bull_bt, "h1bull_live": bull_lv,
        "h1_candle_bt": f"{hr-1:02d}:00", "h1_candle_live": f"{hr-2:02d}:00",
        "raw_kijun_buy": bool(row.get("base_long_raw", False)),
        "raw_tenkan_buy": bool(row.get("tenkan_long_raw", False)),
        "raw_kijun_sell": bool(row.get("base_short_raw", False)),
        "raw_tenkan_sell": bool(row.get("tenkan_short_raw", False)),
        "signal": int(row["signal"]), "layer": row.get("signal_layer", ""),
    })

df = pd.DataFrame(rows).set_index("bar")
print(df.to_string())

print("\n--- H1 candles around the event (label, close, ema20) ---")
for lbl in ["2026-09-16 02:00", "2026-09-16 03:00", "2026-09-16 04:00", "2026-09-16 05:00",
            "2026-09-16 06:00", "2026-09-16 07:00"]:
    c, e = h1_candle(pd.Timestamp(lbl))
    print(lbl, "close", round(c, 2), "ema20", round(e, 2), "bull", c >= e)
