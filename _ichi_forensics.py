"""Forensics on the instrumented ichi baseline — reads trade CSV, no new sims."""
import json, sys
import pandas as pd
import numpy as np

f = sys.argv[1] if len(sys.argv) > 1 else "_ichi_out/baseline_sp20_trades.csv"
df = pd.read_csv(f, parse_dates=["entry_time", "time"])
df["year"] = df["entry_time"].dt.year
out = {}

def stats(d, label):
    if len(d) == 0:
        return {label: "no trades"}
    w = d[d.pnl > 0]; l = d[d.pnl < 0]
    return {
        "label": label, "n": len(d), "net": round(d.pnl.sum(), 2),
        "pf": round(w.pnl.sum() / abs(l.pnl.sum()), 3) if len(l) and l.pnl.sum() != 0 else None,
        "wr": round(len(w) / len(d) * 100, 1),
        "avg": round(d.pnl.mean(), 2),
        "worst": round(d.pnl.min(), 2), "best": round(d.pnl.max(), 2),
    }

# 1) per-layer overall + yearly
out["layers"] = [stats(g, k) for k, g in df.groupby("layer")]
out["layers_yearly"] = {k: {int(y): round(g.pnl.sum(), 2) for y, g in d.groupby("year")}
                        for k, d in df.groupby("layer")}
out["layer_side"] = [stats(g, f"{k}/{s}") for (k, s), g in df.groupby(["layer", "side"])]

# 2) exit reason per layer
out["reason_by_layer"] = {}
for lay, g in df.groupby("layer"):
    out["reason_by_layer"][lay] = {r: int(n) for r, n in g["reason"].value_counts().items()}

# 3) worst 12 losses with context
cols = ["entry_time", "layer", "side", "pnl", "mfe", "mae", "bars_held", "be_done",
        "entry_px", "exit_px", "sl_initial", "sl_final", "atr_entry", "lot", "risk_pct"]
out["worst12"] = df.nsmallest(12, "pnl")[cols].to_dict("records")

# 4) MFE giveback: reached >= $8 profit (MFE) then how much kept?
big_mfe = df[df.mfe >= 8.0]
out["giveback"] = {
    "trades_mfe_ge_8": len(big_mfe),
    "sum_mfe": round(big_mfe.mfe.sum(), 2),
    "sum_pnl": round(big_mfe.pnl.sum(), 2),
    "kept_pct": round(big_mfe.pnl.sum() / big_mfe.mfe.sum() * 100, 1) if len(big_mfe) else None,
    "ended_negative": int((big_mfe.pnl < 0).sum()),
    "mfe_ge8_by_reason": {k: int(v) for k, v in big_mfe.reason.value_counts().items()},
    "mfe_ge8_be_done_split": {f"be_{b}": round(g.pnl.sum(), 2)
                              for b, g in big_mfe.groupby("be_done")},
}
# giveback granularity: pnl vs mfe by exit reason
out["giveback_by_reason"] = [
    {"reason": r, "n": len(g), "sum_mfe": round(g.mfe.sum(), 2), "sum_pnl": round(g.pnl.sum(), 2),
     "kept_pct": round(g.pnl.sum() / g.mfe.sum() * 100, 1) if g.mfe.sum() > 0 else None}
    for r, g in df[df.mfe > 0].groupby("reason")]

# 5) BE/trailing: did BE save or cut?
be = df[df.be_done]; nbe = df[~df.be_done]
out["be_split"] = {
    "be_done_n": len(be), "be_done_net": round(be.pnl.sum(), 2),
    "be_done_win_rate": round((be.pnl > 0).mean() * 100, 1) if len(be) else None,
    "be_done_neg": int((be.pnl < 0).sum()), "be_done_neg_net": round(be[be.pnl < 0].pnl.sum(), 2),
    "no_be_n": len(nbe), "no_be_net": round(nbe.pnl.sum(), 2),
    "be_neg_pnl_distribution": be[be.pnl < 0].pnl.round(1).tolist()[:40],
}
# BE exits that returned to entry (pnl in [-1, 1] after BE) — "BE kickouts"
out["be_kickouts"] = {
    "n_pnl_between_neg2_2": int(((be.pnl > -2.0) & (be.pnl < 2.0)).sum()),
    "sum_pnl": round(be[(be.pnl > -2.0) & (be.pnl < 2.0)].pnl.sum(), 2),
}

# 6) full-SL losses (exit at initial SL, no BE) — entry-quality question
full_sl = df[(df.reason == "sl") & (~df.be_done) & (np.abs(df.sl_final - df.sl_initial) < 1e-9)]
out["full_sl"] = [stats(full_sl, "full_sl_hits"),
                  {"by_layer": {k: len(g) for k, g in full_sl.groupby("layer")},
                   "net": round(full_sl.pnl.sum(), 2),
                   "by_year": {int(y): round(g.pnl.sum(), 2) for y, g in full_sl.groupby("year")}}]

# 7) yearly net (all layers)
out["yearly_net"] = {int(y): round(g.pnl.sum(), 2) for y, g in df.groupby("year")}

# 8) risk/sizing shape
out["sizing"] = {"risk_pct_mean": round(df.risk_pct.mean(), 2),
                 "risk_pct_max": round(df.risk_pct.max(), 2),
                 "lot_mean": round(df.lot.mean(), 3),
                 "avg_loss": round(df[df.pnl < 0].pnl.mean(), 2),
                 "avg_win": round(df[df.pnl > 0].pnl.mean(), 2)}

# 9) MAE of winners (how far against us before winning) + MFE of losers
out["excursion"] = {
    "winners_mae_mean": round(df[df.pnl > 0].mae.mean(), 2),
    "winners_mae_p90": round(df[df.pnl > 0].mae.quantile(0.9), 2),
    "losers_mfe_mean": round(df[df.pnl < 0].mfe.mean(), 2),
    "losers_mfe_ge_5": int((df[df.pnl < 0].mfe >= 5).sum()),
    "losers_mfe_ge_5_sum": round(df[(df.pnl < 0) & (df.mfe >= 5)].mfe.sum(), 2),
    "losers_mfe_ge_5_net": round(df[(df.pnl < 0) & (df.mfe >= 5)].pnl.sum(), 2),
}

# 10) bars held shape for losers vs winners
out["bars"] = {"winners_mean_bars": round(df[df.pnl > 0].bars_held.mean(), 1),
               "losers_mean_bars": round(df[df.pnl < 0].bars_held.mean(), 1),
               "trades_le_2_bars": int((df.bars_held <= 2).sum()),
               "le2_bars_net": round(df[df.bars_held <= 2].pnl.sum(), 2)}

print(json.dumps(out, indent=1, default=str))
json.dump(out, open(f.replace("_trades.csv", "_forensics.json"), "w"), indent=1, default=str)
