# _incident_before_after.py — (A) before/after on the incident window with the
# FIXED live call path; (B) limited 10-day comparison beyond the event.
import pandas as pd
from loguru import logger

logger.remove()
from core import Settings
from strategies import build_from_settings

m15 = pd.read_csv("_ichi_out/incident_m15_metaquotes.csv", index_col=0, parse_dates=True)
h1 = pd.read_csv("_ichi_out/incident_h1_metaquotes.csv", index_col=0, parse_dates=True)

s = Settings.load("config/settings_ichimoku.yaml")
s.set("strategy.params.ichimoku_m15.h1_ema_period", 20)
s.set("strategy.params.ichimoku_m15.enable_kijun_short", False)
s.set("strategy.params.ichimoku_m15.enable_tenkan_short", True)
strat = build_from_settings(s)


def live_eval(B, fixed):
    """Emulate the live decision for M15 bar B (decision at B+15min)."""
    T = B + pd.Timedelta(minutes=15)
    m = m15.loc[:B]
    if fixed:
        h = h1.loc[: T.floor("h")]          # forming candle KEPT (the fix)
    else:
        h = h1.loc[: T.floor("h") - pd.Timedelta(hours=1)]  # old double-drop
    f = strat.prepare_and_sign(m, m, h)
    r = f.iloc[-1]
    return bool(r["h1_trend_bull"]), int(r["signal"]), str(r.get("signal_layer", ""))


# ---------- A) incident window before/after ----------
print("=== A) incident window: h1_bull old vs fixed, signals ===")
rows = []
for B in pd.date_range("2026-09-16 04:45", "2026-09-16 08:00", freq="15min"):
    b_old, s_old, l_old = live_eval(B, fixed=False)
    b_new, s_new, l_new = live_eval(B, fixed=True)
    rows.append({"bar": B.strftime("%H:%M"), "h1_old": b_old, "h1_fixed": b_new,
                 "sig_old": s_old, "sig_fixed": s_new})
print(pd.DataFrame(rows).set_index("bar").to_string())

# ---------- B) limited comparison: last 10 days ----------
print("\n=== B) 10-day comparison (all closed M15 bars, old vs fixed) ===")
start = m15.index[-1] - pd.Timedelta(days=10)
diff_bull, diff_sig, total = 0, 0, 0
sig_changes = []
for B in m15.index[m15.index >= start]:
    if B + pd.Timedelta(minutes=15) > h1.index[-1] + pd.Timedelta(hours=1):
        continue
    b_old, s_old, l_old = live_eval(B, fixed=False)
    b_new, s_new, l_new = live_eval(B, fixed=True)
    total += 1
    if b_old != b_new:
        diff_bull += 1
    if s_old != s_new:
        diff_sig += 1
        sig_changes.append((str(B), s_old, s_new, l_old, l_new))
print(f"bars compared: {total} | h1_bull differs: {diff_bull} ({diff_bull/max(1,total)*100:.1f}%) "
      f"| final signal differs: {diff_sig}")
for c in sig_changes[:20]:
    print("signal change:", c)
