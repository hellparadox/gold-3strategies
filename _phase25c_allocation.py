"""PHASE 2.5c — CAPITAL-ALLOCATION ISOLATION + missing scenario (research-only).

ONE engine (_phase25c_core.simulate), ONE dataset, ONE exit-management (M5 grid,
BE/trailing next-bar rule), ONE cost model (fixed 20pts spread, slip 0, comm 0),
gap-aware fill ON, guard 3% ON — the scenario missing from phase 2.5 (V1+gap).

Cases (explicit capital):
  A) Ichimoku alone            — $482.53
  B) ORB alone                 — $482.53
  C) Independent accounts      — $482.53 EACH ($965.06 total) = A + B summed
  D) Shared account            — $482.53 TOTAL
The ONLY difference between C and D is account structure; trailing/management
identical, so any C-vs-D delta is attributable to the shared balance alone.
"""
import json, pickle
import pandas as pd

from _phase25c_core import SimConfig, simulate
from core import Settings
from core.risk_manager import RiskConfig
from backtest.engine import HistoricalNewsChecker
from strategies import build_from_settings

M5, M15, H1 = pickle.load(open("_sp2l_data_5y.pkl", "rb"))
CAP = 482.53

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

CFG = dict(capital=CAP, guard_pct=3.0, spread_pts=20.0, gap_fill_at_open=True, leverage=100.0)

def summarize(name, r, capital_total, trades_csv=None):
    nets = [t["pnl"] for t in r.trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    out = {
        "case": name, "capital_total": capital_total,
        "trades": len(r.trades), "net": round(sum(nets), 2),
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

r = simulate(M5, M15, H1, {"ichi": CTX["ichi"]}, SimConfig(bots=["ichi"], shared=False, **CFG))
results.append(summarize("A_ichi_alone_482", r, CAP, "_phase2_out/ALLOCATION_A_ichi_trades.csv"))
print(json.dumps(results[-1], default=str), flush=True)

r = simulate(M5, M15, H1, {"orb": CTX["orb"]}, SimConfig(bots=["orb"], shared=False, **CFG))
results.append(summarize("B_orb_alone_482", r, CAP, "_phase2_out/ALLOCATION_B_orb_trades.csv"))
print(json.dumps(results[-1], default=str), flush=True)

# C) independent: two separate accounts, each 482.53 -> sum of A and B paths
ra = simulate(M5, M15, H1, {"ichi": CTX["ichi"]}, SimConfig(bots=["ichi"], shared=False, **CFG))
rb = simulate(M5, M15, H1, {"orb": CTX["orb"]}, SimConfig(bots=["orb"], shared=False, **CFG))
eq = ra.equity + rb.equity - CAP  # two accounts -> portfolio equity = sum minus one duplicated base
nets_all = [t["pnl"] for t in ra.trades] + [t["pnl"] for t in rb.trades]
wins = [x for x in nets_all if x > 0]; losses = [x for x in nets_all if x < 0]
from _phase25c_core import _dd_details
results.append({
    "case": "C_independent_482_each", "capital_total": 2 * CAP,
    "trades": len(nets_all), "net": round(sum(nets_all), 2),
    "pf": round(sum(wins) / abs(sum(losses)), 3) if losses and sum(losses) < 0 else None,
    "win_rate": round(len(wins) / max(1, len(nets_all)) * 100, 1),
    "guard_blocks": ra.guard_blocks + rb.guard_blocks,
    "max_trade_risk_pct": max(ra.max_trade_risk_pct, rb.max_trade_risk_pct),
    "dd": _dd_details(eq, 2 * CAP),
    "note": "two SEPARATE accounts; equity series summed; each bot sized off its own balance",
})
print(json.dumps(results[-1], default=str), flush=True)

r = simulate(M5, M15, H1, CTX, SimConfig(bots=["orb", "ichi"], shared=True, **CFG))
results.append(summarize("D_shared_482_total", r, CAP, "_phase2_out/ALLOCATION_D_shared_trades.csv"))
print(json.dumps(results[-1], default=str), flush=True)

json.dump(results, open("_phase25c_allocation.json", "w"), indent=1, default=str)

# ---- point 3: spread-timing proof for a few live-config entries ----
sample = []
for t_ in r.trades[:5]:
    sample.append(t_)
print("SPREAD-TIMING SAMPLE (fixed-spread mode here; see test T5 for prev-bar rule):", flush=True)
print(json.dumps(sample, indent=1), flush=True)
print("DONE", flush=True)
