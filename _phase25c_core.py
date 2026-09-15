"""PHASE 2.5c — testable shared-account simulation core (research-only).

simulate() is a PURE function over frames -> results, designed for unit tests.
Key fixes vs 2.5:
  - SPREAD TIMING: an entry at bar i's open uses the spread of bar i-1 (the
    last value KNOWN before the fill). The bar's own `spread` field may be
    stamped at bar close -> using it for the same bar's open would leak.
  - GAP FILL: when the bar OPEN is already beyond SL, the exit fills at the
    open (an execution ASSUMPTION, not a guaranteed worst price).
Equity is sampled at bar CLOSE only (no intrabar marks) — declared limitation.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from core.risk_manager import PositionView, RiskManager, SymbolSpec


@dataclass
class SimConfig:
    capital: float
    bots: List[str]                      # subset of {"orb", "ichi"}
    shared: bool                         # True: one account for all bots
    guard_pct: float                     # forced-risk cap; 0 = off
    spread_pts: float = 20.0             # fixed spread (points); 0 = use bar spread (prev bar)
    gap_fill_at_open: bool = True
    leverage: float = 100.0
    max_positions_per_bot: int = 1


@dataclass
class SimResult:
    trades: List[dict] = field(default_factory=list)
    equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    balance: float = 0.0
    guard_blocks: int = 0
    margin_rejects: int = 0
    max_trade_risk_pct: float = 0.0
    dd: dict = field(default_factory=dict)


def _dd_details(eq: pd.Series, initial: float) -> dict:
    if eq.empty:
        return {}
    peak = eq.cummax()
    dd = eq - peak
    trough_i = int(dd.values.argmin())
    peak_i = int(np.argmax(eq.values[: trough_i + 1]))
    dd_usd = float(-dd.values[trough_i])
    return {
        "dd_amount_usd": round(dd_usd, 2),
        "dd_pct_of_peak": round(dd_usd / float(eq.values[peak_i]) * 100, 2) if eq.values[peak_i] > 0 else None,
        "dd_amount_over_initial_capital": round(dd_usd / initial * 100, 2),   # SEPARATE metric, not "MaxDD%"
        "peak_date": str(eq.index[peak_i]), "peak_equity": round(float(eq.values[peak_i]), 2),
        "trough_date": str(eq.index[trough_i]), "trough_equity": round(float(eq.values[trough_i]), 2),
        "min_equity": round(float(eq.min()), 2),
        "sampling": "equity at bar CLOSE only; intrabar fluctuations not marked",
    }


class _Pos:
    __slots__ = ("bot", "side", "lot", "entry", "sl", "tp", "atr", "be_done", "t")
    def __init__(self, bot, side, lot, entry, sl, tp, atr):
        self.bot, self.side, self.lot, self.entry = bot, side, lot, entry
        self.sl, self.tp, self.atr, self.be_done = sl, tp, atr, False
        self.t = None


def simulate(m5, m15, h1, bot_ctx, cfg: SimConfig) -> SimResult:
    """bot_ctx: {bot: {sig: pd.Series(int), atr: pd.Series(float), rc: RiskConfig,
                        news: checker, sess: (start,end) or None}}"""
    spec = SymbolSpec.gold_default()
    res = SimResult()
    if cfg.shared:
        balances = {b: cfg.capital for b in cfg.bots}      # one shared pot
        shared_balance = cfg.capital
    else:
        balances = {b: cfg.capital for b in cfg.bots}      # independent pots (only 1 bot expected)
    positions: Dict[str, _Pos] = {}
    last_consumed: Dict[str, int] = {}
    rm = {b: RiskManager(bot_ctx[b]["rc"], spec) for b in cfg.bots}
    equity_vals, equity_idx = [], []

    gi = m5.index
    O = m5["open"].to_numpy(float); H = m5["high"].to_numpy(float)
    L = m5["low"].to_numpy(float); C = m5["close"].to_numpy(float)
    bar_spread = None
    if "spread" in m5.columns:
        bar_spread = m5["spread"].to_numpy(float) * spec.point

    def bal_now(bot):
        return shared_balance if cfg.shared else balances[bot]

    def add_bal(bot, d):
        nonlocal shared_balance
        if cfg.shared:
            shared_balance += d
        else:
            balances[bot] += d

    sig_close = {b: bot_ctx[b].get("sig_close",
                                   bot_ctx[b]["sig"].index + pd.Timedelta(minutes=5))
                 for b in cfg.bots}
    # a signal is actionable only AFTER its bar CLOSES: last closed signal bar per grid bar
    sig_pos = {b: sig_close[b].searchsorted(gi, side="right") for b in cfg.bots}
    atr_ff = {b: bot_ctx[b]["atr"].reindex(gi, method="ffill") for b in cfg.bots}

    def check_exit(p, i):
        """SL/TP/gap check for bar i (BUY: bid side; SELL: ask side + spread)."""
        if p.side == "BUY":
            if cfg.gap_fill_at_open and O[i] < p.sl:
                return O[i], "gap"
            if L[i] <= p.sl:
                return p.sl, "sl"
            if H[i] >= p.tp:
                return p.tp, "tp"
        else:
            if cfg.gap_fill_at_open and (O[i] + sp) > p.sl:
                return O[i] + sp, "gap"
            if (H[i] + sp) >= p.sl:
                return p.sl, "sl"
            if (L[i] + sp) <= p.tp:
                return p.tp, "tp"
        return None, None

    def settle(p, i, px, reason):
        d = (px - p.entry) if p.side == "BUY" else (p.entry - px)
        pnl = (d / spec.tick_size) * spec.tick_value * p.lot
        add_bal(p.bot, pnl)
        res.trades.append({"bot": p.bot, "side": p.side, "entry_time": str(p.t),
                           "time": str(gi[i]), "pnl": round(pnl, 2), "reason": reason})

    for i in range(len(gi)):
        t = gi[i]
        sp = (cfg.spread_pts * spec.point) if cfg.spread_pts > 0 else (
            bar_spread[i - 1] if (bar_spread is not None and i > 0 and np.isfinite(bar_spread[i - 1])) else 0.20)
        exited = set()
        for key in list(positions.keys()):
            p = positions[key]
            px, reason = check_exit(p, i)
            if px is not None:
                settle(p, i, px, reason)
                del positions[key]
                exited.add(key)
                continue
            bb, ba = (H[i], H[i] + sp) if p.side == "BUY" else (L[i], L[i] + sp)
            view = PositionView(ticket=0, side=p.side, volume=p.lot, price_open=p.entry,
                                sl=p.sl, tp=p.tp, breakeven_done=p.be_done)
            act = rm[key].evaluate_position(view, bb, ba, p.atr)
            if act is not None:
                p.sl = act.new_sl
                p.be_done = True
        # ---- entries (signal consumed ONCE per signal bar — live evaluates once) ----
        for bot in cfg.bots:
            if bot in positions or bot in exited:
                continue
            k = sig_pos[bot][i] - 1
            if k < 1 or k == last_consumed.get(bot):
                continue
            last_consumed[bot] = k   # consume regardless of outcome (live fires once)
            sig = int(bot_ctx[bot]["sig"].iloc[k])
            if sig == 0:
                continue
            atrv = atr_ff[bot].iloc[i]
            atr = float(atrv) if pd.notna(atrv) else 0.0
            if atr <= 0:
                continue
            if t.weekday() > 4:
                continue
            sess = bot_ctx[bot].get("sess")
            if sess:
                s0, s1 = sess
                if not (s0 <= t.hour < s1 if s1 > s0 else (t.hour >= s0 or t.hour < s1)):
                    continue
            if bot_ctx[bot]["news"] is not None and bot_ctx[bot]["news"].is_blocked(t):
                continue
            side = "BUY" if sig > 0 else "SELL"
            rc = bot_ctx[bot]["rc"]
            entry = O[i] + sp if side == "BUY" else O[i]
            sl_d = max(rc.sl_atr_multiplier * atr,
                       spec.price_from_points(max(rc.min_sl_points, spec.stops_level_points + 5)))
            tp_d = rc.tp_atr_multiplier * atr
            lot, risk = rm[bot].calculate_lot(bal_now(bot), sl_d)
            if lot <= 0:
                res.guard_blocks += 1
                continue
            forced = risk / bal_now(bot) * 100
            res.max_trade_risk_pct = max(res.max_trade_risk_pct, forced)
            if cfg.guard_pct > 0 and forced > cfg.guard_pct:
                res.guard_blocks += 1
                continue
            margin = lot * spec.contract_size * entry / cfg.leverage
            if margin > bal_now(bot):
                res.margin_rejects += 1
                continue
            p = _Pos(bot, side, lot, entry,
                     entry - sl_d if side == "BUY" else entry + sl_d,
                     entry + tp_d if side == "BUY" else entry - tp_d, atr)
            p.t = t
            positions[bot] = p
            # ENGINE PARITY: the entry bar's own range can stop the position
            # out (engine passes entry_bar=True). BE/trailing still deferred
            # to the next bar (next-bar rule).
            px, reason = check_exit(p, i)
            if px is not None:
                settle(p, i, px, reason)
                del positions[bot]
                exited.add(bot)
        # ---- equity at bar close ----
        floating = 0.0
        for p in positions.values():
            d = (C[i] - p.entry) if p.side == "BUY" else (p.entry - C[i])
            floating += (d / spec.tick_size) * spec.tick_value * p.lot
        base = shared_balance if cfg.shared else sum(balances.values())
        equity_vals.append(base + floating)
        equity_idx.append(t)

    res.equity = pd.Series(equity_vals, index=pd.DatetimeIndex(equity_idx))
    res.balance = shared_balance if cfg.shared else sum(balances.values())
    total_cap = cfg.capital if cfg.shared else cfg.capital * len(cfg.bots)
    res.dd = _dd_details(res.equity, total_cap)
    return res
