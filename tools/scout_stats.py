"""آمار ژورنال اسکات — انتخاب‌های شما در برابر همهٔ ستاپ‌ها.

برای هر هشدار (و هر ستاپی که هنگام باز بودن معامله بی‌صدا ثبت شده) حساب می‌کند اگر با همان
قیمت، حد ضرر و هدف ۲R گرفته می‌شد چه می‌شد (نتیجهٔ فرضی، روی کندل‌های M1 همان بروکر، با کسر
اسپرد). برای معاملاتی که واقعاً باز شده‌اند سود واقعی را از تاریخچهٔ MT5 می‌خواند.

اجرا روی سرور (فقط خواندن؛ هیچ سفارشی نمی‌دهد):
    py -3.11 tools/scout_stats.py
    py -3.11 tools/scout_stats.py --hours 24 --no-mt5        # بدون MT5: فقط شمارش
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ژورنال قدیمی: هر رویداد ستون‌های خودش را به ترتیب کد می‌نوشت (بدون سرستون درست)
LEGACY = {
    ("alert", 13): ["alert", "setup", "n_setups", "side", "bar", "price", "sl", "tp", "atr", "spread", "risk_usd", "ts"],
    ("alert", 12): ["alert", "setup", "side", "bar", "price", "sl", "tp", "atr", "spread", "risk_usd", "ts"],
    ("expired", 5): ["alert", "setup", "side", "ts"],
    ("rejected", 5): ["alert", "setup", "side", "ts"],
    ("approved_dry", 5): ["alert", "setup", "side", "ts"],
    ("blocked_hedge", 6): ["alert", "setup", "side", "note", "ts"],
    ("order_failed", 6): ["alert", "setup", "side", "note", "ts"],
    ("opened", 8): ["alert", "setup", "side", "ticket", "price", "sl", "ts"],
    ("closed", 7): ["ticket", "setup", "side", "price", "ok", "ts"],
    ("gone", 4): ["ticket", "setup", "ts"],
    ("reached_1R", 5): ["ticket", "setup", "profit", "ts"],
    ("structure_break", 5): ["ticket", "setup", "profit", "ts"],
}
DECISIONS = {"expired", "rejected", "approved_dry", "blocked_hedge", "order_failed",
             "invalid", "cancelled", "opened"}
LABEL = {"opened": "تأیید و باز شد", "approved_dry": "تأیید (آزمایشی)", "rejected": "رد کردی",
         "expired": "منقضی شد", "suppressed": "بی‌صدا (معامله باز بود)", "cancelled": "لغو شد",
         "invalid": "باطل (قیمت از حد ضرر رد شد)", "blocked_hedge": "بلاک (پوزیشن مخالف)",
         "order_failed": "خطای سفارش", "pending": "بی‌جواب مانده"}


def read_events(v2: Optional[Path], legacy: Optional[Path]) -> List[Dict[str, Any]]:
    ev: List[Dict[str, Any]] = []
    if legacy and legacy.exists():
        with legacy.open(encoding="utf-8", newline="") as fh:
            for row in csv.reader(fh):
                if not row or row[0] == "event":
                    continue
                names = LEGACY.get((row[0], len(row)))
                if names:
                    d = dict(zip(names, row[1:])); d["event"] = row[0]; d["src"] = "legacy"; ev.append(d)
    if v2 and v2.exists():
        with v2.open(encoding="utf-8", newline="") as fh:
            for d in csv.DictReader(fh):
                d["src"] = "v2"; ev.append(d)
    ev.sort(key=lambda d: str(d.get("ts", "")))
    return ev


def _f(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def build(ev: List[Dict[str, Any]]) -> pd.DataFrame:
    recs: List[Dict[str, Any]] = []
    latest: Dict[str, Dict[str, Any]] = {}          # شناسهٔ هشدار بعد از هر ری‌استارت از ۱ شروع می‌شود
    by_ticket: Dict[str, Dict[str, Any]] = {}
    for d in ev:
        e = d.get("event")
        if e in ("alert", "suppressed"):
            r = dict(ts=d.get("ts"), setup=d.get("setup"), n_setups=_f(d.get("n_setups") or 1),
                     side=d.get("side"), bar=d.get("bar"), price=_f(d.get("price")), sl=_f(d.get("sl")),
                     tp=_f(d.get("tp")), spread=_f(d.get("spread")), status="suppressed" if e == "suppressed" else "pending",
                     ticket=None, actual=np.nan, src=d.get("src"))
            recs.append(r)
            if e == "alert":
                latest[str(d.get("alert"))] = r
        elif e in DECISIONS:
            r = latest.get(str(d.get("alert")))
            if r is not None and r["status"] == "pending":
                r["status"] = e
                if e == "opened":
                    r["ticket"] = str(d.get("ticket")); by_ticket[r["ticket"]] = r
        elif e == "gone":
            r = by_ticket.get(str(d.get("ticket")))
            if r is not None and not np.isnan(_f(d.get("profit"))):
                r["actual"] = _f(d.get("profit"))
    return pd.DataFrame(recs)


def virtual(df: pd.DataFrame, m1: Optional[pd.DataFrame], hours: float) -> pd.DataFrame:
    """نتیجهٔ فرضی: ورود همان قیمت هشدار، حد ضرر هشدار، هدف ۲R؛ اولین برخورد روی M1."""
    df = df.copy(); df["R"] = np.nan; df["exit"] = ""
    if m1 is None or m1.empty or df.empty:
        return df
    H, L, C, idx = m1["high"].to_numpy(float), m1["low"].to_numpy(float), m1["close"].to_numpy(float), m1.index
    for i, r in df.iterrows():
        try:
            start = pd.Timestamp(r.bar) + pd.Timedelta(minutes=15)
        except Exception:
            continue
        dist = abs(r.price - r.sl)
        if not dist or np.isnan(dist):
            continue
        a = idx.searchsorted(start); b = idx.searchsorted(start + pd.Timedelta(hours=hours))
        if a >= len(idx) or b <= a:
            continue
        up = r.side == "BUY"; res = None
        for k in range(a, b):
            hit_sl = L[k] <= r.sl if up else H[k] >= r.sl
            hit_tp = H[k] >= r.tp if up else L[k] <= r.tp
            if hit_sl:                                   # هر دو در یک کندل = محافظه‌کارانه، حد ضرر
                res = ("SL", -1.0); break
            if hit_tp:
                res = ("TP", abs(r.tp - r.price) / dist); break
        if res is None:
            last = C[b - 1]
            res = ("باز", ((last - r.price) if up else (r.price - last)) / dist)
        cost = (r.spread / 100.0) / dist if not np.isnan(r.spread) else 0.0
        df.at[i, "R"] = res[1] - cost; df.at[i, "exit"] = res[0]
    return df


def attach_actual(df: pd.DataFrame, deals: Optional[Callable[[int], List[Any]]]) -> pd.DataFrame:
    if deals is None:
        return df
    for i, r in df[df.status == "opened"].iterrows():
        if not np.isnan(r.actual) or not r.ticket:
            continue
        try:
            ds = deals(int(float(r.ticket)))
        except Exception:
            continue
        if any(int(getattr(d, "entry", 0)) in (1, 3) for d in ds):
            df.at[i, "actual"] = round(sum(float(getattr(d, a, 0.0) or 0.0) for d in ds
                                           for a in ("profit", "commission", "swap", "fee")), 2)
    return df


def report(df: pd.DataFrame) -> str:
    out = []
    n = len(df)
    out.append(f"ستاپ‌های ثبت‌شده: {n}  (از {df.ts.min()} تا {df.ts.max()})" if n else "ژورنال خالی است.")
    if not n:
        return "\n".join(out)
    out.append("\n— وضعیت —")
    for k, v in df.status.value_counts().items():
        out.append(f"  {LABEL.get(k, k):28} {v}")
    have = df.R.notna()
    if have.any():
        def line(lbl, x):
            x = x[x.R.notna()]
            if not len(x):
                return f"  {lbl:28} —"
            return (f"  {lbl:28} n={len(x):4}  برد {(x.R > 0).mean()*100:5.1f}%  "
                    f"میانگین {x.R.mean():+.2f}R  جمع {x.R.sum():+.1f}R")
        out.append("\n— نتیجهٔ فرضی (همان ورود، همان حد ضرر، هدف ۲R، اسپرد کسر شده) —")
        took = df[df.status.isin(["opened", "approved_dry"])]
        left = df[df.status.isin(["rejected", "expired", "cancelled", "pending"])]
        out.append(line("ستاپ‌هایی که تأیید کردی", took))
        out.append(line("ستاپ‌هایی که رد/منقضی شد", left))
        out.append(line("بی‌صدا (معامله باز بود)", df[df.status == "suppressed"]))
        out.append(line("همهٔ ستاپ‌ها", df))
        out.append("\n— به تفکیک جهت (همهٔ ستاپ‌ها) —")
        for sd, g in df.groupby("side"):
            out.append(line(sd, g))
        out.append("\n— به تفکیک ستاپ (دست‌کم ۵ نمونه) —")
        first = df.assign(s1=df.setup.astype(str).str.split(r"[,+ ]").str[0])
        for st, g in sorted(first.groupby("s1"), key=lambda kv: -kv[1].R.mean() if kv[1].R.notna().any() else 0):
            if g.R.notna().sum() >= 5:
                out.append(line(st, g))
    act = df[(df.status == "opened")]
    if len(act):
        a = act[act.actual.notna()]
        out.append("\n— معاملات واقعی (سود از تاریخچهٔ MT5) —")
        out.append(f"  باز شده: {len(act)}  | نتیجه معلوم: {len(a)}  | جمع ${a.actual.sum():+.2f}  | "
                   f"برد {(a.actual > 0).mean()*100 if len(a) else 0:.0f}%")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/settings_scout.yaml")
    ap.add_argument("--journal", default="data/scout_journal_v2.csv")
    ap.add_argument("--legacy", default="data/scout_journal.csv")
    ap.add_argument("--hours", type=float, default=24.0, help="حداکثر زمان نگه داشتن فرضی")
    ap.add_argument("--no-mt5", action="store_true")
    ap.add_argument("--out", default="data/scout_stats.csv")
    a = ap.parse_args()
    df = build(read_events(Path(a.journal), Path(a.legacy)))
    m1 = deals = None
    if not a.no_mt5 and len(df):
        from core import Settings
        from core.mt5_client import MT5Client, MT5Config
        cli = MT5Client(MT5Config.from_settings(Settings.load(a.config)))
        if cli.connect():
            m1 = cli.get_rates("M1", 60000)
            deals = cli.deals_for_position
        else:
            print("⚠️ اتصال MT5 برقرار نشد؛ فقط شمارش انجام می‌شود.")
    df = attach_actual(virtual(df, m1, a.hours), deals)
    if len(df):
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(a.out, index=False, encoding="utf-8")
    print(report(df))
    if len(df):
        print(f"\nجزئیات هر ستاپ: {a.out}")


if __name__ == "__main__":
    main()
