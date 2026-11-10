"""تست صادقانهٔ Kronos (مدل پایهٔ پیش‌بینی کندل، github.com/shiyu-coder/Kronos) روی طلای خود بروکر.

فقط تحقیق است: هیچ سفارشی نمی‌دهد، به اسکات/ربات‌ها/دفتر دست نمی‌زند و هیچ ماژولی از پروژه را import نمی‌کند.
همهٔ خروجی‌ها در پوشهٔ _kronos_out/ کنار پروژه نوشته می‌شوند.

دو مرحله، با دو پایتون جدا (تا torch هیچ‌وقت در محیط ربات‌ها نصب نشود):

  1) export — با همان پایتون ربات‌ها (MetaTrader5 دارد). کندل‌های M15 را یک بار از MT5 می‌خواند و CSV می‌کند؛
     مثل scout_signal_audit فقط initialize/copy_rates/shutdown است و ربات‌ها روشن می‌مانند.
         py -3.11 tools/research_kronos.py export --bars 30000

  2) run — با پایتونِ venv جدای Kronos (torch + کد Kronos). MT5 را اصلاً صدا نمی‌زند؛ فقط CSV می‌خواند.
         C:\\Users\\Administrator\\kronos-venv\\Scripts\\python tools/research_kronos.py run --kronos-dir C:\\Users\\Administrator\\Kronos

دو سؤال:
  A) جهت خام: در نقاطی با فاصلهٔ مساوی از --since به بعد (دادهٔ ۲۰۲۶ که مدل در آموزش ندیده)، Kronos جهت قیمت
     --pred-len کندل بعد را چند درصد درست گفت؟ و اگر طبق آن معامله می‌شد (ورود روی بسته‌شدن کندل، خروج
     بعد از --pred-len کندل، منهای اسپرد همان کندل) میانگین هر معامله چند دلار بود؟ مقایسه با «مومنتوم ساده».
  B) فیلتر هشدارهای اسکات: برای هر هشدار data/scout_signal_audit.csv (همان کارنامه)، Kronos موافق جهت هشدار بود
     یا مخالف؟ درصد «اول ‎+1R» برای موافق‌ها و مخالف‌ها جدا، با بازهٔ اطمینان، آزمون اختلاف و پایداری در دو نیمه.

مراقبت از سرور: پایتونِ run با اولویت «کمتر از عادی» و --threads رشته (پیش‌فرض ۲) اجرا می‌شود؛ نتیجه‌ها
تکه‌تکه ذخیره می‌شوند، پس با Ctrl+C قطع و بعد با همان دستور ادامه داده می‌شود.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

OUT = Path("_kronos_out")
BARS_CSV = OUT / "xau_m15.csv"
SYMBOLS = ["XAUUSD@", "XAUUSD", "GOLD", "XAUUSDm", "XAUUSD.a", "XAUUSD.pro"]
COLS = ["open", "high", "low", "close", "volume"]


# ───────────────────────────── export (پایتون ربات‌ها) ─────────────────────────────
def export_bars(mt5, bars: int, symbol: Optional[str], tf: str = "M15") -> pd.DataFrame:
    """کندل‌های MT5 با حجم تیکی و اسپرد. اگر ترمینال این تعداد را نداد، نصف می‌خواهد (سقف max-bars)."""
    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        sym = None
        for cand in ([symbol] if symbol else []) + SYMBOLS:
            if mt5.symbol_info(cand) is not None and mt5.symbol_select(cand, True):
                sym = cand
                break
        if not sym:
            raise SystemExit("نماد طلا پیدا نشد؛ با --symbol بدهید")
        code = getattr(mt5, f"TIMEFRAME_{tf}")
        raw, n = None, int(bars)
        while n >= 500:
            raw = mt5.copy_rates_from_pos(sym, code, 0, n)
            if raw is not None and len(raw) > 0:
                break
            n //= 2
    finally:
        mt5.shutdown()
    if raw is None or len(raw) == 0:
        raise SystemExit(f"MT5 برای {sym} {tf} کندلی نداد: {mt5.last_error()}")
    d = pd.DataFrame(raw)
    d["time"] = pd.to_datetime(d["time"], unit="s")
    d = d.rename(columns={"tick_volume": "volume"})
    d = d.drop_duplicates("time", keep="last").sort_values("time")
    print(f"{tf}: {sym}، {len(d)} کندل از {d.time.iloc[0]} تا {d.time.iloc[-1]}", flush=True)
    return d[["time"] + COLS + ["spread"]].reset_index(drop=True)


def load_bars(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["time"] = pd.to_datetime(d["time"])
    d = d.sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)
    for c in COLS + ["spread"]:
        d[c] = pd.to_numeric(d[c], errors="coerce").astype("float64")
    return d.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


# ───────────────────────────── ساختن ورودی مدل (بدون نگاه به آینده) ─────────────────────────────
def context(bars: pd.DataFrame, i: int, lookback: int, pred_len: int, step_min: int = 15
            ) -> Tuple[pd.DataFrame, pd.Series, pd.Series]:
    """ورودی برای پیش‌بینی بعد از بسته شدن کندل i: فقط کندل‌های i-lookback+1 … i.
    زمان‌های آینده فقط ساعت/روز هستند (Kronos ویژگی زمانی می‌خواهد) و هیچ قیمتی از آینده ندارند؛
    اگر کندل آینده موجود نباشد، با فاصلهٔ step_min ساخته می‌شوند."""
    if i < lookback - 1:
        raise ValueError("not enough history")
    x = bars.iloc[i - lookback + 1:i + 1]
    x_df = x[COLS].reset_index(drop=True)
    x_ts = pd.Series(pd.to_datetime(x["time"]).values)
    fut = list(pd.to_datetime(bars["time"].iloc[i + 1:i + 1 + pred_len]))
    last = pd.Timestamp(x["time"].iloc[-1])
    while len(fut) < pred_len:
        fut.append((fut[-1] if fut else last) + pd.Timedelta(minutes=step_min))
    return x_df, x_ts, pd.Series(pd.to_datetime(fut))


# ───────────────────────────── آمار ─────────────────────────────
def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n <= 0:
        return float("nan"), float("nan")
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return c - h, c + h


def two_prop_p(k1: int, n1: int, k2: int, n2: int) -> float:
    """آزمون دوطرفهٔ اختلاف دو نسبت (تقریب نرمال)."""
    if min(n1, n2) == 0:
        return float("nan")
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = (k1 / n1 - k2 / n2) / se
    return math.erfc(abs(z) / math.sqrt(2))


def boot_mean_ci(x: Sequence[float], iters: int = 2000, seed: int = 7) -> Tuple[float, float]:
    a = np.asarray([v for v in x if v == v], dtype=float)
    if len(a) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    m = a[rng.integers(0, len(a), size=(iters, len(a)))].mean(axis=1)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def direction_stats(rows: List[dict]) -> Dict[str, object]:
    """rows: pred_move, act_move, pnl, mom_move (همه دلار). برد = علامت پیش‌بینی = علامت حرکت واقعی."""
    r = [x for x in rows if x["pred_move"] and x["act_move"]]
    n = len(r)
    hit = sum(1 for x in r if (x["pred_move"] > 0) == (x["act_move"] > 0))
    mom = [x for x in r if x.get("mom_move")]
    mh = sum(1 for x in mom if (x["mom_move"] > 0) == (x["act_move"] > 0))
    pnl = [x["pnl"] for x in r]
    lo, hi = wilson(hit, n)
    plo, phi = boot_mean_ci(pnl)
    out = {"n": n, "hit": hit / n if n else float("nan"), "ci": (lo, hi),
           "pnl_avg": float(np.mean(pnl)) if pnl else float("nan"), "pnl_ci": (plo, phi),
           "momentum_hit": mh / len(mom) if mom else float("nan"), "up_share": (
               sum(1 for x in r if x["act_move"] > 0) / n if n else float("nan"))}
    # قوی‌ترین یک‌سوم پیش‌بینی‌ها (بزرگ‌ترین حرکت پیش‌بینی‌شده)
    if n >= 9:
        cut = np.quantile([abs(x["pred_move"]) for x in r], 2 / 3)
        top = [x for x in r if abs(x["pred_move"]) >= cut]
        th = sum(1 for x in top if (x["pred_move"] > 0) == (x["act_move"] > 0))
        out["strong"] = {"n": len(top), "hit": th / len(top), "ci": wilson(th, len(top)),
                         "pnl_avg": float(np.mean([x["pnl"] for x in top]))}
    return out


def filter_stats(rows: List[dict]) -> Dict[str, object]:
    """rows: agree (bool)، win (True=اول ‎+1R، False=اول ‎-1R)، half (0/1)، strength (حرکت پیش‌بینی/فاصلهٔ حد ضرر)."""
    r = [x for x in rows if x.get("win") is not None]

    def grp(sel):
        g = [x for x in r if sel(x)]
        k = sum(1 for x in g if x["win"])
        return {"n": len(g), "k": k, "win": k / len(g) if g else float("nan"), "ci": wilson(k, len(g))}
    a, d = grp(lambda x: x["agree"]), grp(lambda x: not x["agree"])
    out = {"all": grp(lambda x: True), "agree": a, "disagree": d,
           "p_diff": two_prop_p(a["k"], a["n"], d["k"], d["n"]),
           "halves": [{"agree": grp(lambda x, h=h: x["agree"] and x["half"] == h),
                       "disagree": grp(lambda x, h=h: (not x["agree"]) and x["half"] == h)} for h in (0, 1)]}
    st = [x for x in r if x["agree"] and x.get("strength") is not None]
    if len(st) >= 9:
        cut = np.quantile([x["strength"] for x in st], 2 / 3)
        out["agree_strong"] = grp(lambda x: x["agree"] and (x.get("strength") or 0) >= cut)
    return out


def verdict_direction(s: Dict[str, object]) -> str:
    ok = s["n"] >= 200 and s["ci"][0] > 0.5 and s["pnl_ci"][0] > 0
    return "PASS" if ok else "FAIL"


def verdict_filter(s: Dict[str, object]) -> str:
    a, h = s["agree"], s["halves"]
    ok = (a["n"] >= 100 and a["ci"][0] > 0.5 and s["p_diff"] == s["p_diff"] and s["p_diff"] < 0.05
          and all(x["agree"]["win"] > x["disagree"]["win"] for x in h if x["agree"]["n"] and x["disagree"]["n"]))
    return "PASS" if ok else "FAIL"


# ───────────────────────────── نقاط و هشدارها ─────────────────────────────
def raw_points(bars: pd.DataFrame, since: str, n_points: int, lookback: int, pred_len: int) -> List[int]:
    """نقاط با فاصلهٔ مساوی از since به بعد؛ هر نقطه تاریخچهٔ کافی و آیندهٔ کامل دارد."""
    t = pd.to_datetime(bars["time"])
    lo = max(int(np.searchsorted(t.values, np.datetime64(pd.Timestamp(since)))), lookback - 1, 4)
    hi = len(bars) - pred_len - 1
    if hi <= lo:
        return []
    return sorted(set(int(x) for x in np.linspace(lo, hi, num=min(n_points, hi - lo + 1))))


def align_alerts(audit: pd.DataFrame, bars: pd.DataFrame, lookback: int) -> List[dict]:
    """هر هشدار کارنامه → اندیس کندل هشدار (ستون bar = زمان باز شدن کندل M15 به وقت سرور)."""
    idx = {pd.Timestamp(v): i for i, v in enumerate(pd.to_datetime(bars["time"]))}
    out = []
    for _, a in audit.iterrows():
        if str(a.get("status", "ok")) != "ok" or str(a.get("first")) not in ("+1R", "-1R"):
            continue
        i = idx.get(pd.Timestamp(a["bar"]))
        if i is None or i < lookback - 1:
            continue
        sl_dist = abs(float(a["price"]) - float(a["sl"]))
        out.append({"key": f"{a['ts']}|{a['bar']}|{a['side']}", "i": i, "side": str(a["side"]).upper(),
                    "win": str(a["first"]) == "+1R", "ts": str(a["ts"]), "sl_dist": sl_dist,
                    "setup": str(a.get("setup", ""))})
    return out


# ───────────────────────────── اجرای مدل (تکه‌تکه، قابل ادامه) ─────────────────────────────
PredictFn = Callable[[List[Tuple[pd.DataFrame, pd.Series, pd.Series]]], List[np.ndarray]]


def done_keys(path: Path) -> Dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as fh:
        return {r["key"]: r for r in csv.DictReader(fh)}


def append_rows(path: Path, rows: List[dict]) -> None:
    if not rows:
        return
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        if new:
            w.writeheader()
        w.writerows(rows)


def run_jobs(jobs: List[dict], bars: pd.DataFrame, predict: PredictFn, lookback: int, pred_len: int,
             path: Path, row_fn: Callable[[dict, float], dict], batch: int = 8, label: str = "") -> None:
    """jobs: هر کدام key و i. پیش‌بینی‌های قبلاً ذخیره‌شده دوباره حساب نمی‌شوند."""
    have = done_keys(path)
    todo = [j for j in jobs if j["key"] not in have]
    t0 = time.time()
    for s in range(0, len(todo), batch):
        part = todo[s:s + batch]
        preds = predict([context(bars, j["i"], lookback, pred_len) for j in part])
        rows = []
        for j, p in zip(part, preds):
            last_close = float(bars["close"].iloc[j["i"]])
            rows.append(row_fn(j, float(p[-1]) - last_close))
        append_rows(path, rows)
        done = s + len(part)
        el = time.time() - t0
        eta = el / done * (len(todo) - done)
        print(f"  {label} {done}/{len(todo)}  ({el / 60:.1f} دقیقه، حدود {eta / 60:.0f} دقیقهٔ دیگر)", flush=True)


def make_predictor(kronos_dir: str, model_id: str, tok_id: str, threads: int, samples: int,
                   seed: int, pred_len: int, max_context: int = 512) -> PredictFn:
    sys.path.insert(0, str(Path(kronos_dir).resolve()))
    import torch                                                  # فقط در venv جدای Kronos
    from model import Kronos, KronosPredictor, KronosTokenizer    # type: ignore
    torch.set_num_threads(max(1, int(threads)))
    torch.manual_seed(seed)
    np.random.seed(seed)
    tok = KronosTokenizer.from_pretrained(tok_id)
    mdl = Kronos.from_pretrained(model_id)
    mdl.eval()
    pr = KronosPredictor(mdl, tok, device="cpu", max_context=max_context)

    def predict(items):
        out = pr.predict_batch([x for x, _, _ in items], [a for _, a, _ in items], [b for _, _, b in items],
                               pred_len=pred_len, T=1.0, top_p=0.9, sample_count=samples, verbose=False)
        return [np.asarray(o["close"].values, dtype=float) for o in out]
    return predict


def lower_priority() -> None:
    """پایتون تحقیق نباید با ربات‌ها سر پردازنده رقابت کند."""
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32                            # type: ignore[attr-defined]
            k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)
    except Exception:
        pass


# ───────────────────────────── گزارش ─────────────────────────────
def pct(x: float) -> str:
    return "—" if x != x else f"{100 * x:.1f}%"


def report(dirs: Optional[dict], filt: Optional[dict], meta: dict) -> str:
    L = [f"KRONOS TEST  model={meta['model']}  lookback={meta['lookback']}  horizon={meta['pred_len']}x M15  "
         f"samples={meta['samples']}  since={meta['since']}", ""]
    if dirs:
        L += ["== A) RAW DIRECTION (Kronos alone) ==",
              f"  n={dirs['n']}  hit {pct(dirs['hit'])}  CI [{pct(dirs['ci'][0])}, {pct(dirs['ci'][1])}]  "
              f"(simple momentum {pct(dirs['momentum_hit'])}, market went up {pct(dirs['up_share'])})",
              f"  trade at close, exit after horizon, minus spread: avg ${dirs['pnl_avg']:+.3f} per 0.01 lot  "
              f"CI [{dirs['pnl_ci'][0]:+.3f}, {dirs['pnl_ci'][1]:+.3f}]"]
        if "strong" in dirs:
            s = dirs["strong"]
            L.append(f"  strongest third: n={s['n']} hit {pct(s['hit'])} CI [{pct(s['ci'][0])}, {pct(s['ci'][1])}]  "
                     f"avg ${s['pnl_avg']:+.3f}")
        L += [f"  VERDICT: {verdict_direction(dirs)}  (PASS = n>=200, CI low > 50%, profit CI low > 0)", ""]
    if filt:
        g = lambda x: f"n={x['n']:4d}  first +1R {pct(x['win'])}  CI [{pct(x['ci'][0])}, {pct(x['ci'][1])}]"  # noqa: E731
        L += ["== B) SCOUT ALERTS FILTERED BY KRONOS ==",
              f"  all alerts      {g(filt['all'])}",
              f"  Kronos agrees   {g(filt['agree'])}",
              f"  Kronos disagrees{g(filt['disagree'])}",
              f"  difference p-value {filt['p_diff']:.3f}"]
        if "agree_strong" in filt:
            L.append(f"  strong agree    {g(filt['agree_strong'])}")
        for h, x in enumerate(filt["halves"]):
            L.append(f"  half {h + 1}: agree {pct(x['agree']['win'])} (n={x['agree']['n']})  "
                     f"disagree {pct(x['disagree']['win'])} (n={x['disagree']['n']})")
        L += [f"  VERDICT: {verdict_filter(filt)}  (PASS = agree n>=100, CI low > 50%, p<0.05, better in both halves)", ""]
    L.append("Research only. Past results; spread on the alert test is not deducted (direction test).")
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Honest Kronos test on gold (research only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="با پایتون ربات‌ها: کندل‌های M15 از MT5 به CSV")
    e.add_argument("--bars", type=int, default=30000)
    e.add_argument("--symbol", default=None)
    e.add_argument("--out", default=str(BARS_CSV))
    r = sub.add_parser("run", help="با پایتون venv جدای Kronos")
    r.add_argument("--kronos-dir", required=True, help="پوشهٔ clone شدهٔ github.com/shiyu-coder/Kronos")
    r.add_argument("--bars-csv", default=str(BARS_CSV))
    r.add_argument("--audit-csv", default="data/scout_signal_audit.csv")
    r.add_argument("--model", default="NeoQuasar/Kronos-small")
    r.add_argument("--tokenizer", default="NeoQuasar/Kronos-Tokenizer-base")
    r.add_argument("--lookback", type=int, default=400)
    r.add_argument("--pred-len", type=int, default=4, help="تعداد کندل M15 آینده (۴ = یک ساعت)")
    r.add_argument("--samples", type=int, default=5)
    r.add_argument("--points", type=int, default=600, help="تعداد نقاط آزمون جهت خام")
    r.add_argument("--since", default="2026-01-01", help="فقط داده‌ای که مدل در آموزش ندیده")
    r.add_argument("--threads", type=int, default=2)
    r.add_argument("--batch", type=int, default=8)
    r.add_argument("--point", type=float, default=0.01, help="ارزش یک پوینت اسپرد به دلار در 0.01 لات")
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--skip-raw", action="store_true")
    r.add_argument("--skip-alerts", action="store_true")
    r.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)

    if a.cmd == "export":
        import MetaTrader5 as mt5                                   # فقط پایتون ربات‌ها
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        export_bars(mt5, a.bars, a.symbol).to_csv(a.out, index=False)
        print(f"saved {a.out}")
        return

    lower_priority()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    bars = load_bars(Path(a.bars_csv))
    print(f"bars: {len(bars)} از {bars.time.iloc[0]} تا {bars.time.iloc[-1]}", flush=True)
    predict = make_predictor(a.kronos_dir, a.model, a.tokenizer, a.threads, a.samples, a.seed, a.pred_len)
    tag = f"{a.model.split('/')[-1]}_L{a.lookback}_H{a.pred_len}_S{a.samples}"
    meta = {"model": a.model, "lookback": a.lookback, "pred_len": a.pred_len, "samples": a.samples, "since": a.since}
    close, spread = bars["close"].values, bars["spread"].fillna(0).values

    dirs = None
    if not a.skip_raw:
        pts = raw_points(bars, a.since, a.points, a.lookback, a.pred_len)
        print(f"A) {len(pts)} نقطه", flush=True)
        jobs = [{"key": str(bars.time.iloc[i]), "i": i} for i in pts]
        path = out / f"raw_{tag}.csv"

        def raw_row(j, pm):
            i = j["i"]
            act = float(close[i + a.pred_len] - close[i])
            d = 1 if pm > 0 else -1 if pm < 0 else 0
            return {"key": j["key"], "i": i, "pred_move": round(pm, 4), "act_move": round(act, 4),
                    "mom_move": round(float(close[i] - close[i - a.pred_len]), 4),
                    "pnl": round(d * act - (abs(d) * spread[i] * a.point), 4)}
        run_jobs(jobs, bars, predict, a.lookback, a.pred_len, path, raw_row, a.batch, "A")
        rows = [{k: float(v) for k, v in r.items() if k != "key"} for r in done_keys(path).values()]
        dirs = direction_stats(rows)

    filt = None
    if not a.skip_alerts and Path(a.audit_csv).exists():
        audit = pd.read_csv(a.audit_csv, encoding="utf-8-sig")
        al = align_alerts(audit, bars, a.lookback)
        print(f"B) {len(al)} هشدار با کندل در CSV", flush=True)
        path = out / f"alerts_{tag}.csv"

        def al_row(j, pm):
            sgn = 1 if j["side"] == "BUY" else -1
            return {"key": j["key"], "ts": j["ts"], "side": j["side"], "setup": j["setup"], "win": int(j["win"]),
                    "pred_move": round(pm, 4), "agree": int(pm * sgn > 0),
                    "strength": round(pm * sgn / j["sl_dist"], 4) if j["sl_dist"] else ""}
        run_jobs(al, bars, predict, a.lookback, a.pred_len, path, al_row, a.batch, "B")
        rs = sorted(done_keys(path).values(), key=lambda r: r["ts"])
        mid = rs[len(rs) // 2]["ts"] if rs else ""
        filt = filter_stats([{"agree": r["agree"] == "1", "win": r["win"] == "1", "half": int(r["ts"] >= mid),
                              "strength": float(r["strength"]) if r["strength"] not in ("", None) else None}
                             for r in rs])
    elif not a.skip_alerts:
        print(f"B) {a.audit_csv} نیست؛ اول py -3.11 tools/scout_signal_audit.py را بزنید", flush=True)

    txt = report(dirs, filt, meta)
    (out / f"summary_{tag}.txt").write_text(txt, encoding="utf-8")
    (out / f"summary_{tag}.json").write_text(json.dumps({"meta": meta, "direction": dirs, "filter": filt},
                                                        ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("\n" + txt)
    print(f"\nsaved {out / f'summary_{tag}.txt'}")


if __name__ == "__main__":
    main()
