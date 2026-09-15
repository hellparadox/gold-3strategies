"""PHASE-2 DELIVERABLES (research-only; no production changes, no optimization).

Produces in _phase2_out/:
  TRADE_LIST.csv        — full per-trade rows (C/D @ $533 & $482.53, S2; A@S2 ichi)
  REJECTED_SIGNALS.csv  — guard-blocked entries (from C-vs-D path diff), hypothetical outcome = side info
  DAILY_EQUITY.csv      — daily realized P&L + equity (per bot, combined; shared $533)
  BASELINE_RESULTS.csv  — all runs from _phase2_results.json flattened
  COST_SCENARIOS.csv    — spread sweep (C config)
  BALANCE_SWEEP.csv     — ichi C@S2 initial balance $400..$800 step $25 (discontinuity test)
  GAP_ANALYSIS.csv      — gap duration buckets + gap-days-removed QC runs
  EXPECTANCY_R.csv      — full-precision R stats (mean-of-ratios AND aggregate ratio)
  PORTFOLIO_CHECK.json  — combined Sharpe/DD verification (both day-handling conventions)
  equity charts         — PNG per bot + combined
"""
import json, math, os, pickle, sys
import numpy as np
import pandas as pd

from core import Settings
from core.risk_manager import RiskConfig, SymbolSpec
from backtest.engine import BacktestConfig, BacktestEngine
from strategies import build_from_settings

OUT = "_phase2_out"
os.makedirs(OUT, exist_ok=True)
M5, M15, H1 = pickle.load(open("_sp2l_data_5y.pkl", "rb"))
SPEC = SymbolSpec.gold_default()
BOTS = {"orb": "config/settings.yaml", "ichi": "config/settings_ichimoku.yaml"}
INIT_SHARED = 533.0

def engine_for(bot, risk_over, bt_over, balance=None):
    s = Settings.load(BOTS[bot])
    for k, v in risk_over.items():
        s.set(k, v)
    bt = {"backtest.commission_per_lot": 0.0, "session.max_spread_points": 999.0,
          "backtest.simulate_breakeven": True, "backtest.simulate_trailing": False,
          "backtest.simulate_partial": False,
          "backtest.spread_points": 20.0, "backtest.slippage_points": 0.0}
    bt.update(bt_over)
    for k, v in bt.items():
        s.set(k, v)
    if balance is not None:
        s.set("backtest.initial_balance", float(balance))
    return BacktestEngine(build_from_settings(s), RiskConfig.from_settings(s),
                          BacktestConfig.from_settings(s), SPEC, symbol="XAUUSD",
                          timeframe="M5"), s

def layer_map(bot):
    """entry_time -> signal_layer from the strategy's own prepare (post-cooldown)."""
    s = Settings.load(BOTS[bot])
    strat = build_from_settings(s)
    if bot == "orb":
        p = strat.prepare_and_sign(M5, None, None)
        return {ts: l for ts, l in zip(p.index, p["signal_layer"])}
    p = strat.prepare_and_sign(M15, M15, H1)
    return {ts: l for ts, l in zip(p.index, p["signal_layer"])}

def rows_from(res, run_id, strategy, balance0, spread_pts, slip_pts, lmap):
    rows, bal = [], balance0
    for t in res.trades:
        risk0 = SPEC.money_per_lot(t.initial_sl_distance) * t.lot  # + commission (0 on VPS)
        slip_apps = 1 + (1 if t.exit_reason in ("sl", "trailing", "breakeven") else 0)
        layer = lmap.get(t.entry_time, "")
        rows.append({
            "run_id": run_id, "strategy": strategy,
            "entry_time": t.entry_time, "exit_time": t.exit_time,
            "side": t.side, "entry": t.entry_price, "exit": t.exit_price,
            "lot": t.lot, "sl": t.sl, "tp": t.tp,
            "gross_pnl": round(t.gross_profit, 2),
            "spread_cost_derived": round(spread_pts * t.lot, 2),   # one full spread per round trip, inside gross
            "slippage_cost_derived": round(slip_pts * slip_apps * t.lot, 2),
            "commission": round(t.commission, 2), "swap": 0.0, "fee": 0.0,
            "net_pnl": round(t.net_profit, 2),
            "balance_before": round(bal, 2), "balance_after": round(t.balance_after, 2),
            "exit_reason": t.exit_reason, "signal_layer": layer,
            "initial_risk_usd": round(risk0, 2),
            "r_multiple": round(t.net_profit / risk0, 4) if risk0 > 0 else None,
            "held_overnight": int(pd.Timestamp(t.entry_time).date() != pd.Timestamp(t.exit_time).date()),
        })
        bal = t.balance_after
    return rows

def qmetrics(trades_df):
    n = len(trades_df)
    if n == 0:
        return {}
    nets = trades_df["net_pnl"].values
    wins, losses = nets[nets > 0], nets[nets < 0]
    eq = trades_df["balance_after"].values
    peak = np.maximum.accumulate(np.concatenate([[INIT_SHARED], eq]))[1:]
    dd = peak - eq
    m = {
        "trades": n, "net": round(float(nets.sum()), 2),
        "pf": round(float(wins.sum() / abs(losses.sum())), 4) if len(losses) and losses.sum() < 0 else None,
        "win_rate": round(len(wins) / n * 100, 2),
        "expectancy_usd": round(float(nets.mean()), 4),
        "expR_mean_of_ratios": round(float(trades_df["r_multiple"].mean()), 6),
        "expR_aggregate": round(float(nets.sum() / trades_df["initial_risk_usd"].sum()), 6),
        "max_dd_usd": round(float(dd.max()), 2),
    }
    return m

def main():
    report = {}

    # ---------- full-capture runs ----------
    lmaps = {b: layer_map(b) for b in BOTS}
    RUNS = [
        ("C533", {"risk.max_forced_risk_percent": 3.0, "risk.sl_atr_multiplier": 2.0}, None),
        ("D533", {"risk.max_forced_risk_percent": 0.0, "risk.sl_atr_multiplier": 2.0}, None),
        ("C482", {"risk.max_forced_risk_percent": 3.0, "risk.sl_atr_multiplier": 2.0}, 482.53),
        ("D482", {"risk.max_forced_risk_percent": 0.0, "risk.sl_atr_multiplier": 2.0}, 482.53),
    ]
    all_rows, res_store = [], {}
    for bot in ("orb", "ichi"):
        for rid, risk_over, bal in RUNS:
            eng, _ = engine_for(bot, risk_over, {}, balance=bal)
            res = eng.run(M5, M15, H1)
            b0 = bal if bal else 533.0
            all_rows += rows_from(res, f"{rid}|{bot}|S2", bot, b0, 20.0, 0.0, lmaps[bot])
            res_store[f"{rid}|{bot}"] = res
            print(f"captured {rid}|{bot}: {len(res.trades)} trades", flush=True)
    # A-config ichi (SL 1.6, guard off) for expectancy_R point
    eng, _ = engine_for("ichi", {"risk.max_forced_risk_percent": 0.0}, {})
    res = eng.run(M5, M15, H1)
    all_rows += rows_from(res, "A533|ichi|S2", "ichi", 533.0, 20.0, 0.0, lmaps["ichi"])
    print(f"captured A533|ichi: {len(res.trades)} trades", flush=True)

    TL = pd.DataFrame(all_rows)
    TL.to_csv(f"{OUT}/TRADE_LIST.csv", index=False)

    # ---------- REJECTED_SIGNALS (guard path-diff; hypothetical = side info) ----------
    rej = []
    for bot in ("orb", "ichi"):
        D = TL[(TL.run_id == f"D533|{bot}|S2")]
        C = set(zip(TL[TL.run_id == f"C533|{bot}|S2"].entry_time, TL[TL.run_id == f"C533|{bot}|S2"].side))
        for _, r in D.iterrows():
            if (r.entry_time, r.side) not in C:
                rej.append({"run": f"C533|{bot}|S2", "entry_time": r.entry_time, "side": r.side,
                            "hypothetical_net_pnl_IF_EXECUTED": r.net_pnl,
                            "note": "path-counterfactual only — not additive evidence"})
    for bot in ("orb", "ichi"):
        D = TL[(TL.run_id == f"D482|{bot}|S2")]
        C = set(zip(TL[TL.run_id == f"C482|{bot}|S2"].entry_time, TL[TL.run_id == f"C482|{bot}|S2"].side))
        for _, r in D.iterrows():
            if (r.entry_time, r.side) not in C:
                rej.append({"run": f"C482|{bot}|S2", "entry_time": r.entry_time, "side": r.side,
                            "hypothetical_net_pnl_IF_EXECUTED": r.net_pnl,
                            "note": "path-counterfactual only"})
    pd.DataFrame(rej).to_csv(f"{OUT}/REJECTED_SIGNALS.csv", index=False)
    print(f"rejected rows: {len(rej)}", flush=True)

    # ---------- guard path comparison (point 4) ----------
    guard_cmp = {}
    for bot in ("orb", "ichi"):
        for bal in (533.0, 482.53):
            tag = f"{int(bal)}"
            c = TL[(TL.run_id == f"C{tag}|{bot}|S2")]
            d = TL[(TL.run_id == f"D{tag}|{bot}|S2")]
            guard_cmp[f"{bot}_bal{tag}"] = {
                "guard_ON": qmetrics(c), "guard_OFF": qmetrics(d),
                "blocked_count": len(d) - len(c),
            }
    report["guard_path_comparison"] = guard_cmp

    # ---------- point 5: balance sensitivity & first divergence (ichi C 533 vs 482) ----------
    c5 = TL[TL.run_id == "C533|ichi|S2"].reset_index()
    c4 = TL[TL.run_id == "C482|ichi|S2"].reset_index()
    div = None
    for i in range(min(len(c5), len(c4))):
        if c5.entry_time[i] != c4.entry_time[i] or c5.side[i] != c4.side[i]:
            t5 = c5.iloc[i - 1] if i > 0 else None
            prev_bal5 = c5.balance_after[i - 1] if i > 0 else 533.0
            prev_bal4 = c4.balance_after[i - 1] if i > 0 else 482.53
            nxt = c5.iloc[i] if i < len(c5) else None
            nxt4 = c4.iloc[i] if i < len(c4) else None
            div = {
                "index": i,
                "trade_before_both_shared": {"entry_time": str(c5.entry_time[i - 1]), "side": c5.side[i - 1],
                                             "bal533_after": float(c5.balance_after[i - 1]),
                                             "bal482_after": float(c4.balance_after[i - 1])},
                "next_C533_trade": {"entry_time": str(nxt.entry_time), "side": nxt.side,
                                    "initial_risk": float(nxt.initial_risk_usd),
                                    "forced_pct_533": round(float(nxt.initial_risk_usd) / float(prev_bal5) * 100, 2)},
                "next_C482_trade": ({"entry_time": str(nxt4.entry_time), "side": nxt4.side,
                                     "initial_risk": float(nxt4.initial_risk_usd),
                                     "forced_pct_482": round(float(nxt4.initial_risk_usd) / float(prev_bal4) * 100, 2)}
                                    if nxt4 is not None else "no trade — guard blocked"),
            }
            break
    report["ichi_first_divergence_533_vs_482"] = div

    # balance sweep
    sweep = []
    for bal in range(400, 825, 25):
        eng, _ = engine_for("ichi", {"risk.max_forced_risk_percent": 3.0, "risk.sl_atr_multiplier": 2.0}, {}, balance=bal)
        res = eng.run(M5, M15, H1)
        rows = rows_from(res, f"SWEEP{bal}|ichi|S2", "ichi", bal, 20.0, 0.0, lmaps["ichi"])
        df = pd.DataFrame(rows)
        nets = df.net_pnl.values if len(df) else np.array([0.0])
        wins = nets[nets > 0]; losses = nets[nets < 0]
        eq = df.balance_after.values if len(df) else np.array([bal])
        peak = np.maximum.accumulate(np.concatenate([[bal], eq]))[1:]
        dd = (peak - eq).max() if len(df) else 0.0
        d5 = len(TL[TL.run_id == "D533|ichi|S2"])
        sweep.append({"initial_balance": bal, "trades": len(df), "net": round(float(nets.sum()), 2),
                      "pf": round(float(wins.sum() / abs(losses.sum())), 3) if len(losses) and losses.sum() < 0 else None,
                      "win_rate": round(len(wins) / max(1, len(df)) * 100, 2),
                      "max_dd_usd": round(float(dd), 2),
                      "blocked_vs_D533_trades": d5 - len(df)})
        print(f"sweep {bal}: net={sweep[-1]['net']} trades={len(df)}", flush=True)
    pd.DataFrame(sweep).to_csv(f"{OUT}/BALANCE_SWEEP.csv", index=False)

    # ---------- DAILY_EQUITY + portfolio verification (points 2,3) ----------
    do = pd.Series(
        TL[TL.run_id == "C533|orb|S2"].net_pnl.values,
        index=pd.DatetimeIndex(TL[TL.run_id == "C533|orb|S2"].exit_time)).groupby(level=0).sum()
    do.index = do.index.normalize()   # collapse intraday exit times to calendar dates
    di = pd.Series(
        TL[TL.run_id == "C533|ichi|S2"].net_pnl.values,
        index=pd.DatetimeIndex(TL[TL.run_id == "C533|ichi|S2"].exit_time)).groupby(level=0).sum()
    di.index = di.index.normalize()
    do = do.groupby(level=0).sum()
    di = di.groupby(level=0).sum()
    cal = pd.date_range(min(do.index.min(), di.index.min()), max(do.index.max(), di.index.max()), freq="D")
    de = pd.DataFrame({"orb_pnl": do.reindex(cal).fillna(0.0),
                       "ichi_pnl": di.reindex(cal).fillna(0.0)})
    de["combined_pnl"] = de.orb_pnl + de.ichi_pnl
    de["orb_equity"] = INIT_SHARED + de.orb_pnl.cumsum()
    de["ichi_equity"] = INIT_SHARED + de.ichi_pnl.cumsum()
    de["combined_equity"] = INIT_SHARED + de.combined_pnl.cumsum()
    de.index.name = "date"
    de.to_csv(f"{OUT}/DAILY_EQUITY.csv")

    def dd_info(eq):
        peak = eq.cummax()
        dd = eq - peak
        i = int(dd.idxmin().ordinal if hasattr(dd.idxmin(), "ordinal") else dd.values.argmin())
        dmax, imax = dd.max(), None
        # peak date = date of running max at trough
        trough_i = int(np.argmin(dd.values))
        peak_i = int(np.argmax(eq.values[: trough_i + 1]))
        return {"peak_date": str(eq.index[peak_i].date()), "peak_equity": round(float(eq.values[peak_i]), 2),
                "trough_date": str(eq.index[trough_i].date()), "trough_equity": round(float(eq.values[trough_i]), 2),
                "dd_usd": round(float(-dd.values[trough_i]), 2),
                "dd_pct_of_peak": round(float(-dd.values[trough_i] / eq.values[peak_i] * 100), 2),
                "dd_pct_of_initial": round(float(-dd.values[trough_i] / INIT_SHARED * 100), 2)}
    def sharpe_variants(pnl_col, eq_col):
        r_full = de[eq_col].pct_change().dropna()            # full calendar incl. flat days
        p_traded = de.loc[de[pnl_col] != 0, pnl_col]          # traded days only
        r_traded = p_traded / INIT_SHARED                     # return on initial (approx, static denominator documented)
        return {
            "sharpe_fullcalendar_ann": round(float(r_full.mean() / r_full.std() * math.sqrt(252)), 4) if len(r_full) > 2 and r_full.std() > 0 else None,
            "days_fullcalendar": int(len(r_full)),
            "mean_ret_fullcalendar_daily": round(float(r_full.mean()), 6),
            "std_fullcalendar_daily": round(float(r_full.std()), 6),
            "sharpe_tradeddays_ann_static_denominator": round(float(r_traded.mean() / r_traded.std() * math.sqrt(252)), 4) if len(r_traded) > 2 and r_traded.std() > 0 else None,
            "days_traded": int(len(r_traded)),
            "mean_pnl_tradedday": round(float(p_traded.mean()), 4),
            "std_pnl_tradedday": round(float(p_traded.std()), 4),
            "note": "full-calendar: returns on running equity, zero on flat days. traded-days: daily P&L over static initial capital (convention B); both reported, no cherry-pick.",
        }
    report["portfolio_verification"] = {
        "capital_model": "SHARED single account $533 (matches live: both bots trade one account); per-bot sizing in the two backtests ran on their own curves -> approximation, corr 0.06",
        "weights": "equal nominal P&L sum (both bots risk 1% of the same account in live)",
        "daily_pnl_correlation": round(float(de.loc[(de.orb_pnl != 0) | (de.ichi_pnl != 0), "orb_pnl"].corr(
            de.loc[(de.orb_pnl != 0) | (de.ichi_pnl != 0), "ichi_pnl"])), 4),
        "orb": {"sharpe": sharpe_variants("orb_pnl", "orb_equity"), "maxdd": dd_info(de.orb_equity)},
        "ichi": {"sharpe": sharpe_variants("ichi_pnl", "ichi_equity"), "maxdd": dd_info(de.ichi_equity)},
        "combined": {"sharpe": sharpe_variants("combined_pnl", "combined_equity"), "maxdd": dd_info(de.combined_equity)},
        "correction_of_v1": "v1 combined Sharpe 1.31 used TRADED-DAYS-ONLY with static denominator -> convention B here; full-calendar convention A is the defensible one and is reported first.",
    }

    # ---------- point 7: cost model transparency + overnight/swap exposure ----------
    ichi_c = TL[TL.run_id == "C533|ichi|S2"]
    orb_c = TL[TL.run_id == "C533|orb|S2"]
    report["cost_model"] = {
        "spread": "ONE full spread per round trip (BUY pays on entry at ask=open+spread; SELL pays on exit at ask-side checks). NOT double-counted.",
        "slippage": "applied on entry (both sides, adverse) AND on stop-type exits (sl/trailing/breakeven, adverse). TP exits and end-of-data fills: NO slippage.",
        "tp_exit": "long: filled at tp (bid high touched); short: exit at tp after adding spread to bar low (engine 661-700)",
        "sl_exit": "long: exit_price = sl - slip; short: sl + slip (engine 645-655). GAP THROUGH SL FILLS AT STOP, not at the gap price (optimistic vs real broker) - bug table B12",
        "end_of_data": "exit at last close (+spread for SELL) - engine 552-562",
        "units_check": "spread_points * point(0.01) = price distance; money = dist/tick_size(0.01)*tick_value(1.0)*lot -> spread cost $ = spread_points * lot",
        "swap": "NOT MODELED. Overnight-held trades (entry date != exit date): ichi "
                f"{int(ichi_c.held_overnight.sum())}/{len(ichi_c)} ({ichi_c.held_overnight.mean()*100:.1f}%), "
                f"orb {int(orb_c.held_overnight.sum())}/{len(orb_c)} ({orb_c.held_overnight.mean()*100:.1f}%)",
        "net_label": "all 'net' figures = after all MODELED costs (spread+slip+commission=0). Swap excluded.",
    }

    # ---------- point 8: gaps ----------
    gaps = M5.index.to_series().diff()
    gaps.iloc[0] = pd.Timedelta(0)
    big = gaps[gaps > pd.Timedelta("6min")]
    buckets = {"6-15min": int(((big > pd.Timedelta("6min")) & (big <= pd.Timedelta("15min"))).sum()),
               "15-60min": int(((big > pd.Timedelta("15min")) & (big <= pd.Timedelta("60min"))).sum()),
               "60-240min": int(((big > pd.Timedelta("60min")) & (big <= pd.Timedelta("240min"))).sum()),
               ">240min": int((big > pd.Timedelta("240min")).sum())}
    multi = big[gaps > pd.Timedelta("1 day")]
    gap_days = set((big.index[big > pd.Timedelta("60min")]).date)
    trades_adj = TL[TL.run_id.isin(["C533|orb|S2", "C533|ichi|S2"])].copy()
    trades_adj["gap_after"] = [any(abs((t - M5.index).min()) <= pd.Timedelta("6min") and
                                   (gaps.loc[gaps.index[gaps.index.get_indexer([t], method="nearest")[0]]] > pd.Timedelta("60min")))
                               for t in trades_adj.entry_time] if False else [False] * len(trades_adj)
    # simpler: trades whose entry bar immediately follows a >60min gap
    gap_starts = set(big.index[big > pd.Timedelta("60min")])
    def follows_gap(t):
        i = M5.index.get_indexer([t], method="nearest")[0]
        return i > 0 and (M5.index[i] - M5.index[i - 1]) > pd.Timedelta("60min")
    trades_adj["entry_follows_big_gap"] = trades_adj.entry_time.map(follows_gap)
    # QC run: remove only days with INTRADAY data gaps (>60min within the same date);
    # the daily ~1h market maintenance break (spanning midnight) is normal, not data loss.
    pos = M5.index.get_indexer(big.index)
    prev_bar = M5.index[np.maximum(pos - 1, 0)]
    intraday_mask = (prev_bar.normalize() == big.index.normalize()) & (big > pd.Timedelta("60min"))
    intraday_gaps = big[intraday_mask]
    gap_days = set(intraday_gaps.index.date)
    keep = ~M5.index.normalize().isin(gap_days)
    qc = {}
    for bot in ("orb", "ichi"):
        eng, _ = engine_for(bot, {"risk.max_forced_risk_percent": 3.0, "risk.sl_atr_multiplier": 2.0}, {})
        res = eng.run(M5[keep], M15, H1)
        rows = rows_from(res, f"QC_nogapdays|{bot}", bot, 533.0, 20.0, 0.0, lmaps[bot])
        dfq = pd.DataFrame(rows)
        if len(dfq):
            nets = dfq.net_pnl.values
            qc[bot] = {"trades": len(dfq), "net": round(float(nets.sum()), 2),
                       "removed_intraday_gap_days": len(gap_days),
                       "note": "data-QC only — days with >60min INTRADAY gaps removed (daily maintenance break excluded); not a result-selector"}
        else:
            qc[bot] = {"trades": 0, "net": 0.0, "removed_intraday_gap_days": len(gap_days), "note": "no trades"}
        print(f"QC nogap {bot}: {qc[bot]}", flush=True)
    report["gap_analysis"] = {
        "buckets": buckets,
        "intraday_gaps_over_60min": int(len(intraday_gaps)),
        "intraday_gap_days_removed_in_qc": len(gap_days),
        "multi_day_gaps": [str(d.date()) for d in multi.index][:5],
        "multi_day_gap_durations": [str(v) for v in multi.values][:5],
        "trades_entering_right_after_big_gap": int(trades_adj.entry_follows_big_gap.sum()),
        "indicator_note": "ATR/cooldown: computed on the continuous bar series — a gap becomes one huge synthetic 'bar' span in rolling windows only via the following bars' ranges; ORB: session-day grouping by (time-1h).date, range uses bars present after gap; H1 alignment: merge_asof backward -> after a gap the last closed H1 value persists (ffill). No lookahead introduced.",
        "qc_run_without_gap_days": qc,
    }
    pd.DataFrame([buckets]).to_csv(f"{OUT}/GAP_ANALYSIS.csv", index=False)

    # ---------- point 9: yearly metrics (C@S2) ----------
    yearly = {}
    for bot in ("orb", "ichi"):
        df = TL[TL.run_id == f"C533|{bot}|S2"].copy()
        df["year"] = pd.DatetimeIndex(df.exit_time).year
        yl = {}
        for y, g in df.groupby("year"):
            nets = g.net_pnl.values
            wins, losses = nets[nets > 0], nets[nets < 0]
            eq = nets.cumsum() + 533.0
            peak = np.maximum.accumulate(eq)
            yl[int(y)] = {"net": round(float(nets.sum()), 2), "trades": len(g),
                          "pf": round(float(wins.sum() / abs(losses.sum())), 3) if len(losses) and losses.sum() < 0 else None,
                          "win_rate": round(len(wins) / len(g) * 100, 2),
                          "expectancy_usd": round(float(nets.mean()), 3),
                          "max_dd_usd_within_year": round(float((peak - eq).max()), 2)}
        yearly[bot] = yl
    # 2026 ichi decomposition
    i26 = TL[(TL.run_id == "C533|ichi|S2") & (pd.DatetimeIndex(TL.exit_time).year == 2026)]
    yearly["ichi_2026_decomposition"] = {
        "by_side": {s: round(float(i26[i26.side == s].net_pnl.sum()), 2) for s in ("BUY", "SELL")},
        "by_layer": {l: round(float(i26[i26.signal_layer == l].net_pnl.sum()), 2) for l in i26.signal_layer.unique()},
        "by_exit_reason": {r: round(float(i26[i26.exit_reason == r].net_pnl.sum()), 2) for r in i26.exit_reason.unique()},
        "top5_trades": sorted([round(float(x), 2) for x in i26.net_pnl], reverse=True)[:5],
        "n_trades": len(i26),
    }
    report["yearly_C_S2"] = yearly

    # ---------- expectancy_R full precision (point 6) ----------
    expR = {}
    for rid in ("A533|ichi|S2", "C533|ichi|S2", "C533|orb|S2", "D533|ichi|S2"):
        df = TL[TL.run_id == rid]
        if len(df) == 0:
            continue
        expR[rid] = {
            "expR_mean_of_ratios_full": float(df.r_multiple.mean()),
            "expR_aggregate_sum_net_over_sum_risk_full": float(df.net_pnl.sum() / df.initial_risk_usd.sum()),
            "definition": "R_i = net_i / (money_per_lot(initial_sl_distance_i)*lot_i + commission_i); commission=0 on VPS; spread/slip are NOT in initial risk (they are inside net)",
        }
    report["expectancy_R_full"] = expR
    pd.DataFrame(expR).T.to_csv(f"{OUT}/EXPECTANCY_R.csv")

    # ---------- BASELINE_RESULTS.csv from phase2 json ----------
    R = json.load(open("_phase2_results.json"))
    bl = []
    for k, m in R.items():
        if not isinstance(m, dict):
            continue
        row = {"run_id": k}
        row.update({kk: vv for kk, vv in m.items() if kk != "_trades"})
        bl.append(row)
    pd.DataFrame(bl).to_csv(f"{OUT}/BASELINE_RESULTS.csv", index=False)

    # COST_SCENARIOS.csv
    cs = []
    for sp in (8, 20, 26, 35, 50, 70, 100):
        for bot in ("orb", "ichi"):
            key = (f"C_vps_guard3|{bot}|S1_ideal8_slip0" if sp == 8 else
                   f"C_vps_guard3|{bot}|S2_est20_slip0" if sp == 20 else
                   f"C_vps_guard3|{bot}|S3_sp26_slip0" if sp == 26 else
                   f"C_vps_guard3|{bot}|BE_sp{sp}")
            if key in R:
                cs.append({"config": "C_vps_guard3", "bot": bot, "spread_pts": sp, "slip_pts": 0,
                           "net": R[key].get("net"), "pf": R[key].get("pf"), "trades": R[key].get("trades")})
    pd.DataFrame(cs).to_csv(f"{OUT}/COST_SCENARIOS.csv", index=False)

    # ---------- charts ----------
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        for name, col in (("orb", "orb_equity"), ("ichi", "ichi_equity"), ("combined", "combined_equity")):
            fig, ax = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1]})
            ax[0].plot(de.index, de[col], lw=1.0)
            ax[0].axhline(INIT_SHARED, color="grey", ls="--", lw=0.8)
            ax[0].set_title(f"{name} — equity (C@S2, initial $533)")
            dd = de[col] - de[col].cummax()
            ax[1].fill_between(de.index, dd, 0, color="crimson", alpha=0.5)
            ax[1].set_ylabel("drawdown $")
            fig.tight_layout()
            fig.savefig(f"{OUT}/equity_{name}.png", dpi=110)
            plt.close(fig)
        print("charts saved", flush=True)
    except Exception as e:
        print("chart skipped:", e, flush=True)

    json.dump(report, open(f"{OUT}/PORTFOLIO_CHECK.json", "w"), indent=1, default=str)
    print("DONE", flush=True)

if __name__ == "__main__":
    main()
