"""PHASE 2.5b — MANAGEMENT-GRANULARITY LADDER (research-only).

Isolates ONE variable: the bar grid on which open positions are managed
(BE/trailing/SL/TP checks). Signals and sizing stay identical. Grids:
M15 (engine-equivalent) / M5 / M1 (closest available to live tick behaviour).

Window = M1 availability ∩ cached dataset = 2025-04-08 .. 2026-09-07 (~17 months).
CAVEAT (declared, not hidden): this window sits inside the FAVORABLE 2025-26
regime; the granularity effect may differ in bad years — not generalizable.

Config: shared account $482.53, both bots, SL 2.0 ATR, guard 3% (live config),
fixed 20pts spread (isolates granularity from the spread model).
"""
import json, pickle
from dataclasses import replace
import numpy as np
import pandas as pd
import MetaTrader5 as mt5

from core import Settings
from core.risk_manager import RiskConfig, RiskManager, SymbolSpec, PositionView
from backtest.engine import HistoricalNewsChecker
from strategies import build_from_settings

M5, M15, H1 = pickle.load(open("_sp2l_data_5y.pkl", "rb"))
SPEC = SymbolSpec.gold_default()
INIT = 482.53
SPREAD = 20.0 * 0.01
GUARD = 3.0
LEVERAGE = 100.0

# ---------------- fetch M1 from local terminal ----------------
print("fetching M1...", flush=True)
mt5.initialize()
raw = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M1, 0, 500000)
mt5.shutdown()
M1 = pd.DataFrame(raw)
M1["time"] = pd.to_datetime(M1["time"], unit="s")
M1 = M1.set_index("time").sort_index()
M1 = M1[~M1.index.duplicated(keep="last")]
print("M1 bars:", len(M1), M1.index[0], "->", M1.index[-1], flush=True)

# ---------------- align window ----------------
W0 = max(M1.index[0], M5.index[0])
W1 = min(M1.index[-1], M5.index[-1])
m5 = M5.loc[W0:W1].copy()
m15 = M15.loc[W0:W1].copy()
h1 = H1.loc[W0:W1].copy()
m1 = M1.loc[W0:W1].copy()
print("window:", W0, "->", W1, "| M5:", len(m5), "M1:", len(m1), flush=True)

def build(bot):
    s = Settings.load({"orb": "config/settings.yaml", "ichi": "config/settings_ichimoku.yaml"}[bot])
    strat = build_from_settings(s)
    rc = RiskConfig.from_settings(s)
    rc.sl_atr_multiplier = 2.0
    rc.max_forced_risk_percent = GUARD
    news = HistoricalNewsChecker(filepath="data/historical_news.json", before_minutes=5,
                                 after_minutes=5, enabled=True, server_utc_offset_hours=3.0,
                                 after_minutes_tier1=120,
                                 tier1_patterns=["non-farm", "nfp", "cpi", "fomc", "federal funds", "powell"])
    return strat, rc, news, s

ORB_STRAT, ORB_RC, ORB_NEWS, ORB_S = build("orb")
ICHI_STRAT, ICHI_RC, ICHI_NEWS, ICHI_S = build("ichi")
P_ORB = ORB_STRAT.prepare_and_sign(m5, None, None)
P_ICHI = ICHI_STRAT.prepare_and_sign(m15, m15, h1)
ORB_SIG = P_ORB["signal"]
ORB_ATR = P_ORB["atr"]
ICHI_SIG = P_ICHI["signal"]
ICHI_ATR = P_ICHI["atr"]
ICHI_END = P_ICHI.index + pd.Timedelta(minutes=15)
ORB_SESS = (int(ORB_S.get("session.start_hour", 0)), int(ORB_S.get("session.end_hour", 24)))
WEEKDAYS = (0, 1, 2, 3, 4)

class Pos:
    __slots__ = ("bot", "side", "lot", "entry", "sl", "tp", "atr", "be_done", "_last_15")
    def __init__(self, bot, side, lot, entry, sl, tp, atr):
        self.bot, self.side, self.lot, self.entry = bot, side, lot, entry
        self.sl, self.tp, self.atr, self.be_done = sl, tp, atr, False
        self._last_15 = -1

def run_grid(grid_name, grid):
    """grid: DataFrame with open/high/low/close, DatetimeIndex."""
    gi = grid.index
    O = grid["open"].to_numpy("float64"); H = grid["high"].to_numpy("float64")
    L = grid["low"].to_numpy("float64"); C = grid["close"].to_numpy("float64")
    # signal lookup helpers
    orb_pos = ORB_SIG.index.searchsorted(gi)          # count of M5 signal bars <= grid bar
    ichi_pos = ICHI_END.searchsorted(gi, side="right")
    orb_atr_on_grid = ORB_ATR.reindex(gi, method="ffill").to_numpy("float64")
    balance = INIT
    positions = {}
    trades = []
    peak_eq = INIT
    maxdd = 0.0
    guard_blocks = 0
    max_risk = 0.0
    ichi_used = None
    risk_mgr = {"orb": RiskManager(ORB_RC, SPEC), "ichi": RiskManager(ICHI_RC, SPEC)}

    def try_entry(bot, i, side, atr, news):
        nonlocal balance, guard_blocks, max_risk
        t = gi[i]
        if t.weekday() not in WEEKDAYS:
            return None
        if bot == "orb":
            s0, s1 = ORB_SESS
            if not (s0 <= t.hour < s1 if s1 > s0 else (t.hour >= s0 or t.hour < s1)):
                return None
        if news.is_blocked(t):
            return None
        entry = O[i] + SPREAD if side == "BUY" else O[i]
        sl_d = max(2.0 * atr, SPEC.price_from_points(max(ORB_RC.min_sl_points, 5.0)))
        tp_d = (ORB_RC if bot == "orb" else ICHI_RC).tp_atr_multiplier * atr
        lot, risk = risk_mgr[bot].calculate_lot(balance, sl_d)
        if lot <= 0:
            guard_blocks += 1
            return None
        fr = risk / balance * 100
        max_risk = max(max_risk, fr)
        if fr > GUARD:
            guard_blocks += 1
            return None
        margin = lot * SPEC.contract_size * entry / LEVERAGE
        if margin > balance:
            return None
        return Pos(bot, side, lot, entry,
                   entry - sl_d if side == "BUY" else entry + sl_d,
                   entry + tp_d if side == "BUY" else entry - tp_d, atr)

    for i in range(len(gi)):
        t = gi[i]
        exited = set()
        for key in list(positions.keys()):
            p = positions[key]
            bar_h, bar_l = H[i], L[i]
            if p.side == "BUY":
                if L[i] <= p.sl:
                    px = p.sl
                elif bar_h >= p.tp:
                    px = p.tp
                else:
                    px = None
            else:
                if (bar_h + SPREAD) >= p.sl:
                    px = p.sl
                elif (bar_l + SPREAD) <= p.tp:
                    px = p.tp
                else:
                    px = None
            if px is not None:
                d = (px - p.entry) if p.side == "BUY" else (p.entry - px)
                balance += (d / SPEC.tick_size) * SPEC.tick_value * p.lot
                trades.append({"bot": p.bot, "pnl": round((d / SPEC.tick_size) * SPEC.tick_value * p.lot, 2)})
                del positions[key]
                exited.add(key)
                continue
            rc = ORB_RC if p.bot == "orb" else ICHI_RC
            if p.side == "BUY":
                bb, ba = bar_h, bar_h + SPREAD
            else:
                bb, ba = bar_l, bar_l + SPREAD
            view = PositionView(ticket=0, side=p.side, volume=p.lot, price_open=p.entry,
                                sl=p.sl, tp=p.tp, breakeven_done=p.be_done)
            act = risk_mgr[p.bot].evaluate_position(view, bb, ba, p.atr)
            if act is not None:
                p.sl = act.new_sl
                p.be_done = True
        # entries
        for bot in ("ichi", "orb"):
            if bot in positions or bot in exited:
                continue
            if bot == "orb":
                k = orb_pos[i] - 1
                if k < 1:
                    continue
                sig = int(ORB_SIG.iloc[k])
                atr = float(orb_atr_on_grid[i]) if np.isfinite(orb_atr_on_grid[i]) else 0.0
                if sig != 0 and atr > 0:
                    p = try_entry(bot, i, "BUY" if sig > 0 else "SELL", atr, ORB_NEWS)
                    if p:
                        positions[bot] = p
            else:
                k = ichi_pos[i] - 1
                if k < 1 or k == ichi_used:
                    continue
                sig = int(ICHI_SIG.iloc[k])
                atrv = ICHI_ATR.iloc[k]
                atr = float(atrv) if pd.notna(atrv) else 0.0
                if sig != 0 and atr > 0:
                    p = try_entry(bot, i, "BUY" if sig > 0 else "SELL", atr, ICHI_NEWS)
                    if p:
                        positions[bot] = p
                    ichi_used = k
        floating = 0.0
        for p in positions.values():
            d = (C[i] - p.entry) if p.side == "BUY" else (p.entry - C[i])
            floating += (d / SPEC.tick_size) * SPEC.tick_value * p.lot
        eq = balance + floating
        peak_eq = max(peak_eq, eq)
        maxdd = max(maxdd, peak_eq - eq)

    nets = [x["pnl"] for x in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    return {
        "grid": grid_name, "bars": len(gi), "trades": len(trades),
        "net": round(sum(nets), 2),
        "net_ichi": round(sum(x["pnl"] for x in trades if x["bot"] == "ichi"), 2),
        "net_orb": round(sum(x["pnl"] for x in trades if x["bot"] == "orb"), 2),
        "pf": round(sum(wins) / abs(sum(losses)), 3) if losses and sum(losses) < 0 else None,
        "wr": round(len(wins) / max(1, len(nets)) * 100, 1),
        "eq_dd_of_peak_pct": round(maxdd / peak_eq * 100, 1) if peak_eq else None,
        "guard_blocks": guard_blocks, "max_trade_risk_pct": round(max_risk, 2),
    }

res = []
for name, g in (("M15", m15), ("M5", m5), ("M1", m1)):
    out = run_grid(name, g)
    res.append(out)
    print(json.dumps(out), flush=True)
json.dump(res, open("_phase25b_granularity.json", "w"), indent=1)
print("DONE", flush=True)
