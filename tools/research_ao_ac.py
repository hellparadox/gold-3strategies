"""تست صادقانهٔ Awesome Oscillator (AO) + Accelerator Oscillator (AC) بیل ویلیامز روی طلای خود بروکر.

فقط تحقیق: هیچ سفارشی نمی‌دهد و به اسکات/دفتر دست نمی‌زند. همان معیار تست‌های قبلی (Supertrend/RSI Bands):
ورود open کندل بعد از سیگنال (خرید با ask)، اسپرد واقعی هر کندل، حد ضرر قبل از هدف وقتی هر دو در یک کندل‌اند،
۰.۰۱ لات (۱ دلار به ازای ۱ دلار حرکت)، حد ضرر بین ۲ و ۱۰ دلار (مثل سقف اسکات)، یک پوزیشن هم‌زمان.

    AO = SMA5(median) − SMA34(median)      median = (high + low) / 2
    AC = AO − SMA5(AO)

ورودها (روی کندل بسته‌شده):
  ao_zero   AO از صفر رد می‌شود (بالا = خرید، پایین = فروش)
  ao_ac     AO در جهت معامله است و AC از صفر در همان جهت رد می‌شود (مومنتوم + شتاب، ترکیب پرامپت)
  ac_2      AC دو کندل پشت سر هم در یک جهت و هم‌علامت با آن (قاعدهٔ «دو میلهٔ سبز/قرمز» ویلیامز)
  saucer    «نعلبکی» AO: بالای صفر، یک میلهٔ قرمز بعد سبز = خرید؛ آینه‌اش فروش
خروج‌ها: r1 (هدف ۱R) · r2 (هدف ۲R) · acflip (برگشت علامت AC خلاف معامله). همه با حد ضرر و زمان‌سنج.
حد ضرر = 1.5 × ATR14 (بین ۲ و ۱۰ دلار).

قبول (PASS) مثل قبل: حداقل ۲۰۰ معامله، حد پایین بازهٔ اطمینان میانگین R > ۰، هر سه‌ثلث دوره مثبت، و با
اسپرد ۱٫۵ برابر هنوز مثبت. «contraAvgR» = همان سیگنال برعکس (کنترل). چون ۲۴ ترکیب امتحان می‌شود،
یک PASS تنها را هم باید با احتیاط دید.

اجرا روی VPS با پایتون ربات‌ها (MetaTrader5 دارد؛ فقط خواندن کندل، چند دقیقه، با کمترین اولویت):
    py -3.11 tools/research_ao_ac.py --mt5
یا روی هر CSV کندل (ستون‌های time,open,high,low,close[,spread]):
    py -3.11 tools/research_ao_ac.py --csv _kronos_out/xau_m15.csv --tf M15
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import research_rsiband_supertrend as rr  # noqa: E402

ENTRIES = ("ao_zero", "ao_ac", "ac_2", "saucer")
EXITS = ("r1", "r2", "acflip")
HORIZON = {"M5": 48, "M15": 16}          # زمان‌سنج: ۴ ساعت
STOP_ATR = 1.5


def sma(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x, dtype="float64").rolling(n, min_periods=n).mean().to_numpy()


def ao_ac(high: np.ndarray, low: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    med = (np.asarray(high, float) + np.asarray(low, float)) / 2.0
    ao = sma(med, 5) - sma(med, 34)
    ac = ao - sma(ao, 5)
    return ao, ac


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    pc = np.r_[np.nan, close[:-1]]
    tr = np.nanmax(np.vstack([high - low, np.abs(high - pc), np.abs(low - pc)]), axis=0)
    return rr.rma(tr, n)


def _prev(x: np.ndarray, k: int = 1) -> np.ndarray:
    return np.r_[np.full(k, np.nan), x[:-k]]


def signals(mode: str, ao: np.ndarray, ac: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """(buy, sell) روی کندل بسته‌شده؛ NaN همیشه False."""
    with np.errstate(invalid="ignore"):
        ao1, ac1, ac2, ao2 = _prev(ao), _prev(ac), _prev(ac, 2), _prev(ao, 2)
        if mode == "ao_zero":
            b, s = (ao1 <= 0) & (ao > 0), (ao1 >= 0) & (ao < 0)
        elif mode == "ao_ac":
            b, s = (ao > 0) & (ac1 <= 0) & (ac > 0), (ao < 0) & (ac1 >= 0) & (ac < 0)
        elif mode == "ac_2":
            up = (ac > ac1) & (ac1 > ac2) & (ac > 0)
            dn = (ac < ac1) & (ac1 < ac2) & (ac < 0)
            b, s = up & ~_prev(up.astype(float)).astype(bool), dn & ~_prev(dn.astype(float)).astype(bool)
        elif mode == "saucer":
            b = (ao > 0) & (ao1 > 0) & (ao2 > 0) & (ao1 < ao2) & (ao > ao1)
            s = (ao < 0) & (ao1 < 0) & (ao2 < 0) & (ao1 > ao2) & (ao < ao1)
        else:
            raise ValueError(mode)
    return np.nan_to_num(b).astype(bool), np.nan_to_num(s).astype(bool)


def simulate(df: pd.DataFrame, buy: np.ndarray, sell: np.ndarray, ac: np.ndarray, atr_: np.ndarray,
             exit_mode: str, horizon: int, spread_default: float = 0.20, spread_mult: float = 1.0,
             contra: bool = False) -> pd.DataFrame:
    """یک پوزیشن هم‌زمان؛ ورود open کندل بعد؛ حد ضرر قبل از هدف در یک کندل (محافظه‌کارانه)."""
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(df)
    sp = (df["spread"].to_numpy(float) * 0.01) if "spread" in df.columns else np.full(n, spread_default)
    sp = np.where(sp > 0, sp, spread_default) * spread_mult
    idx = df.index
    cand = np.flatnonzero(buy | sell)
    cand = cand[(cand >= max(rr.WARMUP, 40)) & (cand < n - 2)]
    rows: List[dict] = []
    free = 0
    for t in cand:
        if t < free or np.isnan(atr_[t]):
            continue
        sig = 1 if buy[t] else -1
        side = -sig if contra else sig
        e = t + 1
        entry = o[e] + (sp[e] if side == 1 else 0.0)
        dist = float(np.clip(STOP_ATR * atr_[t], rr.MIN_SL, rr.SL_CAP))
        stop = entry - dist if side == 1 else entry + dist
        mult = 1.0 if exit_mode == "r1" else 2.0
        tp = entry + mult * dist if side == 1 else entry - mult * dist
        end = min(n - 1, e + (horizon if exit_mode != "acflip" else 3 * horizon))
        exit_px, why, last = None, None, end
        for k in range(e, end + 1):
            if side == 1:
                hit_sl, hit_tp = l[k] <= stop, exit_mode != "acflip" and h[k] >= tp
            else:
                hit_sl = (h[k] + sp[k]) >= stop                        # فروش با ask بسته می‌شود
                hit_tp = exit_mode != "acflip" and (l[k] + sp[k]) <= tp
            if hit_sl:
                exit_px, why, last = stop, "SL", k
                break
            if hit_tp:
                exit_px, why, last = tp, "TP", k
                break
            if exit_mode == "acflip" and k < n - 1 and not np.isnan(ac[k]) and ac[k] * sig < 0 and k > e:
                last = k + 1                                           # فرمان در close، اجرا open بعدی
                exit_px, why = (o[last] if side == 1 else o[last] + sp[last]), "FLIP"
                break
        if exit_px is None:
            last = end
            why = "TIME"
            exit_px = c[last] if side == 1 else c[last] + sp[last]
        pnl = (exit_px - entry) if side == 1 else (entry - exit_px)
        rows.append({"time": idx[e], "side": "BUY" if side == 1 else "SELL", "entry": entry, "exit": exit_px,
                     "dist": dist, "why": why, "bars": int(last - e), "pnl": pnl, "R": pnl / dist,
                     "usd": pnl * rr.USD_PER_PRICE, "hour": idx[e].hour})
        free = last + 1
    return pd.DataFrame(rows)


def run_one(df: pd.DataFrame, tf: str, spread_default: float = 0.20) -> Tuple[List[dict], Dict[str, pd.DataFrame]]:
    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    ao, ac = ao_ac(h, l)
    at = atr(h, l, c, 14)
    rows, trades = [], {}
    for em in ENTRIES:
        b, s = signals(em, ao, ac)
        for xm in EXITS:
            key = f"{tf}|{em}|{xm}"
            args = (df, b, s, ac, at, xm, HORIZON.get(tf, 16), spread_default)
            base = simulate(*args, 1.0, False)
            stress = simulate(*args, 1.5, False)
            ctrl = simulate(*args, 1.0, True)
            ss, sx, sc = rr.stats(base), rr.stats(stress), rr.stats(ctrl)
            rows.append({"key": key, "tf": tf, "entry": em, "exit": xm, **ss, "stressAvgR": sx.get("avgR"),
                         "contraAvgR": sc.get("avgR"), "PASS": rr.verdict(ss, sx)})
            trades[key] = base
    return rows, trades


def load_csv(path: str) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["time"] = pd.to_datetime(d["time"])
    d = d.drop_duplicates("time", keep="last").sort_values("time").set_index("time")
    cols = ["open", "high", "low", "close"] + (["spread"] if "spread" in d.columns else [])
    return d[cols].astype("float64")


def lower_priority() -> None:
    """سر پردازنده با اسکات رقابت نکند (VPS یک هسته دارد)."""
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32                                 # type: ignore[attr-defined]
            k.SetPriorityClass(k.GetCurrentProcess(), 0x00000040)       # IDLE_PRIORITY_CLASS
        else:
            os.nice(19)
    except Exception:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="AO + AC honest test on gold (research only)")
    ap.add_argument("--mt5", action="store_true", help="M5 و M15 مستقیم از MT5 (فقط خواندن)")
    ap.add_argument("--bars", type=int, default=100000)
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--csv", default=None, help="CSV کندل به‌جای MT5")
    ap.add_argument("--tf", default="M15", choices=("M5", "M15"), help="تایم‌فریم CSV")
    ap.add_argument("--spread", type=float, default=0.20, help="اسپرد ثابت (دلار) وقتی ستون spread نیست")
    ap.add_argument("--out", default="_aoac_out")
    a = ap.parse_args(argv)
    lower_priority()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    frames: List[Tuple[str, pd.DataFrame]] = []
    if a.mt5:
        for tf in ("M5", "M15"):
            d = rr.load_mt5(a.bars, a.symbol, tf)
            print(f"   {d.index[0]} → {d.index[-1]}", flush=True)
            frames.append((tf, d))
    elif a.csv:
        frames.append((a.tf, load_csv(a.csv)))
    if not frames:
        print("داده‌ای نیست: --mt5 یا --csv بدهید")
        return 2
    all_rows: List[dict] = []
    for tf, df in frames:
        print(f"--- {tf}: {len(df)} کندل", flush=True)
        rows, trades = run_one(df, tf, a.spread)
        for k, v in trades.items():
            v.to_csv(out / f"trades_{k.replace('|', '_')}.csv", index=False)
        all_rows += rows
    (out / "summary.json").write_text(json.dumps(all_rows, ensure_ascii=False, indent=1, default=float),
                                      encoding="utf-8")
    txt = rr.fmt_table(all_rows, 60)
    passed = [r["key"] for r in all_rows if r.get("PASS")]
    txt += "\n\nPASS: " + (", ".join(passed) if passed else "none")
    (out / "summary.txt").write_text(txt, encoding="utf-8")
    print("\n===== AO + AC (sorted by avgR) =====\n" + txt)
    print(f"\nsaved {out / 'summary.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
