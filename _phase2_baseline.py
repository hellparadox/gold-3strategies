"""PHASE-2 BASELINE MATRIX (research-only; NOT committed, NOT touching live path).

Runs the locked baseline grid on the cached 5y dataset:
  configs  A (era settings, guard OFF)  B (repo settings, guard 3%)
           C (VPS settings SL2.0, guard 3%)  D (VPS settings, guard OFF)
  bots     orb_gold (config/settings.yaml) + ichimoku_m15 (config/settings_ichimoku.yaml)
  costs    S1(8,0) S2(20,0) S3(26,0) S4(8,10) S5(26,10)  [points; point=0.01]
  extras   C at real VPS balance 482.53 (S2); break-even spread grid for C
Outputs  _phase2_results.json
"""
import json, math, pickle, time
import numpy as np
import pandas as pd

from core import Settings
from core.risk_manager import RiskConfig, SymbolSpec
from backtest.engine import BacktestConfig, BacktestEngine
from strategies import build_from_settings

M5, M15, H1 = pickle.load(open("_sp2l_data_5y.pkl", "rb"))
SPEC = SymbolSpec.gold_default()
BOTS = {"orb": "config/settings.yaml", "ichi": "config/settings_ichimoku.yaml"}
SCEN = {"S1_ideal8_slip0": (8, 0), "S2_est20_slip0": (20, 0), "S3_sp26_slip0": (26, 0),
        "S4_sp8_slip10": (8, 10), "S5_sp26_slip10": (26, 10)}

def engine_for(bot, risk_over, bt_over, balance=None):
    s = Settings.load(BOTS[bot])
    for k, v in risk_over.items():
        s.set(k, v)
    bt = {"backtest.commission_per_lot": 0.0, "session.max_spread_points": 999.0,
          "backtest.simulate_breakeven": True, "backtest.simulate_trailing": False,
          "backtest.simulate_partial": False}
    bt.update(bt_over)
    for k, v in bt.items():
        s.set(k, v)
    if balance is not None:
        s.set("backtest.initial_balance", balance)
    eng = BacktestEngine(build_from_settings(s), RiskConfig.from_settings(s),
                         BacktestConfig.from_settings(s), SPEC, symbol="XAUUSD",
                         timeframe="M5")
    return eng, s

def metrics(res):
    tr = res.trades
    n = len(tr)
    if n == 0:
        return {"trades": 0}
    nets = np.array([t.net_profit for t in tr])
    wins, losses = nets[nets > 0], nets[nets < 0]
    eq = np.array(res.equity_curve, dtype=float)
    if len(eq) == n + 1:
        eq = eq[1:]  # drop the initial-balance seed point
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    i_dd = int(np.argmax(dd))
    maxdd = float(-dd[i_dd]) if len(dd) else 0.0
    maxdd_pct = float(maxdd / peak[i_dd] * 100) if len(dd) and peak[i_dd] > 0 else 0.0
    # daily balance series -> sharpe/sortino (rf=0, annualized sqrt(252))
    bal = pd.Series(eq, index=pd.DatetimeIndex([t.exit_time for t in tr])).groupby(level=0).last()
    days = pd.date_range(bal.index[0], bal.index[-1], freq="D")
    daily = bal.reindex(days).ffill().fillna(res.initial_balance)
    ret = daily.pct_change().dropna()
    sharpe = float(ret.mean() / ret.std() * math.sqrt(252)) if len(ret) > 2 and ret.std() > 0 else None
    dn = ret[ret < 0]
    sortino = float(ret.mean() / dn.std() * math.sqrt(252)) if len(dn) > 2 and dn.std() > 0 else None
    # streaks
    sign = np.sign(nets)
    max_w = max_l = cur_w = cur_l = 0
    for x in sign:
        if x > 0:
            cur_w += 1; cur_l = 0
        else:
            cur_l += 1; cur_w = 0
        max_w, max_l = max(max_w, cur_w), max(max_l, cur_l)
    hold = [(t.exit_time - t.entry_time).total_seconds() / 60 for t in tr]
    r_mult = []
    bal_run = res.initial_balance
    risk_pcts = []
    for t in tr:
        risk = SPEC.money_per_lot(t.initial_sl_distance) * t.lot
        r_mult.append(t.net_profit / risk if risk > 0 else 0.0)
        risk_pcts.append(risk / bal_run * 100 if bal_run > 0 else 0.0)
        bal_run += t.net_profit
    risk_pcts = np.array(risk_pcts)
    longs = [t for t in tr if t.side == "BUY"]
    shorts = [t for t in tr if t.side == "SELL"]
    ydf = pd.DataFrame({"net": nets, "year": pd.DatetimeIndex([t.exit_time for t in tr]).year})
    yearly = {int(k): round(float(v), 2) for k, v in ydf.groupby("year")["net"].sum().items()}
    period_days = (M5.index[-1] - M5.index[0]).days
    return {
        "trades": n, "trades_per_month": round(n / (period_days / 30.44), 2),
        "net": round(float(nets.sum()), 2),
        "gross_profit": round(float(wins.sum()), 2), "gross_loss": round(float(losses.sum()), 2),
        "pf": round(float(wins.sum() / abs(losses.sum())), 3) if losses.sum() < 0 else None,
        "win_rate": round(len(wins) / n * 100, 2),
        "expectancy_usd": round(float(nets.mean()), 2),
        "expectancy_R": round(float(np.mean(r_mult)), 3),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else 0,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0,
        "payoff": round(float(wins.mean() / abs(losses.mean())), 3) if len(wins) and len(losses) else None,
        "max_dd_usd": round(maxdd, 2), "max_dd_pct": round(maxdd_pct, 2),
        "recovery_factor": round(float(nets.sum() / maxdd), 2) if maxdd > 0 else None,
        "sharpe_daily": round(sharpe, 3) if sharpe else None,
        "sortino_daily": round(sortino, 3) if sortino else None,
        "max_win_streak": max_w, "max_loss_streak": max_l,
        "avg_hold_hours": round(float(np.mean(hold)) / 60, 1),
        "pct_long": round(len(longs) / n * 100, 1),
        "long_net": round(sum(t.net_profit for t in longs), 2),
        "short_net": round(sum(t.net_profit for t in shorts), 2),
        "yearly": yearly,
        "exposure_pct": round(sum(hold) / (period_days * 24 * 60) * 100, 2),
        "avg_mae": round(float(np.mean([t.mae for t in tr])), 2),
        "avg_mfe": round(float(np.mean([t.mfe for t in tr])), 2),
        "risk_pct_min": round(float(risk_pcts.min()), 2) if n else None,
        "risk_pct_median": round(float(np.median(risk_pcts)), 2) if n else None,
        "risk_pct_mean": round(float(risk_pcts.mean()), 2) if n else None,
        "risk_pct_p90": round(float(np.percentile(risk_pcts, 90)), 2) if n else None,
        "risk_pct_max": round(float(risk_pcts.max()), 2) if n else None,
        "_trades": [(str(t.entry_time), t.side, round(t.net_profit, 2)) for t in tr],
    }

def main():
    t0 = time.time()
    out = {"env": {
        "commit": "dfb400f1b0e3997a5b8b7f249d66717b2e2d4658",
        "dataset_sha256_16": "de6e3843b0735cb9",
        "m5_bars": len(M5), "m5_from": str(M5.index[0]), "m5_to": str(M5.index[-1]),
        "python": "3.11.9", "pandas": pd.__version__, "numpy": np.__version__,
        "symbol": "XAUUSD (MetaQuotes-Demo feed, server time naive)",
        "tick_size": SPEC.tick_size, "tick_value": SPEC.tick_value,
        "point": SPEC.point, "contract": SPEC.contract_size,
        "min_lot": 0.01, "volume_step": 0.01, "commission_per_lot": 0.0,
        "swap_model": "NOT MODELED (engine has no swap)",
    }, "runs": {}}

    CONFIGS = {
        "A_era_guard0": {"risk.max_forced_risk_percent": 0.0},
        "B_repo_guard3": {"risk.max_forced_risk_percent": 3.0},
        "C_vps_guard3": {"risk.max_forced_risk_percent": 3.0, "risk.sl_atr_multiplier": 2.0},
        "D_vps_guard0": {"risk.max_forced_risk_percent": 0.0, "risk.sl_atr_multiplier": 2.0},
    }
    for cfg_name, risk_over in CONFIGS.items():
        for bot in BOTS:
            for sc_name, (sp, sl) in SCEN.items():
                key = f"{cfg_name}|{bot}|{sc_name}"
                t1 = time.time()
                eng, _ = engine_for(bot, risk_over, {"backtest.spread_points": float(sp),
                                                      "backtest.slippage_points": float(sl)})
                res = eng.run(M5, M15, H1)
                out["runs"][key] = metrics(res)
                print(f"{key}: net={out['runs'][key].get('net')} trades={out['runs'][key].get('trades')} ({time.time()-t1:.0f}s)", flush=True)

    # C at real VPS balance (guard interaction with 482.53)
    for bot in BOTS:
        eng, _ = engine_for(bot, CONFIGS["C_vps_guard3"], {"backtest.spread_points": 20.0,
                                                            "backtest.slippage_points": 0.0}, balance=482.53)
        res = eng.run(M5, M15, H1)
        out["runs"][f"C_vps_bal482|{bot}|S2_est20_slip0"] = metrics(res)
        print(f"C_vps_bal482|{bot}: net={out['runs'][f'C_vps_bal482|{bot}|S2_est20_slip0'].get('net')}", flush=True)

    # break-even grid for C (spread only)
    for sp in (35, 50, 70, 100):
        for bot in BOTS:
            eng, _ = engine_for(bot, CONFIGS["C_vps_guard3"], {"backtest.spread_points": float(sp),
                                                                "backtest.slippage_points": 0.0})
            res = eng.run(M5, M15, H1)
            out["runs"][f"C_vps_guard3|{bot}|BE_sp{sp}"] = metrics(res)
            print(f"BE sp{sp} {bot}: net={out['runs'][f'C_vps_guard3|{bot}|BE_sp{sp}'].get('net')}", flush=True)

    json.dump(out, open("_phase2_results.json", "w"), default=str)
    print(f"DONE in {time.time()-t0:.0f}s -> _phase2_results.json", flush=True)

if __name__ == "__main__":
    main()
