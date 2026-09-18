"""چرا ربات وارد نشد؟ — گزارش کندل‌به‌کندل برای یک روز مشخص (فقط خواندن؛ هیچ سفارشی ثبت نمی‌شود).

داده را از همان ترمینال MT5 می‌گیرد، همان استراتژی و همان فایل تنظیمات ربات را اجرا می‌کند و
برای هر کندل M15 نشان می‌دهد کدام شرط باز بوده و کدام بسته. ستون آخر می‌گوید سیگنال پذیرفته
شده یا نه، و اگر شده، ورود با گارد ریسک اجرا می‌شد یا رد می‌شد.

    py -3.11 tools/why_no_entry.py --config config/settings_ichimoku.yaml
    py -3.11 tools/why_no_entry.py --date 2026-09-18 --from 07:00 --to 18:00
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
from loguru import logger  # noqa: E402

import MetaTrader5 as mt5  # noqa: E402

from core import Settings  # noqa: E402
from core.risk_manager import RiskConfig, RiskManager, SymbolSpec  # noqa: E402
from strategies import build_from_settings  # noqa: E402


def rates(sym, tf, days):
    end = datetime.now()
    r = mt5.copy_rates_range(sym, tf, end - timedelta(days=days), end)
    if r is None or len(r) == 0:
        raise SystemExit(f"no rates for {sym}: {mt5.last_error()}")
    d = pd.DataFrame(r)
    d["time"] = pd.to_datetime(d["time"], unit="s")
    cols = [c for c in ("open", "high", "low", "close", "tick_volume", "spread") if c in d.columns]
    return d.set_index("time")[cols]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/settings_ichimoku.yaml")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today, server time)")
    ap.add_argument("--from", dest="t0", default="00:00")
    ap.add_argument("--to", dest="t1", default="23:59")
    ap.add_argument("--days", type=int, default=25, help="history to pull for the indicators")
    ap.add_argument("--symbol", default=None)
    a = ap.parse_args()
    logger.remove()

    s = Settings.load(a.config)
    if not mt5.initialize(**({"path": p} if (p := s.get("mt5.terminal_path")) else {})):
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        sym = a.symbol or s.get("symbol.name", "XAUUSD")
        if mt5.symbol_info(sym) is None:
            for c in [sym + "@"] + list(s.get("symbol.fallback_names", []) or []):
                if mt5.symbol_info(c) is not None:
                    sym = c
                    break
        mt5.symbol_select(sym, True)
        m15 = rates(sym, mt5.TIMEFRAME_M15, a.days)
        h1 = rates(sym, mt5.TIMEFRAME_H1, a.days)
        acc = mt5.account_info()
        bal = float(getattr(acc, "balance", 0.0) or s.get("risk.base_balance", 533.0))
        spec = SymbolSpec.from_mt5(mt5.symbol_info(sym))
        day = a.date or str(m15.index[-1].date())

        strat = build_from_settings(s)
        rc = RiskConfig.from_settings(s)
        rm = RiskManager(rc, spec)
        f = strat.prepare_and_sign(m15, m15, h1)
        p = strat.params
        atr = f["atr"]
        prev_mean = atr.shift(1).rolling(int(p["tenkan_atr_rising_lookback"])).mean()
        rng = (f.high - f.low).clip(lower=0.01)
        rows = []
        for t, r in f.loc[f"{day} {a.t0}":f"{day} {a.t1}"].iterrows():
            A = float(r["atr"]) if pd.notna(r["atr"]) else 0.0
            why_b, why_s = [], []
            if not r.close > r.ema_200: why_b.append("ema200")
            else: why_s.append("ema200")
            if not r.close > r.cloud_top: why_b.append("cloud")
            if not r.close < r.cloud_bottom: why_s.append("cloud")
            if not r.h1_trend_bull: why_b.append("h1")
            if not r.h1_trend_bear: why_s.append("h1")
            if not (r.tenkan > r.kijun): why_b.append("tk<=kj")
            if not (r.tenkan < r.kijun): why_s.append("tk>=kj")
            if not (A and atr[t] > prev_mean[t]): why_b.append("atr↓"); why_s.append("atr↓")
            dl, ds = r.close - r.kijun, r.kijun - r.close
            if A:
                if not (p["tenkan_min_distance_atr"] * A <= dl <= p["tenkan_max_distance_atr"] * A): why_b.append("dist")
                if not (p["tenkan_min_distance_atr"] * A <= ds <= p["tenkan_max_distance_atr"] * A): why_s.append("dist")
                if not (r.low <= r.tenkan + p["tenkan_near_atr"] * A): why_b.append("touch")
                if not (r.high >= r.tenkan - p["tenkan_near_atr"] * A): why_s.append("touch")
            if not ((r.close - r.open) > p["tenkan_min_body_fraction"] * rng[t]): why_b.append("body")
            if not ((r.open - r.close) > p["tenkan_min_body_fraction"] * rng[t]): why_s.append("body")
            sig = int(r["signal"])
            exe = ""
            if sig != 0:
                d = rm.entry_sl_distance(bal, A) if hasattr(rm, "entry_sl_distance") else rm.sl_distance(A)
                lot, risk = rm.calculate_lot(bal, d)
                exe = f"{'OK' if lot > 0 else 'GUARD'} risk ${risk:.2f} ({risk / bal * 100:.2f}%) SL {d:.2f}"
            rows.append({"bar": t.strftime("%H:%M"), "close": round(r.close, 2), "atr": round(A, 2),
                         "signal": {1: "BUY", -1: "SELL", 0: ""}[sig], "layer": r["signal_layer"],
                         "exec": exe,
                         "buy_blocked_by": ",".join(why_b) if why_b else "PASS",
                         "sell_blocked_by": ",".join(why_s) if why_s else "PASS"})
        pd.set_option("display.width", 250)
        print(f"symbol={sym} balance={bal:.2f} day={day} bars={len(rows)}  "
              f"(cooldown={p['cooldown_bars']} scope={p.get('cooldown_scope', 'global')} "
              f"maxpos={s.get('risk.max_positions_per_symbol')})")
        print(pd.DataFrame(rows).to_string(index=False))
        print("\nستون‌ها: شرط‌هایی که جلوی لایهٔ تنکان را گرفته‌اند. PASS یعنی همهٔ شرط‌ها باز بوده‌اند.")
        print("اگر signal خالی است ولی buy_blocked_by=PASS، یعنی کولداون یا اولویت لایه‌ها مانع شده.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
