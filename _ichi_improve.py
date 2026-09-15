"""Ichimoku improvement driver — BUDGET-CAPPED (max 8 full runs).

Reuses THE one engine (_phase25c_core.simulate), the one dataset
(_sp2l_data_5y.pkl) and the allocation-driver build path verbatim
(Settings -> build_from_settings -> RiskConfig -> news/sess). Variants differ
ONLY in strategy params (Settings overrides) and/or RiskConfig fields.
ORB untouched; guard 3% stays ON in every run; capital 482.53 in every run.

Usage:  py -3.11 _ichi_improve.py <variant> <spread_pts>
Writes: _ichi_out/<variant>_sp<spread>_trades.csv / _equity.csv / _summary.json
"""
import json, pickle, sys
import pandas as pd

from loguru import logger
logger.remove()   # silence risk-manager warning flood (guard blocks are counted in results)

from _phase25c_core import SimConfig, simulate
from core import Settings
from core.risk_manager import RiskConfig
from backtest.engine import HistoricalNewsChecker
from strategies import build_from_settings

M5, M15, H1 = pickle.load(open("_sp2l_data_5y.pkl", "rb"))
CAP = 482.53

# variant -> (strategy param overrides, RiskConfig overrides)
VARIANTS = {
    # as-committed BASE behavior — flags pinned True so this stays reproducible
    # even though the proposed yaml now sets enable_kijun_short: false
    "baseline": ({"enable_kijun_short": True, "enable_tenkan_short": True}, {}),
    # change 1: kijun pullback layer long-only (SELL −$204.56, PF 0.793, 4/5 years)
    "no_kijun_short": ({"enable_kijun_short": False}, {}),
    # change 2: tenkan momentum layer long-only (SELL −$45.34, PF 0.894)
    "no_tenkan_short": ({"enable_tenkan_short": False}, {}),
    # combo (only if both changes individually help): fully long-only ichimoku
    "long_only": ({"enable_kijun_short": False, "enable_tenkan_short": False}, {}),
    # PROPOSED version = the committed yaml as-is (enable_kijun_short: false,
    # tenkan short untouched). Effective params identical to no_kijun_short.
    "proposed": ({}, {}),
}


def build_ichi(param_overrides=None, rc_overrides=None):
    s = Settings.load("config/settings_ichimoku.yaml")
    for k, v in (param_overrides or {}).items():
        s.set(f"strategy.params.ichimoku_m15.{k}", v)
    strat = build_from_settings(s)
    rc = RiskConfig.from_settings(s)
    rc.sl_atr_multiplier = 2.0          # VPS live config (allocation-driver parity)
    rc.max_forced_risk_percent = 3.0    # guard ON
    for k, v in (rc_overrides or {}).items():
        setattr(rc, k, v)
    news = HistoricalNewsChecker(filepath="data/historical_news.json", before_minutes=5,
                                 after_minutes=5, enabled=True, server_utc_offset_hours=3.0,
                                 after_minutes_tier1=120,
                                 tier1_patterns=["non-farm", "nfp", "cpi", "fomc", "federal funds", "powell"])
    sess = (int(s.get("session.start_hour", 0)), int(s.get("session.end_hour", 24)))
    return strat, rc, news, sess


def make_ctx(param_overrides, rc_overrides):
    strat, rc, news, sess = build_ichi(param_overrides, rc_overrides)
    p = strat.prepare_and_sign(M15, M15, H1)
    return {"sig": p["signal"], "atr": p["atr"], "layer": p["signal_layer"],
            "rc": rc, "news": news, "sess": sess,
            "sig_close": p.index + pd.Timedelta(minutes=15)}


def summarize(name, spread, r):
    nets = [t["pnl"] for t in r.trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    return {
        "variant": name, "spread_pts": spread, "capital": CAP,
        "trades": len(r.trades), "net": round(sum(nets), 2),
        "net_from_equity_delta": round(float(r.equity.iloc[-1] - CAP), 2),
        "pf": round(sum(wins) / abs(sum(losses)), 3) if losses and sum(losses) < 0 else None,
        "win_rate": round(len(wins) / max(1, len(nets)) * 100, 1),
        "avg_per_trade": round(sum(nets) / max(1, len(nets)), 2),
        "guard_blocks": r.guard_blocks, "margin_rejects": r.margin_rejects,
        "max_trade_risk_pct": r.max_trade_risk_pct, "dd": r.dd,
    }


def main():
    variant, spread = sys.argv[1], float(sys.argv[2])
    po, ro = VARIANTS[variant]
    ctx = make_ctx(po, ro)
    r = simulate(M5, M15, H1, {"ichi": ctx},
                 SimConfig(bots=["ichi"], shared=False, capital=CAP, guard_pct=3.0,
                           spread_pts=spread, gap_fill_at_open=True, leverage=100.0))
    tag = f"_ichi_out/{variant}_sp{int(spread)}"
    pd.DataFrame(r.trades).to_csv(f"{tag}_trades.csv", index=False)
    r.equity.to_frame("equity").to_csv(f"{tag}_equity.csv", index_label="bar_close_time")
    s = summarize(variant, spread, r)
    json.dump(s, open(f"{tag}_summary.json", "w"), indent=1, default=str)
    print(json.dumps(s, default=str), flush=True)


if __name__ == "__main__":
    main()
