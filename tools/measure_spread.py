"""اندازه‌گیری اسپرد واقعی روی ترمینال MT5 خودِ بروکر (فقط خواندن؛ هیچ سفارشی ثبت نمی‌شود).

اجرا:  py -3.11 tools/measure_spread.py --days 10
       py -3.11 tools/measure_spread.py --days 10 --symbol XAUUSD@ --out spread_wm.json
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import MetaTrader5 as mt5


def connect(path: str | None) -> None:
    kw = {}
    if path:
        kw["path"] = path
    login = os.environ.get("MT5_LOGIN")
    if login:
        kw.update(login=int(login), password=os.environ.get("MT5_PASSWORD", ""),
                  server=os.environ.get("MT5_SERVER", ""))
    if not mt5.initialize(**kw):
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")


def pick_symbol(name: str | None) -> str:
    if name:
        if not mt5.symbol_select(name, True):
            raise SystemExit(f"symbol not found/selectable: {name}")
        return name
    for cand in ("XAUUSD@", "XAUUSD", "GOLD", "XAUUSDm", "XAUUSD.a", "XAUUSD.pro"):
        if mt5.symbol_info(cand) is not None and mt5.symbol_select(cand, True):
            return cand
    raise SystemExit("no gold symbol found; pass --symbol")


def get_ticks(symbol: str, days: int) -> pd.DataFrame:
    end = datetime.now()
    start = end - timedelta(days=days)
    t = mt5.copy_ticks_range(symbol, start, end, mt5.COPY_TICKS_INFO)
    if t is None or len(t) == 0:
        t = mt5.copy_ticks_range(symbol, start, end, mt5.COPY_TICKS_ALL)
    if t is None or len(t) == 0:
        raise SystemExit(f"no ticks returned ({mt5.last_error()}); the terminal may not "
                         "have tick history for this symbol — try fewer days")
    df = pd.DataFrame(t)
    df["time"] = pd.to_datetime(df["time_msc"], unit="ms") if "time_msc" in df else \
        pd.to_datetime(df["time"], unit="s")
    return df.set_index("time")[["bid", "ask"]].astype("float64")


def m1_ranges(symbol: str, days: int) -> pd.Series:
    end = datetime.now()
    r = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, end - timedelta(days=days), end)
    if r is None or len(r) == 0:
        return pd.Series(dtype=float)
    d = pd.DataFrame(r)
    d["time"] = pd.to_datetime(d["time"], unit="s")
    return (d["high"] - d["low"]).set_axis(d["time"])


def q(s: pd.Series) -> dict:
    return {"n": int(s.size), "min": round(float(s.min()), 1), "median": round(float(s.median()), 1),
            "mean": round(float(s.mean()), 1), "p75": round(float(s.quantile(.75)), 1),
            "p90": round(float(s.quantile(.90)), 1), "p95": round(float(s.quantile(.95)), 1),
            "p99": round(float(s.quantile(.99)), 1), "max": round(float(s.max()), 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--path", default=os.environ.get("MT5_PATH") or None)
    ap.add_argument("--out", default="spread_report.json")
    a = ap.parse_args()

    connect(a.path)
    try:
        sym = pick_symbol(a.symbol)
        info = mt5.symbol_info(sym)
        acc = mt5.account_info()
        point = float(info.point or 0.01)
        ticks = get_ticks(sym, a.days)
        ticks = ticks[(ticks.bid > 0) & (ticks.ask > 0)]
        sp = ((ticks.ask - ticks.bid) / point).rename("spread_points")
        sp = sp[(sp >= 0) & (sp < 5000)]

        rep = {
            "symbol": sym, "point": point, "digits": int(info.digits),
            "server": getattr(acc, "server", ""), "company": getattr(acc, "company", ""),
            "account_type": {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(int(getattr(acc, "trade_mode", 0)), "?"),
            "ticks_from": str(ticks.index[0]), "ticks_to": str(ticks.index[-1]),
            "note": "spread in POINTS (1 point = symbol point); this is what the backtest calls spread_points",
            "overall": q(sp),
            "by_server_hour": {int(h): q(g) for h, g in sp.groupby(sp.index.hour) if len(g) > 50},
        }

        rng = m1_ranges(sym, a.days)
        if not rng.empty:
            thr = float(rng.quantile(0.95))
            fast = rng[rng >= thr].index.floor("min")
            mask = pd.Index(sp.index.floor("min")).isin(fast)
            if mask.sum() > 50:
                rep["during_fast_minutes"] = q(sp[mask])
                rep["fast_minute_threshold_price_range"] = round(thr, 2)
            rep["m1_range_median"] = round(float(rng.median()), 2)

        money_per_point = point * float(info.trade_contract_size or 100.0) * 0.01
        rep["cost_at_min_lot_usd"] = {
            "median_spread": round(rep["overall"]["median"] * money_per_point, 3),
            "p90_spread": round(rep["overall"]["p90"] * money_per_point, 3),
        }
        json.dump(rep, open(a.out, "w"), indent=1)
        print(json.dumps({k: rep[k] for k in ("symbol", "server", "account_type", "ticks_from",
                                              "ticks_to", "overall")}, indent=1))
        if "during_fast_minutes" in rep:
            print("during fast minutes:", json.dumps(rep["during_fast_minutes"]))
        print(f"\nwritten -> {a.out}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
