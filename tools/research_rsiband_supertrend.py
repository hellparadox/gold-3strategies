"""تست تاریخی «Supertrend (۹، hl2، ۳.۹) + خط میانی RSI Bands (LazyBear)» روی طلا — فقط پژوهش، به ربات‌ها دست نمی‌زند.

اندیکاتورها همان فرمول اسکریپت‌های TradingView هستند:
- Supertrend (KivancOzbilgic): src=hl2، ATR با RMA، trend با up/dn، Buy/Sell = برگشت trend.
- RSI Bands [LazyBear] (RSIBANDS_LB ۷۰/۳۰/۱۴): ub/lb/mid=(ub+lb)/2 — «RSI Midline» همان mid است.

بدون نگاه به آینده: سیگنال روی کندلِ بسته‌شده ساخته می‌شود و ورود با open کندل بعدی است (خرید با ask، فروش با bid).
اسپرد واقعی هر کندل (ستون spread از MT5، پوینت) یا --spread ثابت. هر معامله ۰.۰۱ لات (۱ دلار به ازای ۱ دلار حرکت).

حالت‌های ورود:  st = برگشت Supertrend · st_mid = برگشت Supertrend و close سمت درستِ خط میانی ·
                mid_cross = close خط میانی را در جهت روند Supertrend قطع کند
حالت‌های خروج:  flip = حد ضرر + خروج در برگشت مخالف Supertrend · sl2r = حد ضرر + هدف 2R + زمان‌سنج ·
                be_flip = مثل flip ولی بعد از +1R حد ضرر روی ورود
حد ضرر: فاصلهٔ ورود تا خط Supertrend، حداقل MIN_SL و حداکثر SL_CAP دلار (مثل سقف Scout).
گروه کنترل «contra»: همان ورودها با جهت برعکس — اگر هر دو سودده باشند، سود از سیگنال نیست (از رانش بازار است).

اجرا روی VPS (MT5 دارد):
    python tools/research_rsiband_supertrend.py --mt5            # M1/M5/M15 مستقیم از MT5 (تا ۱۰۰٬۰۰۰ کندل هرکدام)
    python tools/research_rsiband_supertrend.py --mt5 --long-pkl _sp2l_data_5y.pkl      # M5/M15 پنج‌ساله هم
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

MIN_SL = 2.0            # حداقل فاصلهٔ حد ضرر (دلار قیمت) — مثل MIN_STOP_PRICE اسکات
SL_CAP = 10.0           # سقف حد ضرر (دلار قیمت) — مثل سقف تنظیمات اسکات با ۰.۰۱ لات
USD_PER_PRICE = 1.0     # ۰.۰۱ لات: ۱ دلار سود/زیان به ازای ۱ دلار حرکت قیمت
WARMUP = 600            # کندل‌های اول (اندیکاتورها هنوز جا نیفتاده‌اند) معامله نمی‌شوند
HORIZON = {"M1": 720, "M5": 144, "M15": 48}      # ۱۲ ساعت
ENTRIES = ("st", "st_mid", "mid_cross")
EXITS = ("flip", "sl2r", "be_flip")


# ------------------------------------------------------------------ اندیکاتورها
def rma(x: np.ndarray, n: int) -> np.ndarray:
    """Pine ta.rma: آلفا=1/n، بذر = میانگین ساده‌ی n مقدار اول."""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = np.nanmean(x[:n])
    a = 1.0 / n
    for i in range(n, len(x)):
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
    return out


def supertrend(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 9,
               mult: float = 3.9) -> Dict[str, np.ndarray]:
    """Supertrend KivancOzbilgic (src=hl2، ATR با RMA). خروجی: trend (+1/−1)، up، dn، buy، sell، line."""
    n = len(close)
    pc = np.r_[close[0], close[:-1]]
    tr = np.maximum.reduce([high - low, np.abs(high - pc), np.abs(low - pc)])
    atr = rma(tr, period)
    src = (high + low) / 2.0
    up_raw, dn_raw = src - mult * atr, src + mult * atr
    up, dn = np.full(n, np.nan), np.full(n, np.nan)
    trend = np.ones(n, dtype=int)
    for i in range(n):
        if np.isnan(atr[i]):
            continue
        u1 = up[i - 1] if i > 0 and not np.isnan(up[i - 1]) else up_raw[i]
        d1 = dn[i - 1] if i > 0 and not np.isnan(dn[i - 1]) else dn_raw[i]
        c1 = close[i - 1] if i > 0 else close[i]
        up[i] = max(up_raw[i], u1) if c1 > u1 else up_raw[i]
        dn[i] = min(dn_raw[i], d1) if c1 < d1 else dn_raw[i]
        t = trend[i - 1] if i > 0 else 1
        if t == -1 and close[i] > d1:
            t = 1
        elif t == 1 and close[i] < u1:
            t = -1
        trend[i] = t
    prev = np.r_[trend[0], trend[:-1]]
    valid = ~np.isnan(atr)
    buy = valid & (trend == 1) & (prev == -1)
    sell = valid & (trend == -1) & (prev == 1)
    line = np.where(trend == 1, up, dn)
    return {"trend": trend, "up": up, "dn": dn, "buy": buy, "sell": sell, "line": line, "atr": atr}


def rsi_bands(close: np.ndarray, length: int = 14, ob: float = 70.0, os_: float = 30.0) -> Dict[str, np.ndarray]:
    """RSI Bands [LazyBear]: قیمتی که اگر کندل بعدی آنجا ببندد RSI برابر ob / os_ می‌شود. mid = میانگین دو باند."""
    c = pd.Series(close, dtype="float64")
    up = c.diff().clip(lower=0.0)
    dn = (-c.diff()).clip(lower=0.0)
    alpha = 2.0 / ((2 * length - 1) + 1)                 # ema(…، 2*length-1) = Wilder
    auc = up.ewm(alpha=alpha, adjust=False).mean().to_numpy()
    adc = dn.ewm(alpha=alpha, adjust=False).mean().to_numpy()

    def band(level: float) -> np.ndarray:
        x = (length - 1) * (adc * level / (100.0 - level) - auc)
        return np.where(x >= 0, close + x, close + x * (100.0 - level) / level)
    ub, lb = band(ob), band(os_)
    rsi50 = close + (length - 1) * (adc - auc)           # قیمت RSI=50 (برای مقایسه؛ «Midline» اسکریپت mid است)
    return {"ub": ub, "lb": lb, "mid": (ub + lb) / 2.0, "rsi50": rsi50}


# ------------------------------------------------------------------ سیگنال‌ها
def entry_signals(mode: str, st: Dict[str, np.ndarray], bands: Dict[str, np.ndarray],
                  close: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """(buy، sell) روی کندل بسته‌شده."""
    mid = bands["mid"]
    if mode == "st":
        return st["buy"].copy(), st["sell"].copy()
    if mode == "st_mid":
        return st["buy"] & (close > mid), st["sell"] & (close < mid)
    if mode == "mid_cross":
        pc, pm = np.r_[close[0], close[:-1]], np.r_[mid[0], mid[:-1]]
        up = (pc <= pm) & (close > mid) & (st["trend"] == 1)
        dn = (pc >= pm) & (close < mid) & (st["trend"] == -1)
        valid = ~np.isnan(st["atr"])
        return up & valid, dn & valid
    raise ValueError(mode)


# ------------------------------------------------------------------ شبیه‌ساز
def simulate(df: pd.DataFrame, buy: np.ndarray, sell: np.ndarray, st: Dict[str, np.ndarray], exit_mode: str,
             horizon: int, spread_default: float = 0.20, spread_mult: float = 1.0, contra: bool = False,
             tf: str = "M1") -> pd.DataFrame:
    """یک پوزیشن هم‌زمان. ورود open کندل بعد (+اسپرد برای خرید). حد ضرر قبل از هدف در یک کندل."""
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(df)
    sp = (df["spread"].to_numpy(float) * 0.01) if "spread" in df.columns else np.full(n, spread_default)
    sp = np.where(sp > 0, sp, spread_default) * spread_mult
    idx = df.index
    flips = np.flatnonzero(st["buy"] | st["sell"])
    cand = np.flatnonzero((buy | sell))
    cand = cand[(cand >= WARMUP) & (cand < n - 2)]
    rows: List[dict] = []
    free = 0
    for t in cand:
        if t < free:
            continue
        sig = 1 if buy[t] else -1                                  # جهت سیگنال
        side = -sig if contra else sig                             # جهت معامله (کنترل = برعکس)
        e = t + 1
        line = st["line"][t]
        if np.isnan(line):
            continue
        entry = o[e] + (sp[e] if side == 1 else 0.0)             # خرید با ask، فروش با bid
        dist = float(np.clip(abs(c[t] - line), MIN_SL, SL_CAP))
        sl = entry - dist if side == 1 else entry + dist
        tp = entry + 2 * dist if side == 1 else entry - 2 * dist
        end = min(n - 1, e + (horizon if exit_mode == "sl2r" else 20 * horizon))
        j = np.searchsorted(flips, e, side="right")              # برگشتِ مخالفِ جهت سیگنال بعد از کندل ورود
        flip_at = None
        for k in flips[j:]:
            if k > end:
                break
            if (sig == 1 and st["sell"][k]) or (sig == -1 and st["buy"][k]):
                flip_at = k
                break
        exit_px, why, last = None, None, end
        be = False
        stop = sl
        limit = end if (exit_mode == "sl2r" or flip_at is None) else min(flip_at, end)
        for k in range(e, limit + 1):
            if side == 1:
                hit_sl = l[k] <= stop
                hit_tp = exit_mode == "sl2r" and h[k] >= tp
                fav = h[k] - entry
            else:
                hit_sl = (h[k] + sp[k]) >= stop                    # فروش با ask بسته می‌شود
                hit_tp = exit_mode == "sl2r" and (l[k] + sp[k]) <= tp
                fav = entry - (l[k] + sp[k])
            if hit_sl:                                             # حد ضرر اول (محافظه‌کارانه)
                exit_px, why, last = stop, "BE" if be else "SL", k
                break
            if hit_tp:
                exit_px, why, last = tp, "TP", k
                break
            if exit_mode == "be_flip" and not be and fav >= dist:
                be = True
                stop = entry
        if exit_px is None:
            if exit_mode != "sl2r" and flip_at is not None:
                last = min(flip_at + 1, n - 1)                      # فرمان در close کندل flip، اجرا در open بعدی
                why = "FLIP"
                exit_px = o[last] if side == 1 else o[last] + sp[last]
            else:
                last = end                                          # زمان‌سنج / آخر داده: close همان کندل
                why = "TIME" if exit_mode == "sl2r" else "END"
                exit_px = c[last] if side == 1 else c[last] + sp[last]
        pnl = (exit_px - entry) if side == 1 else (entry - exit_px)
        rows.append({"time": idx[e], "side": "BUY" if side == 1 else "SELL", "entry": entry, "exit": exit_px,
                     "dist": dist, "why": why, "bars": int(last - e), "pnl": pnl, "R": pnl / dist,
                     "usd": pnl * USD_PER_PRICE, "hour": idx[e].hour})
        free = last - 1 if why == "FLIP" else last               # برگشت و ورود جدید هم‌زمان در open بعدی
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ آمار
def boot_ci(x: np.ndarray, n_boot: int = 2000, seed: int = 7) -> Tuple[float, float]:
    if len(x) < 20:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    m = rng.choice(x, size=(n_boot, len(x)), replace=True).mean(axis=1)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def stats(tr: pd.DataFrame) -> Dict[str, float]:
    if tr is None or len(tr) == 0:
        return {"n": 0}
    r, u = tr["R"].to_numpy(), tr["usd"].to_numpy()
    lo, hi = boot_ci(r)
    gp, gl = u[u > 0].sum(), -u[u < 0].sum()
    eq = np.cumsum(u)
    dd = float((np.maximum.accumulate(eq) - eq).max())
    thirds = [float(tr.iloc[ix]["R"].mean()) if len(ix) else float("nan")
              for ix in np.array_split(np.arange(len(tr)), 3)]
    return {"n": int(len(tr)), "win": round(float((r > 0).mean() * 100), 1), "avgR": round(float(r.mean()), 3),
            "ciLo": round(lo, 3), "ciHi": round(hi, 3), "pf": round(float(gp / gl), 2) if gl > 0 else float("inf"),
            "usd": round(float(u.sum()), 1), "maxDD": round(dd, 1),
            "t1": round(thirds[0], 3), "t2": round(thirds[1], 3), "t3": round(thirds[2], 3),
            "buyR": round(float(tr[tr.side == "BUY"]["R"].mean()), 3) if (tr.side == "BUY").any() else float("nan"),
            "sellR": round(float(tr[tr.side == "SELL"]["R"].mean()), 3) if (tr.side == "SELL").any() else float("nan"),
            "avgBars": round(float(tr["bars"].mean()), 1)}


def verdict(s: Dict[str, float], s_stress: Dict[str, float]) -> bool:
    """قبول: حداقل ۲۰۰ معامله، حد پایین بازهٔ اطمینان > ۰، هر سه‌ثلث مثبت، و با اسپرد ۱٫۵ برابر هنوز مثبت."""
    if s.get("n", 0) < 200 or s_stress.get("n", 0) == 0:
        return False
    return bool(s["ciLo"] > 0 and min(s["t1"], s["t2"], s["t3"]) > 0 and s_stress["avgR"] > 0)


# ------------------------------------------------------------------ اجرا
def resample(m1: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "spread" in m1.columns:
        agg["spread"] = "mean"
    r = m1.resample(rule).agg(agg).dropna(subset=["open"])
    return r


def run_one(df: pd.DataFrame, tf: str, spread_default: float,
            label: Optional[str] = None) -> Tuple[List[dict], Dict[str, pd.DataFrame]]:
    label = label or tf
    c = df["close"].to_numpy(float)
    st = supertrend(df["high"].to_numpy(float), df["low"].to_numpy(float), c, 9, 3.9)
    bands = rsi_bands(c, 14, 70.0, 30.0)
    rows, trades = [], {}
    for em in ENTRIES:
        b, s = entry_signals(em, st, bands, c)
        for xm in EXITS:
            key = f"{label}|{em}|{xm}"
            base = simulate(df, b, s, st, xm, HORIZON[tf], spread_default, 1.0, False, tf)
            stress = simulate(df, b, s, st, xm, HORIZON[tf], spread_default, 1.5, False, tf)
            ctrl = simulate(df, b, s, st, xm, HORIZON[tf], spread_default, 1.0, True, tf)
            ss, sx, sc = stats(base), stats(stress), stats(ctrl)
            rows.append({"key": key, "tf": label, "entry": em, "exit": xm, **ss,
                         "stressAvgR": sx.get("avgR"), "contraAvgR": sc.get("avgR"), "PASS": verdict(ss, sx)})
            trades[key] = base
    return rows, trades


MT5_MAX_BARS = 100000       # سقف پیش‌فرض ترمینال (Tools → Options → Charts → Max bars in chart)


def load_mt5(bars: int, symbol: Optional[str], tf: str = "M1") -> pd.DataFrame:
    """کندل‌های خود MT5 برای یک تایم‌فریم (M1/M5/M15). اگر ترمینال تعداد درخواستی را نداد، کمتر می‌خواهد."""
    import MetaTrader5 as mt5                       # فقط روی VPS لازم است
    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        sym = None
        for cand in ([symbol] if symbol else []) + ["XAUUSD@", "XAUUSD", "GOLD", "XAUUSDm", "XAUUSD.a", "XAUUSD.pro"]:
            if mt5.symbol_info(cand) is not None and mt5.symbol_select(cand, True):
                sym = cand
                break
        if not sym:
            names = [x.name for x in (mt5.symbols_get() or []) if "XAU" in x.name.upper() or "GOLD" in x.name.upper()]
            raise SystemExit(f"نماد طلا پیدا نشد؛ نمادهای شبیه: {names}. با --symbol بدهید")
        code = getattr(mt5, f"TIMEFRAME_{tf}")
        raw, n = None, int(bars)
        while n >= 1000:
            raw = mt5.copy_rates_from_pos(sym, code, 0, n)
            if raw is not None and len(raw) > 0:
                break
            n //= 2                                    # بیش از سقف max-bars ترمینال: نصف کن
        info = mt5.terminal_info()
    finally:
        mt5.shutdown()
    if raw is None or len(raw) == 0:
        raise SystemExit(f"MT5 برای {sym} {tf} کندلی نداد (max-bars ترمینال: "
                         f"{getattr(info, 'maxbars', '?')}؛ خطا: {mt5.last_error()})")
    print(f"{tf}: نماد {sym}، {len(raw)} کندل", flush=True)
    d = pd.DataFrame(raw)
    d["time"] = pd.to_datetime(d["time"], unit="s")
    d = d.set_index("time").sort_index()
    return d[~d.index.duplicated(keep="last")][["open", "high", "low", "close", "spread"]].astype("float64")


def fmt_table(rows: List[dict], top: int = 40) -> str:
    cols = ["key", "n", "win", "avgR", "ciLo", "ciHi", "pf", "usd", "maxDD", "t1", "t2", "t3", "buyR", "sellR",
            "stressAvgR", "contraAvgR", "PASS"]
    df = pd.DataFrame(rows)
    df = df[[c for c in cols if c in df.columns]].sort_values("avgR", ascending=False).head(top)
    return df.to_string(index=False)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mt5", action="store_true", help="M1، M5 و M15 را هرکدام مستقیم از MT5 بخوان")
    ap.add_argument("--bars", type=int, default=MT5_MAX_BARS,
                    help="کندل برای هر تایم‌فریم؛ ترمینال معمولاً حداکثر ۱۰۰٬۰۰۰ می‌دهد")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--m1-pkl", default=None, help="DataFrame M1 (open/high/low/close[/spread]) به‌جای MT5")
    ap.add_argument("--long-pkl", default=None, help="pickle پنج‌ساله (M5, M15, H1) برای M5/M15 طولانی")
    ap.add_argument("--spread", type=float, default=0.20, help="اسپرد ثابت (دلار) وقتی ستون spread نیست")
    ap.add_argument("--out", default="_rsiband_out")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    frames: List[Tuple[str, pd.DataFrame]] = []
    if a.mt5:
        for tf in ("M1", "M5", "M15"):                 # هر تایم‌فریم تاریخچهٔ خودش را دارد: M15 حدود ۴ سال
            d = load_mt5(a.bars, a.symbol, tf)
            print(f"   {d.index[0]} → {d.index[-1]}", flush=True)
            frames.append((tf, d))
    elif a.m1_pkl:
        m1 = pd.read_pickle(a.m1_pkl)
        print(f"M1: {len(m1)} کندل  {m1.index[0]} → {m1.index[-1]}", flush=True)
        frames += [("M1", m1), ("M5", resample(m1, "5min")), ("M15", resample(m1, "15min"))]
    if a.long_pkl:
        m5, m15, _h1 = pd.read_pickle(a.long_pkl)
        frames += [("M5", m5.rename(columns=str.lower)), ("M15", m15.rename(columns=str.lower))]
        print(f"long M5: {len(m5)}  M15: {len(m15)}", flush=True)
    if not frames:
        print("داده‌ای نیست: --mt5 یا --m1-pkl یا --long-pkl را بدهید")
        return 2

    all_rows: List[dict] = []
    seen: Dict[str, int] = {}
    for tf, df in frames:
        label = tf if tf not in seen else f"{tf}-long"
        seen[tf] = seen.get(tf, 0) + 1
        print(f"--- {label}: {len(df)} کندل", flush=True)
        rows, trades = run_one(df, tf, a.spread, label)
        for k, v in trades.items():
            v.to_csv(out / f"trades_{k.replace('|', '_')}.csv", index=False)
        all_rows += rows
        print(fmt_table(rows, 9), flush=True)

    (out / "summary.json").write_text(json.dumps(all_rows, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    txt = fmt_table(all_rows, 60)
    (out / "summary.txt").write_text(txt, encoding="utf-8")
    print("\n===== همهٔ ترکیب‌ها (مرتب‌شده با avgR) =====\n" + txt)
    passed = [r["key"] for r in all_rows if r.get("PASS")]
    print("\nقبول‌شده‌ها:", passed if passed else "هیچ‌کدام")
    print(f"فایل‌ها: {out}/summary.txt  summary.json  trades_*.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
