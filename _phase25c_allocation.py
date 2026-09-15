"""PHASE 2.5c — CAPITAL-ALLOCATION ISOLATION + missing scenario (research-only).

CORRECTION (2026-09-15, review round 2): the original release computed Case C as
`ra.equity + rb.equity - CAP`. That `- CAP` shifted the ENTIRE portfolio series
down by one base ($482.53): min_equity was reported as $18.14 (true: $500.67),
peak $578.53 (true: $1,061.06), DD%of-peak 96.86% (true: 52.81%). Each equity
series already carries its own base, so A + B IS the two-account portfolio
equity. Net, DD$ and peak/trough DATES were unaffected (shift-invariant).
Case C is now run THROUGH the engine (independent mode) and cross-checked
against a reconstruction from the raw committed A/B CSVs (part 0 below).

Cases (explicit capital):
  A) Ichimoku alone        — $482.53
  B) ORB alone             — $482.53
  C) Independent accounts  — $482.53 EACH ($965.06 total), engine-run (shared=False)
  D) Shared account        — $482.53 TOTAL
Equal-capital pairs (review point 2):
  E) Independent, $241.265 each (total $482.53)  — same TOTAL as D
  F) Shared, $965.06 total                        — same TOTAL as C

Money handling (declared): capitals are used at full float64 precision — money
is NEVER rounded. Lot sizing: raw = risk budget / loss-per-lot, floored to the
0.01 volume step, clamped to the config band (ORB 0.01–0.05, ichi 0.01–0.10).
RECORDED trade pnl is rounded to 2dp for CSV/JSON only — balances keep full
precision internally, so equity-delta net and rounded-trade net can differ by
a few cents; both are reported.

ONE engine (_phase25c_core.simulate), ONE dataset, ONE exit-management (M5 grid,
BE/trailing next-bar rule), ONE cost model (fixed 20pts spread, slip 0, comm 0),
gap-aware fill ON, guard 3% ON.
"""
import hashlib, json, pickle
import numpy as np
import pandas as pd

from _phase25c_core import SimConfig, simulate
from core import Settings
from core.risk_manager import RiskConfig
from backtest.engine import HistoricalNewsChecker
from strategies import build_from_settings

M5, M15, H1 = pickle.load(open("_sp2l_data_5y.pkl", "rb"))
CAP = 482.53
HALF = CAP / 2      # 241.265 — equal-capital split, full precision (not rounded)
DOUBLE = CAP * 2    # 965.06  — equal-capital counterpart of C


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(bot):
    s = Settings.load({"orb": "config/settings.yaml", "ichi": "config/settings_ichimoku.yaml"}[bot])
    strat = build_from_settings(s)
    rc = RiskConfig.from_settings(s)
    rc.sl_atr_multiplier = 2.0          # VPS live config
    rc.max_forced_risk_percent = 3.0    # guard ON (also enforced in sim)
    news = HistoricalNewsChecker(filepath="data/historical_news.json", before_minutes=5,
                                 after_minutes=5, enabled=True, server_utc_offset_hours=3.0,
                                 after_minutes_tier1=120,
                                 tier1_patterns=["non-farm", "nfp", "cpi", "fomc", "federal funds", "powell"])
    sess = (int(s.get("session.start_hour", 0)), int(s.get("session.end_hour", 24)))
    return strat, rc, news, sess


# ================= PART 0 — reconstruct Case C from the RAW committed CSVs ====
# Runs BEFORE this script's own sims overwrite those CSVs. Alignment method
# (declared): both equity series are stamped with the M5 bar label (open time),
# value = equity at that bar's close. Common calendar = outer-join UNION of the
# two timestamp sets. The indices are verified IDENTICAL (same 350,000
# timestamps), so the portfolio series is an EXACT pointwise sum and NO
# forward/backward filling is required (NaN count asserted 0).
A_EQ = "_phase2_out/ALLOCATION_A_ichi_equity.csv"
B_EQ = "_phase2_out/ALLOCATION_B_orb_equity.csv"
A_TR = "_phase2_out/ALLOCATION_A_ichi_trades.csv"
B_TR = "_phase2_out/ALLOCATION_B_orb_trades.csv"

recon = {
    "method": "common calendar = outer-join union of M5 bar stamps; indices verified "
              "identical -> exact pointwise sum, no fill applied, NaN count 0",
    "inputs": {"A_equity_csv_sha256": sha256_file(A_EQ),
               "B_equity_csv_sha256": sha256_file(B_EQ)},
}
_ea = pd.read_csv(A_EQ, parse_dates=["bar_close_time"], index_col="bar_close_time")["equity"]
_eb = pd.read_csv(B_EQ, parse_dates=["bar_close_time"], index_col="bar_close_time")["equity"]
recon["inputs"].update({"A_rows": len(_ea), "B_rows": len(_eb),
                        "index_identical": bool(_ea.index.equals(_eb.index))})
eq_c_csv = _ea + _eb                       # exact pointwise sum on identical index
assert int(eq_c_csv.isna().sum()) == 0


def dd_full(eq, initial):
    """Full-precision drawdown details (no rounding)."""
    peak = eq.cummax()
    dd = eq - peak
    ti = int(dd.values.argmin())
    pi = int(np.argmax(eq.values[: ti + 1]))
    return {"initial": float(eq.iloc[0]), "final": float(eq.iloc[-1]),
            "min_equity": float(eq.min()),
            "dd_usd": float(-dd.values[ti]),
            "peak": float(eq.values[pi]), "peak_time": str(eq.index[pi]),
            "trough": float(eq.values[ti]), "trough_time": str(eq.index[ti]),
            "dd_pct_of_peak": float(-dd.values[ti] / eq.values[pi] * 100),
            "dd_pct_of_initial": float(-dd.values[ti] / initial * 100)}


_d = dd_full(eq_c_csv, 2 * CAP)
_ta = pd.read_csv(A_TR)
_tb = pd.read_csv(B_TR)
_net_a_eq = float(_ea.iloc[-1] - CAP)      # net from equity delta (full precision)
_net_b_eq = float(_eb.iloc[-1] - CAP)
recon.update({
    "C_initial": _d["initial"],
    "C_final_full_precision": _d["final"],
    "identity_final": {
        "965.06_plus_netA_plus_netB": 2 * CAP + _net_a_eq + _net_b_eq,
        "matches": bool(abs((2 * CAP + _net_a_eq + _net_b_eq) - _d["final"]) < 1e-9)},
    "net_A_from_equity_delta": _net_a_eq,
    "net_B_from_equity_delta": _net_b_eq,
    "net_A_from_rounded_trades": round(float(_ta["pnl"].sum()), 2),
    "net_B_from_rounded_trades": round(float(_tb["pnl"].sum()), 2),
    "rounding_note": "trade records round pnl to 2dp; balances keep full precision "
                     "internally -> equity-delta net vs rounded-trade net differ by cents",
    "min_C": _d["min_equity"],
    "min_A": float(_ea.min()), "min_B": float(_eb.min()),
    "min_A_plus_min_B": float(_ea.min() + _eb.min()),
    "min_C_ge_minA_plus_minB": bool(_d["min_equity"] >= float(_ea.min() + _eb.min()) - 1e-9),
    "dd": _d,
})
json.dump(recon, open("_phase2_out/ALLOCATION_C_reconstruction.json", "w"), indent=1)
print("PART0 C-reconstruction:", json.dumps(recon, default=str), flush=True)

# ================= signals ===================================================
CTX = {}
for bot in ("orb", "ichi"):
    strat, rc, news, sess = build(bot)
    if bot == "orb":
        p = strat.prepare_and_sign(M5, None, None)
    else:
        p = strat.prepare_and_sign(M15, M15, H1)
    CTX[bot] = {"sig": p["signal"], "atr": p["atr"], "rc": rc, "news": news, "sess": sess,
                # a signal is actionable only after its bar closes:
                "sig_close": p.index + pd.Timedelta(minutes=15 if bot == "ichi" else 5)}
print("signals prepared", flush=True)

CFG = dict(guard_pct=3.0, spread_pts=20.0, gap_fill_at_open=True, leverage=100.0)


def summarize(name, r, capital_total, trades_csv=None):
    nets = [t["pnl"] for t in r.trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    out = {
        "case": name, "capital_total": round(capital_total, 2),
        "trades": len(r.trades), "net": round(sum(nets), 2),
        "net_from_equity_delta": round(float(r.equity.iloc[-1] - capital_total), 2),
        "pf": round(sum(wins) / abs(sum(losses)), 3) if losses and sum(losses) < 0 else None,
        "win_rate": round(len(wins) / max(1, len(nets)) * 100, 1),
        "guard_blocks": r.guard_blocks, "margin_rejects": r.margin_rejects,
        "max_trade_risk_pct": r.max_trade_risk_pct,
        "dd": r.dd,
    }
    if trades_csv:
        pd.DataFrame(r.trades).to_csv(trades_csv, index=False)
        r.equity.to_frame("equity").to_csv(trades_csv.replace("trades", "equity"), index_label="bar_close_time")
    return out


results = []


def run(name, bots, shared, capital, capital_total, csv_tag):
    r = simulate(M5, M15, H1, {b: CTX[b] for b in bots},
                 SimConfig(bots=list(bots), shared=shared, capital=capital, **CFG))
    results.append(summarize(name, r, capital_total, f"_phase2_out/ALLOCATION_{csv_tag}_trades.csv"))
    print(json.dumps(results[-1], default=str), flush=True)
    return r


r_a = run("A_ichi_alone_482", ("ichi",), False, CAP, CAP, "A_ichi")
r_b = run("B_orb_alone_482", ("orb",), False, CAP, CAP, "B_orb")
r_c = run("C_independent_482_each", ("orb", "ichi"), False, CAP, 2 * CAP, "C_indep")
r_d = run("D_shared_482_total", ("orb", "ichi"), True, CAP, CAP, "D_shared")
r_e = run("E_independent_241.265_each", ("orb", "ichi"), False, HALF, 2 * HALF, "E_indep241")
r_f = run("F_shared_965_total", ("orb", "ichi"), True, DOUBLE, DOUBLE, "F_shared965")

# ================= identity checks (review point 1) ==========================
ident = {
    "claim": "Equity_C(t) = Equity_A(t) + Equity_B(t) at every bar-close timestamp",
    "index_identical_A_B": bool(r_a.equity.index.equals(r_b.equity.index)),
    "index_identical_C_A": bool(r_c.equity.index.equals(r_a.equity.index)),
    "max_abs_diff_engineC_vs_A_plus_B": float((r_c.equity - (r_a.equity + r_b.equity)).abs().max()),
    "max_abs_diff_engineC_vs_csv_reconstruction": float((r_c.equity - eq_c_csv).abs().max()),
    "trades_C_equals_A_plus_B": len(r_c.trades) == len(r_a.trades) + len(r_b.trades),
    "guard_blocks_C_equals_A_plus_B": r_c.guard_blocks == r_a.guard_blocks + r_b.guard_blocks,
    "margin_rejects_C_equals_A_plus_B": r_c.margin_rejects == r_a.margin_rejects + r_b.margin_rejects,
    "min_C": float(r_c.equity.min()),
    "min_A_plus_min_B": float(r_a.equity.min() + r_b.equity.min()),
    "min_C_ge_minA_plus_minB": bool(r_c.equity.min() >= float(r_a.equity.min() + r_b.equity.min()) - 1e-9),
    "final_C": float(r_c.equity.iloc[-1]),
    "final_C_equals_2CAP_plus_nets": bool(abs(
        r_c.equity.iloc[-1]
        - (2 * CAP + (r_a.equity.iloc[-1] - CAP) + (r_b.equity.iloc[-1] - CAP))) < 1e-8),
}
print("IDENTITY:", json.dumps(ident, default=str), flush=True)


# ================= timing verification (review point 3, real data) ===========
def timing_check(bot, trades, ctx):
    """Independent check on the ACTUAL trade records: the only consumable signal
    bar at entry time T is the last bar whose close <= T. A lookahead entry
    (the old phase-2.5 bug: consuming a bar 10 min before it closed) shows up
    here as: last-closed bar carries no matching signal, side mismatch, or the
    same source bar sourcing two entries."""
    sig, sc = ctx["sig"], ctx["sig_close"]
    out = {"bot": bot,
           "rule": "a signal is actionable only at/after its source bar close "
                   "(label + bar minutes); each signal bar sources at most one "
                   "entry; dataset signal-bar 0 is never consumed (k<1 warmup)",
           "entries_checked": 0, "last_closed_bar_without_matching_signal": 0,
           "side_mismatch": 0, "duplicate_source": 0, "entry_before_source_close": 0}
    gaps, seen = [], set()
    for t_ in trades:
        et = pd.Timestamp(t_["entry_time"])
        k = int(sc.searchsorted(et, side="right")) - 1
        if k < 1:
            out["last_closed_bar_without_matching_signal"] += 1
            continue
        if sc[k] > et:                    # impossible by construction; kept explicit
            out["entry_before_source_close"] += 1
            continue
        s = int(sig.iloc[k])
        if s == 0:
            out["last_closed_bar_without_matching_signal"] += 1
            continue
        if (s > 0) != (t_["side"] == "BUY"):
            out["side_mismatch"] += 1
            continue
        if k in seen:
            out["duplicate_source"] += 1
            continue
        seen.add(k)
        gaps.append((et - sc[k]).total_seconds())
        out["entries_checked"] += 1
    out["min_seconds_after_source_close"] = min(gaps) if gaps else None
    out["max_seconds_after_source_close"] = max(gaps) if gaps else None
    return out


timing = [timing_check("ichi", r_a.trades, CTX["ichi"]),
          timing_check("orb", r_b.trades, CTX["orb"])]
print("TIMING:", json.dumps(timing, default=str), flush=True)

# ================= determinism check =========================================
# the simulator is a pure function: regenerated A/B equity CSVs must be
# byte-identical to the committed inputs hashed in PART 0.
det = {"regenerated_A_equity_sha256": sha256_file(A_EQ),
       "regenerated_B_equity_sha256": sha256_file(B_EQ)}
det["matches_committed_inputs"] = bool(
    det["regenerated_A_equity_sha256"] == recon["inputs"]["A_equity_csv_sha256"]
    and det["regenerated_B_equity_sha256"] == recon["inputs"]["B_equity_csv_sha256"])
print("DETERMINISM:", json.dumps(det), flush=True)

json.dump({"cases": results, "identity": ident, "timing": timing,
           "determinism": det, "reconstruction": recon},
          open("_phase25c_allocation.json", "w"), indent=1, default=str)
print("DONE", flush=True)
