"""PHASE 2.5 — SHARED-ACCOUNT SIMULATION (research-only; nothing committed to production).

One time-ordered M5 replay where BOTH bots trade a SINGLE shared account:
  - sizing from the shared running balance (per-bot risk configs)
  - concurrent positions (max 1 per bot), HEDGING mode assumed (explicit assumption)
  - margin tracked under an explicit 1:100 leverage assumption
  - floating equity marked at each bar close (equity DD includes floating)
  - BE/trailing via each bot's live RiskManager.evaluate_position, next-bar rule
  - SL-first pessimism; V3 adds gap-through-SL fill at open
  - guard 3% evaluated against the SHARED balance (research on/off)
  - entry-order sensitivity: ichi-first vs orb-first on same-bar collisions

Variants (each compared separately, no stacking without reporting):
  V1  shared account, fixed 20pts spread, M5-grid management both bots
  V1c V1 but ichimoku managed on the M15 grid (isolates management-granularity)
  V2  V1 + time-varying demo-feed spread (per-bar `spread` column, no future info)
  V3  V2 + pessimistic gap-through-SL (fill at open when open is beyond SL)
"""
import json, math, pickle
import numpy as np
import pandas as pd

from core import Settings
from core.risk_manager import RiskConfig, RiskManager, SymbolSpec, PositionView
from backtest.engine import HistoricalNewsChecker
from strategies import build_from_settings

M5, M15, H1 = pickle.load(open("_sp2l_data_5y.pkl", "rb"))
SPEC = SymbolSpec.gold_default()
INIT = 482.53          # current live balance
SPREAD_FIXED = 20.0    # pts (demo-percentile scenario, NOT broker measurement)
LEVERAGE = 100.0       # explicit assumption; WMMarkets leverage unknown

def build(bot):
    s = Settings.load({"orb": "config/settings.yaml", "ichi": "config/settings_ichimoku.yaml"}[bot])
    strat = build_from_settings(s)
    rc = RiskConfig.from_settings(s)
    # VPS live config: SL 2.0 ATR (matches live banners)
    rc.sl_atr_multiplier = 2.0
    news = HistoricalNewsChecker(
        filepath="data/historical_news.json", before_minutes=5, after_minutes=5,
        enabled=True, server_utc_offset_hours=3.0, after_minutes_tier1=120,
        tier1_patterns=["non-farm", "nfp", "cpi", "fomc", "federal funds", "powell"])
    return strat, rc, news, s

ORB_STRAT, ORB_RC, ORB_NEWS, ORB_S = build("orb")
ICHI_STRAT, ICHI_RC, ICHI_NEWS, ICHI_S = build("ichi")

print("preparing ORB signals...", flush=True)
P_ORB = ORB_STRAT.prepare_and_sign(M5, None, None)
print("preparing ICHI signals...", flush=True)
P_ICHI = ICHI_STRAT.prepare_and_sign(M15, M15, H1)

ORB_SIG = P_ORB["signal"].to_numpy("int8")
ORB_IDX = P_ORB.index
ICHI_SIG = P_ICHI["signal"].to_numpy("int8")
ICHI_IDX = P_ICHI.index
ICHI_END = ICHI_IDX + pd.Timedelta(minutes=15)   # M15 bar close times
M5I = M5.index
SPREAD_COL = M5["spread"].to_numpy("float64") * 0.01  # price distance, known at bar close
O = M5["open"].to_numpy("float64"); H = M5["high"].to_numpy("float64")
L = M5["low"].to_numpy("float64"); C = M5["close"].to_numpy("float64")
ORB_ATR = P_ORB["atr"].reindex(M5I).ffill().to_numpy("float64")
ICHI_ATR_M15 = P_ICHI["atr"]

ORB_SESS = (int(ORB_S.get("session.start_hour", 0)), int(ORB_S.get("session.end_hour", 24)))
WEEKDAYS = (0, 1, 2, 3, 4)

class Pos:
    __slots__ = ("bot", "side", "lot", "entry", "sl", "tp", "atr", "be_done", "entry_time", "_last_m15")
    def __init__(self, bot, side, lot, entry, sl, tp, atr, t):
        self.bot, self.side, self.lot, self.entry = bot, side, lot, entry
        self.sl, self.tp, self.atr, self.be_done = sl, tp, atr, False
        self.entry_time = t
        self._last_m15 = -1

def sim(guard_on, order, variant):
    guard = 3.0 if guard_on else 0.0
    balance = INIT
    positions = {}
    trades = []
    eq_series = []
    maxdd_eq = 0.0
    peak_eq = INIT
    max_risk_pct = 0.0
    peak_margin = 0.0
    margin_shortfalls = 0
    guard_blocks = 0
    ichi_last_used = None
    risk_mgr = {"orb": RiskManager(ORB_RC, SPEC), "ichi": RiskManager(ICHI_RC, SPEC)}

    def spread_at(i):
        if variant in ("V2", "V3"):
            sp = SPREAD_COL[i]
            return sp if np.isfinite(sp) and sp > 0 else 0.20
        return SPREAD_FIXED * 0.01

    def try_entry(bot, i, side, atr, news, rc):
        nonlocal balance, guard_blocks, margin_shortfalls, peak_margin, max_risk_pct
        t = M5I[i]
        if t.weekday() not in WEEKDAYS:
            return None
        if bot == "orb":
            s0, s1 = ORB_SESS
            if not (s0 <= t.hour < s1 if s1 > s0 else (t.hour >= s0 or t.hour < s1)):
                return None
        if news.is_blocked(t):
            return None
        sp = spread_at(i)
        entry = O[i] + sp if side == "BUY" else O[i]
        sl_d = rc.sl_atr_multiplier * atr
        sl_d = max(sl_d, SPEC.price_from_points(max(rc.min_sl_points, SPEC.stops_level_points + 5)))
        tp_d = rc.tp_atr_multiplier * atr
        lot, risk = risk_mgr[bot].calculate_lot(balance, sl_d)
        if lot <= 0:
            guard_blocks += 1
            return None
        forced = risk / balance * 100
        max_risk_pct = max(max_risk_pct, forced)
        if guard_on and forced > guard:
            guard_blocks += 1
            return None
        margin = lot * SPEC.contract_size * entry / LEVERAGE
        peak_margin = max(peak_margin, margin + sum(
            p.lot * SPEC.contract_size * p.entry / LEVERAGE for p in positions.values()))
        if margin > balance:
            margin_shortfalls += 1
            return None
        sl = entry - sl_d if side == "BUY" else entry + sl_d
        tp = entry + tp_d if side == "BUY" else entry - tp_d
        return Pos(bot, side, lot, entry, sl, tp, atr, t)

    n = len(M5I)
    for i in range(300, n):
        t = M5I[i]
        sp = spread_at(i)
        # ---------------- manage open positions (SL-first, pessimistic) -------
        for key in list(positions.keys()):
            p = positions[key]
            # manage on M5 grid; V1c: ichi managed only when a new M15 bar closed
            if variant == "V1c" and p.bot == "ichi":
                m15_closed = ICHI_END.searchsorted(t, side="right")
                if m15_closed == getattr(p, "_last_m15", -1):
                    continue
                p._last_m15 = m15_closed
            bar_h, bar_l = H[i], L[i]
            gap_fill = False
            if p.side == "BUY":
                sl_hit = bar_l <= p.sl
                tp_hit = bar_h >= p.tp
                if variant == "V3" and O[i] < p.sl:
                    exit_price, gap_fill = O[i], True
                elif sl_hit:
                    exit_price = p.sl
                elif tp_hit:
                    exit_price = p.tp
                else:
                    exit_price = None
            else:
                sl_hit = (bar_h + sp) >= p.sl
                tp_hit = (bar_l + sp) <= p.tp
                if variant == "V3" and (O[i] + sp) > p.sl:
                    exit_price, gap_fill = O[i] + sp, True
                elif sl_hit:
                    exit_price = p.sl
                elif tp_hit:
                    exit_price = p.tp
                else:
                    exit_price = None
            if exit_price is not None:
                d = (exit_price - p.entry) if p.side == "BUY" else (p.entry - exit_price)
                pnl = SPEC.money_per_lot(d) * p.lot
                balance += pnl
                trades.append({"bot": p.bot, "side": p.side, "t": str(t), "pnl": round(pnl, 2),
                               "reason": "gap" if gap_fill else ("sl" if sl_hit and not tp_hit else ("tp" if tp_hit else "sl"))})
                del positions[key]
                continue
            # -------- BE/trailing: bar-best price, next-bar effect ------------
            rc = ORB_RC if p.bot == "orb" else ICHI_RC
            if p.side == "BUY":
                best_bid, best_ask = bar_h, bar_h + sp
            else:
                best_bid, best_ask = bar_l, bar_l + sp
            view = PositionView(ticket=0, side=p.side, volume=p.lot, price_open=p.entry,
                                sl=p.sl, tp=p.tp, breakeven_done=p.be_done)
            act = risk_mgr[p.bot].evaluate_position(view, best_bid, best_ask, p.atr)
            if act is not None:
                p.sl = act.new_sl
                if act.kind == "trailing":
                    p.be_done = True
                else:
                    p.be_done = True
        # ---------------- new entries (previous closed bar signals) ----------
        for bot in (order):
            if bot in positions:
                continue
            if bot == "orb":
                pos_in_orb = ORB_IDX.searchsorted(t)
                if pos_in_orb < 1:
                    continue
                sig = int(ORB_SIG[pos_in_orb - 1])
                atr = float(ORB_ATR[i]) if np.isfinite(ORB_ATR[i]) else 0.0
                if sig != 0 and atr > 0:
                    p = try_entry(bot, i, "BUY" if sig > 0 else "SELL", atr, ORB_NEWS, ORB_RC)
                    if p:
                        positions[bot] = p
            else:
                m15_closed = ICHI_END.searchsorted(t, side="right")
                if m15_closed < 1 or m15_closed == ichi_last_used:
                    continue
                sig = int(ICHI_SIG[m15_closed - 1])
                atr_raw = ICHI_ATR_M15.iloc[m15_closed - 1]
                atr = float(atr_raw) if pd.notna(atr_raw) else 0.0
                if sig != 0 and atr > 0:
                    p = try_entry(bot, i, "BUY" if sig > 0 else "SELL", atr, ICHI_NEWS, ICHI_RC)
                    if p:
                        positions[bot] = p
                    ichi_last_used = m15_closed
        # ---------------- equity mark (floating) ------------------------------
        floating = 0.0
        for p in positions.values():
            mark = C[i]
            d = (mark - p.entry) if p.side == "BUY" else (p.entry - mark)
            floating += SPEC.money_per_lot(d) * p.lot
        eq = balance + floating
        peak_eq = max(peak_eq, eq)
        maxdd_eq = max(maxdd_eq, peak_eq - eq)
        eq_series.append(eq)

    nets = [t_["pnl"] for t_ in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    out = {
        "variant": variant, "guard": "ON" if guard_on else "OFF", "order": order,
        "trades_total": len(trades),
        "trades_orb": sum(1 for t_ in trades if t_["bot"] == "orb"),
        "trades_ichi": sum(1 for t_ in trades if t_["bot"] == "ichi"),
        "net": round(sum(nets), 2),
        "net_orb": round(sum(t_["pnl"] for t_ in trades if t_["bot"] == "orb"), 2),
        "net_ichi": round(sum(t_["pnl"] for t_ in trades if t_["bot"] == "ichi"), 2),
        "pf": round(sum(wins) / abs(sum(losses)), 3) if losses and sum(losses) < 0 else None,
        "win_rate": round(len(wins) / max(1, len(nets)) * 100, 1),
        "max_dd_equity_usd": round(maxdd_eq, 2),
        "max_dd_equity_pct_of_init": round(maxdd_eq / INIT * 100, 1),
        "peak_margin_usage_usd": round(peak_margin, 2),
        "margin_shortfall_events": margin_shortfalls,
        "guard_blocks": guard_blocks,
    }
    return out, trades

def main():
    results = []
    all_runs = []
    for variant in ("V1", "V1c", "V2", "V3"):
        for guard_on in (True, False):
            for order in (("ichi", "orb"), ("orb", "ichi")):
                out, trades = sim(guard_on, order, variant)
                results.append(out)
                all_runs.append(out)
                print(json.dumps(out), flush=True)
    json.dump(results, open("_phase25_shared_results.json", "w"), indent=1)
    print("DONE", flush=True)

if __name__ == "__main__":
    main()
