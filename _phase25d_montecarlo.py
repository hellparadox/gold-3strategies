"""PHASE 2.5d — TRADE-SEQUENCE SENSITIVITY (Monte-Carlo, research-only).

Answers the review point: 'حساسیت به ترتیب تاریخی معاملات بررسی نشده است'.
Method: bootstrap the ORDER of realized trades from the corrected 2.5c sim
(allocation study, gap-aware + M5 grid + 20pts + guard ON) and recompute the
equity path from the SAME initial capital. No parameters touched; the P&L set
per case is fixed — only its sequence is resampled.

Reported per case:
  - max drawdown distribution (P50/P90/P95/P99) in $ and % of initial
  - P(DD > 50% of initial), P(DD > 90%)
  - terminal-net is sequence-INSENSITIVE by construction (sum) — stated, not sampled
  - block bootstrap (block=20 trades) as second variant preserving clustering
"""
import json
import numpy as np
import pandas as pd

CAP = 482.53
RNG = np.random.default_rng(42)
N = 5000

def path_stats(pnls, cap=CAP):
    eq = cap + np.cumsum(pnls)
    peak = np.maximum.accumulate(np.concatenate([[cap], eq]))[1:]
    dd = peak - eq
    return dd.max(), dd.max() / peak[int(dd.argmax())] * 100 if len(dd) and peak[int(dd.argmax())] > 0 else 0.0, eq.min()

def mc(trades_pnl, name, block=0):
    pnl = np.array(trades_pnl, dtype=float)
    dds, dds_pct, mins = [], [], []
    for _ in range(N):
        if block > 0:
            idx = []
            while len(idx) < len(pnl):
                s = RNG.integers(0, len(pnl) - block + 1)
                idx.extend(range(s, min(s + block, len(pnl))))
            sample = pnl[idx[:len(pnl)]]
        else:
            sample = pnl[RNG.permutation(len(pnl))]
        d, dp, mn = path_stats(sample)
        dds.append(d); dds_pct.append(dp); mins.append(mn)
    dds, dds_pct, mins = np.array(dds), np.array(dds_pct), np.array(mins)
    return {
        "case": name, "n_trades": int(len(pnl)), "resamples": N,
        "block": block,
        "actual_net_sum": round(float(pnl.sum()), 2),
        "actual_dd_usd": round(path_stats(pnl)[0], 2),
        "mc_dd_usd_p50": round(float(np.percentile(dds, 50)), 2),
        "mc_dd_usd_p90": round(float(np.percentile(dds, 90)), 2),
        "mc_dd_usd_p95": round(float(np.percentile(dds, 95)), 2),
        "mc_dd_usd_p99": round(float(np.percentile(dds, 99)), 2),
        "mc_dd_pct_initial_p50": round(float(np.percentile(dds, 50) / CAP * 100), 1),
        "mc_dd_pct_initial_p95": round(float(np.percentile(dds, 95) / CAP * 100), 1),
        "P_dd_gt_50pct_initial": round(float((dds > CAP * 0.5).mean() * 100), 2),
        "P_dd_gt_90pct_initial": round(float((dds > CAP * 0.9).mean() * 100), 2),
        "P_min_equity_below_50usd": round(float((mins < 50).mean() * 100), 2),
        "note": "terminal net is order-insensitive (sum); only PATH metrics sampled",
    }

out = []
for case, f in (("A_ichi_alone", "_phase2_out/ALLOCATION_A_ichi_trades.csv"),
                ("B_orb_alone", "_phase2_out/ALLOCATION_B_orb_trades.csv"),
                ("D_shared", "_phase2_out/ALLOCATION_D_shared_trades.csv")):
    pnl = pd.read_csv(f)["pnl"].to_numpy(float)
    out.append(mc(pnl, f"{case}_iid_shuffle"))
    out.append(mc(pnl, f"{case}_block20", block=20))
    print(json.dumps(out[-2]), flush=True)
    print(json.dumps(out[-1]), flush=True)

json.dump(out, open("_phase25d_montecarlo.json", "w"), indent=1)
print("DONE", flush=True)
