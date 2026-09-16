# _incident_live_vs_bt.py — emulate the EXACT live call path per decision bar
# and compare with the backtest-style call. Also: kijun_short gate on/off.
#
# Live path (main_live._on_new_closed_bar): at decision time T (= close of M15
# bar B), frames are fetched and the FORMING candle is dropped via iloc[:-1]:
#   m15 passed = bars <= B          (forming bar B+15min dropped)
#   h1  passed = bars <= hour(T)-1h (forming candle hour(T) dropped)
# Then prepare()'s internal shift(1) drops one MORE closed H1 candle.
# Backtest path: full frames incl. forming candles; shift(1) alone handles it.
import pandas as pd
from loguru import logger

logger.remove()
from core import Settings
from strategies import build_from_settings

m15 = pd.read_csv("_ichi_out/incident_m15_metaquotes.csv", index_col=0, parse_dates=True)
h1 = pd.read_csv("_ichi_out/incident_h1_metaquotes.csv", index_col=0, parse_dates=True)

BARS = pd.date_range("2026-09-16 04:45", "2026-09-16 08:00", freq="15min")


def build(kijun_short):
    s = Settings.load("config/settings_ichimoku.yaml")
    s.set("strategy.params.ichimoku_m15.h1_ema_period", 20)
    s.set("strategy.params.ichimoku_m15.enable_kijun_short", kijun_short)
    s.set("strategy.params.ichimoku_m15.enable_tenkan_short", True)
    return build_from_settings(s)


def ema20_of(h1f):
    return h1f["close"].astype("float64").ewm(span=20, adjust=False).mean()


for kijun_short in (False, True):
    strat = build(kijun_short)
    # full-frame (backtest-style) once
    f_bt = strat.prepare_and_sign(m15, m15, h1)
    rows = []
    for B in BARS:
        T = B + pd.Timedelta(minutes=15)
        m15_live = m15.loc[:B]
        h1_live = h1.loc[:T.floor("h") - pd.Timedelta(hours=1)]
        f_lv = strat.prepare_and_sign(m15_live, m15_live, h1_live)
        r_bt, r_lv = f_bt.loc[B], f_lv.iloc[-1]
        # which H1 candle actually fed the live decision (shift(1) on passed frame)
        used_lbl = h1_live.index[-2] if len(h1_live) >= 2 else None
        closed_lbl = h1_live.index[-1] if len(h1_live) >= 1 else None
        rows.append({
            "bar": B.strftime("%H:%M"),
            "h1bull_bt": bool(r_bt["h1_trend_bull"]),
            "h1bull_live": bool(r_lv["h1_trend_bull"]),
            "live_uses_candle": used_lbl.strftime("%H:%M") if used_lbl is not None else None,
            "last_closed_candle": closed_lbl.strftime("%H:%M") if closed_lbl is not None else None,
            "sig_bt": int(r_bt["signal"]), "sig_live": int(r_lv["signal"]),
            "layer_live": r_lv.get("signal_layer", ""),
            "t_buy_raw": bool(r_bt["tenkan_long_raw"]), "k_buy_raw": bool(r_bt["base_long_raw"]),
        })
    df = pd.DataFrame(rows).set_index("bar")
    print(f"\n================ enable_kijun_short={kijun_short} ================")
    print(df.to_string())

# tenkan-long sub-conditions for the closest bars (backtest-style h1)
strat = build(False)
f = strat.prepare_and_sign(m15, m15, h1)
h1e = h1.copy()
h1e["e20"] = ema20_of(h1)
print("\n--- tenkan-long sub-conditions (bt-style) for closest bars ---")
for B in ["2026-09-16 05:30", "2026-09-16 06:30", "2026-09-16 06:45", "2026-09-16 07:00", "2026-09-16 07:15"]:
    B = pd.Timestamp(B)
    r = f.loc[B]
    atr = float(r["atr"])
    dist = (r["close"] - r["tenkan"]) / atr
    body = r["close"] - r["open_"] if "open_" in r else None
    print(B.strftime("%H:%M"),
          "close", round(r["close"], 2),
          "| cloud", round(r["close"] > r["cloud_top"], 2) if False else (r["close"] > r["cloud_top"]),
          "| ema200", (r["close"] > r["ema_200"]),
          "| t>k", (r["tenkan"] > r["kijun"]),
          "| dist", round(dist, 2), "in[0.4,1.8]", 0.4 <= dist <= 1.8,
          "| h1bull_bt", bool(r["h1_trend_bull"]),
          "| body_bull", bool(r["close"] > r["open"]))
