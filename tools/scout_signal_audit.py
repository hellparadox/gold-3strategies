"""بررسی سیگنال‌های اسکات: بعد از هر هشدار، بازار در جهت سیگنال رفت یا خلاف آن؟

فقط خواندن — هیچ سفارشی نمی‌دهد و به اسکات دست نمی‌زند. برای هر هشدار ژورنال (از بسته شدن کندل
هشدار، روی کندل‌های M1 همان بروکر):
- حرکت در جهت سیگنال بعد از ۱۵، ۳۰ و ۶۰ دقیقه (دلار و R؛ R = فاصلهٔ حد ضرر هشدار)
- بیشترین حرکت به نفع (MFE) و به ضرر (MAE) در ۶۰ دقیقهٔ اول
- کدام زودتر رسید: ‎+1R یا ‎-1R (تا ۴ ساعت؛ هر دو در یک کندل = «هر دو»)
خلاصه به تفکیک جهت، ستاپ، ساعت تهران و تصمیم شما؛ جزئیات هر هشدار در CSV.

اجرا روی سرور (کنار اسکات؛ اسکات لازم نیست خاموش شود):
    py -3.11 tools/scout_signal_audit.py --since 2026-10-05
    py -3.11 tools/scout_signal_audit.py --since 2026-10-05 --until 2026-10-10
قیمت‌ها bid هستند (کندل‌های MT5)؛ اسپرد کسر نمی‌شود — این بررسی جهت حرکت است، نه سود.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DECISIONS = {"opened", "rejected", "expired", "blocked_hedge", "blocked_limit", "invalid", "cancelled",
             "order_failed", "order_uncertain", "approved_dry"}
CHECKPOINTS = (15, 30, 60)
TEHRAN_BUCKETS = ((0, 8), (8, 12), (12, 16), (16, 20), (20, 24))
# برچسب هشدار: سه دسته که در ۱۴ روز اول بیشترین فرق را داشتند (فقط اطلاع؛ هیچ سیگنالی حذف نمی‌شود)
BREAKS = ("range_break", "cloud_break")
TIGHT_STOP = 6.0                       # فاصلهٔ قیمت تا حد ضرر هشدار؛ با لات ۰.۰۱ همان دلار است
LABEL_FA = {"break": "همراه شکست", "plain": "بدون شکست", "tight": "حد ضرر نزدیک"}


def _f(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def read_alerts(journal: Path, since: Optional[str] = None, until: Optional[str] = None) -> pd.DataFrame:
    """هشدارهای ژورنال با تصمیم شما (شمارهٔ هشدار بعد از هر ری‌استارت از ۱ شروع می‌شود؛ تصمیم به
    آخرین هشدارِ همان شماره وصل می‌شود)."""
    recs: List[Dict[str, Any]] = []
    latest: Dict[str, Dict[str, Any]] = {}
    with Path(journal).open(encoding="utf-8-sig", newline="") as fh:
        for d in csv.DictReader(fh):
            e = d.get("event")
            if e == "alert":
                r = {"ts": d.get("ts", ""), "setup": d.get("setup", ""), "side": d.get("side", ""),
                     "bar": d.get("bar", ""), "price": _f(d.get("price")), "sl": _f(d.get("sl")),
                     "decision": "pending"}
                recs.append(r)
                latest[str(d.get("alert"))] = r
            elif e in DECISIONS:
                r = latest.get(str(d.get("alert")))
                if r is not None and r["decision"] == "pending":
                    if e == "opened" and "contra=1" in str(d.get("note") or ""):
                        r["decision"] = "contra"          # خلاف جهت سیگنال گرفته شد
                    else:
                        r["decision"] = "taken" if e in ("opened", "approved_dry") else (
                            "blocked" if e.startswith("blocked") else e)
    df = pd.DataFrame(recs, columns=["ts", "setup", "side", "bar", "price", "sl", "decision"])
    if since:
        df = df[df.ts >= since]
    if until:
        df = df[df.ts < until]
    return df.reset_index(drop=True)


def audit(alerts: pd.DataFrame, m1: Optional[pd.DataFrame], horizon: int = 60,
          race_minutes: int = 240, bar_minutes: int = 15) -> pd.DataFrame:
    """برای هر هشدار: حرکت در جهت سیگنال در ۱۵/۳۰/۶۰ دقیقه، MFE/MAE، و اول ‎+1R یا ‎-1R."""
    df = alerts.copy()
    cols = [f"move{m}" for m in CHECKPOINTS] + [f"r{m}" for m in CHECKPOINTS] + ["mfe", "mae", "mfe_r", "mae_r"]
    for c in cols:
        df[c] = np.nan
    df["first"] = ""
    df["status"] = "no_data"
    if m1 is None or m1.empty or df.empty:
        return df
    idx = pd.DatetimeIndex(m1.index)
    H, L, C = (m1[k].to_numpy(float) for k in ("high", "low", "close"))
    for i, r in df.iterrows():
        try:
            start = pd.Timestamp(r.bar) + pd.Timedelta(minutes=bar_minutes)    # بسته شدن کندل هشدار
        except (TypeError, ValueError):
            continue
        risk = abs(r.price - r.sl)
        if not risk or np.isnan(risk):
            continue
        buy = str(r.side).upper() == "BUY"
        a = idx.searchsorted(start)
        b = idx.searchsorted(start + pd.Timedelta(minutes=horizon))
        if a >= len(idx) or b <= a:
            continue
        sign = 1.0 if buy else -1.0
        complete = idx[-1] >= start + pd.Timedelta(minutes=horizon - 1)
        for m in CHECKPOINTS:
            k = idx.searchsorted(start + pd.Timedelta(minutes=m)) - 1      # آخرین کندل قبل از +m دقیقه
            if k >= a and idx[-1] >= start + pd.Timedelta(minutes=m - 1):
                mv = sign * (C[k] - r.price)
                df.at[i, f"move{m}"] = round(mv, 2)
                df.at[i, f"r{m}"] = round(mv / risk, 2)
        hi, lo = H[a:b].max(), L[a:b].min()
        mfe = (hi - r.price) if buy else (r.price - lo)
        mae = (r.price - lo) if buy else (hi - r.price)
        df.at[i, "mfe"], df.at[i, "mae"] = round(mfe, 2), round(mae, 2)
        df.at[i, "mfe_r"], df.at[i, "mae_r"] = round(mfe / risk, 2), round(mae / risk, 2)
        up_lvl = r.price + sign * risk
        dn_lvl = r.price - sign * risk
        e = idx.searchsorted(start + pd.Timedelta(minutes=race_minutes))
        first = "none"
        for k in range(a, min(e, len(idx))):
            good = H[k] >= up_lvl if buy else L[k] <= up_lvl
            bad = L[k] <= dn_lvl if buy else H[k] >= dn_lvl
            if good and bad:
                first = "both"
                break
            if good:
                first = "+1R"
                break
            if bad:
                first = "-1R"
                break
        df.at[i, "first"] = first
        df.at[i, "status"] = "ok" if complete else "partial"
    return df


def server_to_tehran_minutes(bar: Any) -> int:
    """ساعت تهران منهای ساعت سرور بروکر، برای همان لحظه.
    فرض: ساعت سرور = نیویورک + ۷ (رایج‌ترین قرار بروکرهای طلا: تابستان UTC+3، زمستان UTC+2)؛
    تهران همیشه UTC+3:30 (ساعت تابستانی ندارد). تابستان ۳۰ دقیقه، زمستان ۹۰ دقیقه."""
    try:
        from tools import scout_control as ctl
    except Exception:                      # pragma: no cover
        import scout_control as ctl
    t = pd.Timestamp(bar).to_pydatetime()
    guess_utc = t.replace(tzinfo=timezone.utc) - timedelta(hours=3)
    server = ctl.new_york_offset_hours(guess_utc) + 7
    return 210 - server * 60


def tehran_hour(bar: Any, offset_min: Optional[int] = None, bar_minutes: int = 15) -> Optional[int]:
    """ساعت تهرانِ بسته شدن کندل؛ offset_min=None یعنی خودکار (server_to_tehran_minutes)."""
    try:
        off = server_to_tehran_minutes(bar) if offset_min is None else int(offset_min)
        t = pd.Timestamp(bar) + pd.Timedelta(minutes=bar_minutes + off)
    except (TypeError, ValueError):
        return None
    return int(t.hour)


def _line(label: str, g: pd.DataFrame) -> str:
    g = g[g.status != "no_data"]
    if not len(g):
        return f"  {label:24s} n=0"
    parts = [f"  {label:24s} n={len(g):3d}"]
    for m in CHECKPOINTS:
        x = g[f"r{m}"].dropna()
        parts.append(f"{m}m: {100 * (x > 0).mean():3.0f}% up {x.mean():+.2f}R" if len(x) else f"{m}m: -")
    parts.append(f"MFE {g.mfe_r.mean():.2f}R MAE {g.mae_r.mean():.2f}R")
    f = g["first"]
    decided = f.isin(["+1R", "-1R", "both"]).sum()
    parts.append(f"first +1R {(f == '+1R').sum()} / -1R {(f == '-1R').sum()} / both {(f == 'both').sum()}"
                 f" / none {(f == 'none').sum()}"
                 + (f" ({100 * (f == '+1R').sum() / decided:.0f}% +1R)" if decided else ""))
    return " | ".join(parts)


def group_stats(g: pd.DataFrame) -> Dict[str, Any]:
    """آمار یک گروه برای داشبورد و پیام هشدار (فقط عدد، JSON-پذیر)."""
    g = g[g.status != "no_data"]
    out: Dict[str, Any] = {"n": int(len(g))}
    if not len(g):
        return out
    for m in CHECKPOINTS:
        x = g[f"r{m}"].dropna()
        out[f"up{m}"] = round(float((x > 0).mean()), 3) if len(x) else None
        out[f"r{m}"] = round(float(x.mean()), 2) if len(x) else None
    out["mfe"] = round(float(g.mfe_r.mean()), 2)
    out["mae"] = round(float(g.mae_r.mean()), 2)
    f = g["first"]
    plus, minus, both = int((f == "+1R").sum()), int((f == "-1R").sum()), int((f == "both").sum())
    out.update(plus=plus, minus=minus, both=both, none=int((f == "none").sum()),
               first=round(plus / (plus + minus + both), 3) if (plus + minus + both) else None)
    return out


def setup_groups(df: pd.DataFrame) -> Dict[str, List[int]]:
    fam: Dict[str, List[int]] = {}
    for i, s in df.setup.items():
        for name in str(s).replace("+", ",").split(","):
            name = name.strip()
            if name:
                fam.setdefault(name, []).append(i)
    return fam


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    """بازهٔ اطمینان ۹۵٪ برای درصد (ویلسون): (پایین، بالا)؛ بدون نمونه ← (0, 1)."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return float(c - h), float(c + h)


def label_group(setup: Any, stop_dist: Any, tight: float = TIGHT_STOP, breaks: tuple = BREAKS) -> str:
    """دستهٔ برچسب یک سیگنال: حد ضرر نزدیک ← tight؛ وگرنه اگر range_break/cloud_break دارد ← break؛ بقیه ← plain."""
    d = _f(stop_dist)
    if not np.isnan(d) and d <= tight:
        return "tight"
    names = {x.strip() for x in str(setup).replace("+", ",").split(",")}
    return "break" if names & set(breaks) else "plain"


def label_column(df: pd.DataFrame, tight: float = TIGHT_STOP) -> pd.Series:
    dist = (pd.to_numeric(df.price, errors="coerce") - pd.to_numeric(df.sl, errors="coerce")).abs()
    return pd.Series([label_group(s, d, tight) for s, d in zip(df.setup, dist)], index=df.index, dtype=object)


DECISION_FA = {"contra": "خلاف جهت گرفتید", "taken": "تأیید کردید", "rejected": "رد کردید", "expired": "جواب ندادید (منقضی)",
               "blocked": "سقف پر بود", "pending": "بی‌جواب", "invalid": "باطل", "cancelled": "لغو شد",
               "order_failed": "خطای سفارش", "order_uncertain": "سفارش نامشخص"}


def audit_report(df: pd.DataFrame, offset_min: Optional[int] = None, tight: float = TIGHT_STOP) -> Dict[str, Any]:
    """کارنامهٔ سیگنال‌ها برای داشبورد: کل، برچسب، جهت، ستاپ، ساعت تهران، تصمیم."""
    rep: Dict[str, Any] = {"n": int(len(df)), "all": group_stats(df),
                           "from": str(df.ts.min()) if len(df) else None, "to": str(df.ts.max()) if len(df) else None}
    rep["side"] = [dict(group_stats(g), key=str(k)) for k, g in df.groupby("side")]
    rep["setup"] = sorted((dict(group_stats(df.loc[ids]), key=k) for k, ids in setup_groups(df).items()),
                          key=lambda x: -(x.get("first") or 0) if x["n"] >= 5 else 1)
    th = df.bar.map(lambda b: tehran_hour(b, offset_min))
    rep["hour"] = [dict(group_stats(df[(th >= lo) & (th < hi)]), key=f"{lo:02d}-{hi:02d}") for lo, hi in TEHRAN_BUCKETS]
    rep["decision"] = [dict(group_stats(g), key=str(k), fa=DECISION_FA.get(str(k), str(k)))
                       for k, g in df.groupby("decision")]
    lab = label_column(df, tight) if len(df) else pd.Series([], dtype=object)
    rep["label"] = [dict(group_stats(df[lab == k]), key=k, fa=LABEL_FA[k]) for k in LABEL_FA]
    rep["tight"] = tight
    return rep


def summarize(df: pd.DataFrame, offset_min: Optional[int] = None) -> str:
    ok = df[df.status != "no_data"]
    out = [f"SCOUT SIGNAL AUDIT  alerts={len(df)}  with data={len(ok)}  "
           f"(from {df.ts.min() if len(df) else '-'} to {df.ts.max() if len(df) else '-'})",
           "up% = share of signals that moved in the signal direction; R = alert stop distance",
           "", "== ALL =="]
    out.append(_line("all", df))
    out.append("")
    out.append(f"== BY ALERT LABEL (tight = stop <= {TIGHT_STOP:g}; break = has range_break/cloud_break) ==")
    lab = label_column(df) if len(df) else pd.Series([], dtype=object)
    for k in LABEL_FA:
        out.append(_line(k, df[lab == k]))
    out.append("")
    out.append("== BY SIDE ==")
    for sd, g in df.groupby("side"):
        out.append(_line(str(sd), g))
    out.append("")
    out.append("== BY YOUR DECISION ==")
    for dc, g in df.groupby("decision"):
        out.append(_line(str(dc), g))
    out.append("")
    out.append("== BY SETUP (a signal with several setups counts in each) ==")
    for name, ids in sorted(setup_groups(df).items(), key=lambda kv: -len(kv[1])):
        out.append(_line(name, df.loc[ids]))
    out.append("")
    out.append("== BY TEHRAN HOUR (signal time) ==")
    th = df.bar.map(lambda b: tehran_hour(b, offset_min))
    for lo, hi in TEHRAN_BUCKETS:
        out.append(_line(f"{lo:02d}-{hi:02d}", df[(th >= lo) & (th < hi)]))
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Scout signal audit (read-only)")
    ap.add_argument("--config", default="config/settings_scout.yaml")
    ap.add_argument("--journal", default=None, help="پیش‌فرض: scout.journal در تنظیمات")
    ap.add_argument("--since", default=None, help="مثل 2026-10-05 (زمان VPS در ژورنال)")
    ap.add_argument("--until", default=None)
    ap.add_argument("--horizon", type=int, default=60, help="دقیقه برای MFE/MAE")
    ap.add_argument("--race", type=int, default=240, help="دقیقه برای «اول ‎+1R یا ‎-1R»")
    ap.add_argument("--tehran-offset", type=int, default=None,
                    help="دقیقه: ساعت تهران منهای ساعت سرور بروکر (پیش‌فرض خودکار: تابستان 30، زمستان 90)")
    ap.add_argument("--out", default="data/scout_signal_audit.csv")
    a = ap.parse_args()
    from core import Settings
    from core.mt5_client import MT5Client, MT5Config
    s = Settings.load(a.config)
    journal = Path(a.journal or str(s.get("scout.journal", "data/scout_journal_v2.csv")))
    alerts = read_alerts(journal, a.since, a.until)
    if alerts.empty:
        print("no alerts in the journal for this period")
        return
    cli = MT5Client(MT5Config.from_settings(s))
    m1 = None
    if cli.connect():
        try:
            first = pd.Timestamp(alerts.bar.min())
            minutes = int((pd.Timestamp.now() - first).total_seconds() // 60) + 2 * 1440 + a.race
            m1 = cli.get_history_bars("M1", max(minutes, 2000))
        finally:
            try:
                cli.shutdown()
            except Exception:
                pass
    else:
        print("WARNING: MT5 connection failed; no price data")
    res = audit(alerts, m1, a.horizon, a.race)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(a.out, index=False, encoding="utf-8-sig")
    print(summarize(res, a.tehran_offset))
    print(f"\ndetails per signal: {a.out}")


if __name__ == "__main__":
    main()
