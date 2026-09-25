#!/usr/bin/env python3
"""AI gate evaluation: did the model's verdicts separate good from bad entries?

Read-only: reads the gate journal (data/ai_gate_<strategy>.db) and, unless
``--no-mt5``, broker M1 history.  It never sends orders.

Method
------
* Every verdict with status ``ok`` is simulated identically — TAKE and SKIP,
  traded or not — on broker M1 bars from the close of the signal bar, with the
  bot's own exit rules (SL/TP of the plan, break-even and trailing from the
  bot's settings), the spread, conservative intrabar order (stop first; stop
  and target in the same bar = stop) and a 48 h horizon.  This makes TAKE and
  SKIP comparable; realised P/L exists only for trades that were opened.
* uplift = P/L if only TAKE signals were traded − P/L of all signals
  (= −P/L of SKIP signals), with a 90 % bootstrap interval (10 000 resamples,
  seed 12345).
* Pre-registered verdict — recommend ``live`` only if ALL hold: ≥ 60 ok
  verdicts, ≥ 15 SKIP, lower bound of uplift > 0, mean P/L of SKIP < 0.

Usage:
    py -3.11 tools/ai_gate_report.py --strategy ichimoku_m15 --config config/settings_ichimoku.yaml
    py -3.11 tools/ai_gate_report.py --strategy ichimoku_m15 --no-mt5
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MIN_OK, MIN_SKIP = 60, 15
BOOT_N, BOOT_SEED = 10_000, 12345
HORIZON_HOURS = 48
CONF_BUCKETS = ((0, 59, "<60"), (60, 69, "60–69"), (70, 79, "70–79"), (80, 100, "80+"))


@dataclass(frozen=True)
class ExitRules:
    be_trigger_atr: float
    be_offset_points: float
    trail_trigger_atr: float
    trail_dist_atr: float
    point: float = 0.01
    usd_per_price: float = 1.0          # 0.01 lot XAUUSD: $1 per 1.00 move


@dataclass(frozen=True)
class SimResult:
    profit_usd: float
    exit: str                            # tp | sl | be | trail | horizon | no_data
    protected: bool                      # reached +be_trigger ATR before the stop
    bars: int


def simulate(side: str, entry: float, sl: float, tp: float, atr: float,
             bars: pd.DataFrame, rules: ExitRules, spread_points: float) -> SimResult:
    """Bar-by-bar exit simulation on bid OHLC (see module docstring)."""
    if bars is None or bars.empty or atr <= 0:
        return SimResult(0.0, "no_data", False, 0)
    buy = str(side).upper() == "BUY"
    stop = float(sl)
    stage = "sl"                         # which rule set the current stop: sl | be | trail
    protected = False
    be_level = entry + (rules.be_offset_points * rules.point if buy else -rules.be_offset_points * rules.point)
    best = entry
    has_spread = "spread" in bars.columns
    for i, (_, bar) in enumerate(bars.iterrows(), start=1):
        spread = (float(bar["spread"]) if has_spread and not pd.isna(bar["spread"]) else spread_points) * rules.point
        if buy:
            low, high = float(bar["low"]), float(bar["high"])                 # exit side = bid
        else:
            low, high = float(bar["low"]) + spread, float(bar["high"]) + spread  # exit side = ask
        hit_stop = low <= stop if buy else high >= stop
        hit_tp = high >= tp if buy else low <= tp
        if hit_stop:
            pnl = (stop - entry) if buy else (entry - stop)
            return SimResult(round(pnl * rules.usd_per_price, 2), stage, protected, i)
        if hit_tp:
            pnl = (tp - entry) if buy else (entry - tp)
            return SimResult(round(pnl * rules.usd_per_price, 2), "tp", True, i)
        # update management from this bar's extremes (effective from the next bar)
        best = max(best, high) if buy else min(best, low)
        favour = (best - entry) if buy else (entry - best)
        if favour >= rules.be_trigger_atr * atr:
            protected = True
            if (be_level > stop) if buy else (be_level < stop):
                stop, stage = be_level, "be"
        if favour >= rules.trail_trigger_atr * atr:
            trail = best - rules.trail_dist_atr * atr if buy else best + rules.trail_dist_atr * atr
            if (trail > stop) if buy else (trail < stop):
                stop, stage = trail, "trail"
    last = float(bars["close"].iloc[-1])
    if not buy:
        last += (float(bars["spread"].iloc[-1]) if has_spread else spread_points) * rules.point
    pnl = (last - entry) if buy else (entry - last)
    return SimResult(round(pnl * rules.usd_per_price, 2), "horizon", protected, len(bars))


def bootstrap_uplift(take: Sequence[float], skip: Sequence[float],
                     n: int = BOOT_N, seed: int = BOOT_SEED) -> Dict[str, float]:
    """uplift = −sum(SKIP P/L); signals resampled jointly with replacement."""
    values = np.array(list(take) + list(skip), dtype=float)
    is_skip = np.array([False] * len(take) + [True] * len(skip))
    point = float(-values[is_skip].sum()) if len(values) else 0.0
    if len(values) == 0:
        return {"uplift": 0.0, "lo": 0.0, "hi": 0.0}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(values), size=(n, len(values)))
    sims = -(values[idx] * is_skip[idx]).sum(axis=1)
    return {"uplift": round(point, 2), "lo": round(float(np.percentile(sims, 5)), 2),
            "hi": round(float(np.percentile(sims, 95)), 2)}


def verdict(n_ok: int, n_skip: int, boot: Dict[str, float], skip_mean: Optional[float]) -> Dict[str, Any]:
    checks = [
        (n_ok >= MIN_OK, f"تصمیم‌های سالم ≥ {MIN_OK} (الان {n_ok})"),
        (n_skip >= MIN_SKIP, f"تعداد «وارد نشو» ≥ {MIN_SKIP} (الان {n_skip})"),
        (boot["lo"] > 0, f"کران پایین بهبود > 0 (الان {boot['lo']:+.2f}$)"),
        (skip_mean is not None and skip_mean < 0,
         f"میانگین سود «وارد نشو»ها منفی باشد (الان {_usd(skip_mean)})"),
    ]
    ok = all(c for c, _ in checks)
    return {"go_live": ok, "checks": [{"ok": c, "text": t} for c, t in checks]}


def brier(pairs: Sequence[tuple]) -> Optional[float]:
    pairs = [(float(p), 1.0 if o else 0.0) for p, o in pairs if p is not None]
    if not pairs:
        return None
    return round(sum((p - o) ** 2 for p, o in pairs) / len(pairs), 4)


def _mean(xs: Sequence[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 2) if xs else None


def evaluate(rows: List[Dict[str, Any]], bars_for: Optional[Callable[[Dict[str, Any]], pd.DataFrame]],
             rules: Optional[ExitRules]) -> Dict[str, Any]:
    """Core evaluation (pure given ``bars_for``)."""
    ok = [r for r in rows if r.get("status") == "ok"]
    report: Dict[str, Any] = {
        "total": len(rows), "ok": len(ok),
        "by_status": {}, "latency_p50": None, "latency_p95": None,
        "cost": round(sum(float(r.get("cost_usd") or 0) for r in rows), 4),
        "items": [],
    }
    for r in rows:
        report["by_status"][r.get("status")] = report["by_status"].get(r.get("status"), 0) + 1
    lat = [int(r["latency_ms"]) for r in ok if r.get("latency_ms")]
    if lat:
        report["latency_p50"] = int(np.percentile(lat, 50))
        report["latency_p95"] = int(np.percentile(lat, 95))

    sims: Dict[str, List[float]] = {"TAKE": [], "SKIP": []}
    buckets: Dict[str, Dict[str, List[float]]] = {}
    brier_pairs = []
    actual: Dict[str, List[float]] = {"TAKE": [], "SKIP": []}
    for r in ok:
        item = {"id": r.get("id"), "created_utc": r.get("created_utc"), "side": r.get("side"),
                "layer": r.get("layer"), "decision": r.get("decision"),
                "confidence": r.get("confidence"), "p_protect": r.get("p_protect"),
                "action": r.get("action"), "ticket": r.get("ticket"),
                "actual_profit": r.get("profit"), "sim_profit": None, "sim_exit": None,
                "sim_protected": None}
        if r.get("profit") is not None:
            actual[r["decision"]].append(float(r["profit"]))
        if bars_for is not None and rules is not None:
            bars = bars_for(r)
            res = simulate(r["side"], float(r["entry_plan"]), float(r["sl_plan"]), float(r["tp_plan"]),
                           float(r["atr"]), bars, rules, float(r.get("spread_points") or 0.0))
            if res.exit != "no_data":
                item.update(sim_profit=res.profit_usd, sim_exit=res.exit, sim_protected=res.protected)
                sims[r["decision"]].append(res.profit_usd)
                conf = int(r.get("confidence") or 0)
                label = next(lbl for lo, hi, lbl in CONF_BUCKETS if lo <= conf <= hi)
                buckets.setdefault(label, {"TAKE": [], "SKIP": []})[r["decision"]].append(res.profit_usd)
                brier_pairs.append((r.get("p_protect"), res.protected))
        report["items"].append(item)

    boot = bootstrap_uplift(sims["TAKE"], sims["SKIP"])
    skip_mean = _mean(sims["SKIP"])
    report.update({
        "simulated": bars_for is not None,
        "take": {"n": len(sims["TAKE"]), "sum": round(sum(sims["TAKE"]), 2), "mean": _mean(sims["TAKE"])},
        "skip": {"n": len(sims["SKIP"]), "sum": round(sum(sims["SKIP"]), 2), "mean": skip_mean},
        "actual": {k: {"n": len(v), "sum": round(sum(v), 2), "mean": _mean(v)} for k, v in actual.items()},
        "uplift": boot,
        "buckets": {k: {d: {"n": len(v), "mean": _mean(v)} for d, v in b.items()}
                    for k, b in sorted(buckets.items())},
        "brier": brier(brier_pairs),
        "verdict": verdict(len(sims["TAKE"]) + len(sims["SKIP"]), len(sims["SKIP"]), boot, skip_mean),
    })
    return report


def _usd(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:+.2f}$"


def render(report: Dict[str, Any], strategy: str) -> str:
    L = [f"🤖 ارزیابی دروازهٔ هوش مصنوعی — {strategy}",
         f"کل درخواست‌ها: {report['total']} · سالم: {report['ok']} · وضعیت‌ها: "
         + "، ".join(f"{k}={v}" for k, v in sorted(report['by_status'].items(), key=lambda kv: str(kv[0]))),
         f"هزینهٔ کل: ${report['cost']:.3f} · تأخیر p50/p95: {report['latency_p50']}/{report['latency_p95']} ms"]
    if not report["simulated"]:
        L.append("⚠️ شبیه‌سازی انجام نشد (--no-mt5)؛ فقط نتیجهٔ واقعی معاملاتِ باز شده:")
    else:
        t, s = report["take"], report["skip"]
        L.append("شبیه‌سازی یکسان روی M1 بروکر (همهٔ سیگنال‌ها، ۰.۰۱ لات):")
        L.append(f"  «وارد شو»: {t['n']} سیگنال · جمع {t['sum']:+.2f}$ · میانگین {_usd(t['mean'])}")
        L.append(f"  «وارد نشو»: {s['n']} سیگنال · جمع {s['sum']:+.2f}$ · میانگین {_usd(s['mean'])}")
        u = report["uplift"]
        L.append(f"  بهبود اگر فقط «وارد شو»ها گرفته می‌شد: {u['uplift']:+.2f}$ "
                 f"(بازهٔ ۹۰٪: {u['lo']:+.2f} تا {u['hi']:+.2f})")
        if report["brier"] is not None:
            L.append(f"  Brier برای احتمال رسیدن به سربه‌سر: {report['brier']} (کمتر بهتر؛ 0.25 = بی‌اطلاع)")
        for label, groups in report["buckets"].items():
            parts = [f"{'وارد شو' if d == 'TAKE' else 'وارد نشو'} {g['n']} ({_usd(g['mean'])})"
                     for d, g in groups.items() if g["n"]]
            if parts:
                L.append(f"  اطمینان {label}: " + " · ".join(parts))
    a = report["actual"]
    L.append(f"نتیجهٔ واقعی (فقط معاملات باز شده): «وارد شو» {a['TAKE']['n']} → {a['TAKE']['sum']:+.2f}$ · "
             f"«وارد نشو» {a['SKIP']['n']} → {a['SKIP']['sum']:+.2f}$")
    v = report["verdict"]
    L.append("حکم: " + ("✅ روشن کردن live پیشنهاد می‌شود." if v["go_live"] else "⏳ هنوز نه."))
    for c in v["checks"]:
        L.append(("  ✔ " if c["ok"] else "  ✘ ") + c["text"])
    return "\n".join(L)


def write_csv(path: str, items: List[Dict[str, Any]]) -> None:
    if not items:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(items[0].keys()))
        writer.writeheader()
        writer.writerows(items)


def _mt5_bars_loader(settings: Any, trigger_minutes_default: int) -> Callable[[Dict[str, Any]], pd.DataFrame]:
    from core.mt5_client import MT5Client, MT5Config
    client = MT5Client(MT5Config.from_settings(settings))
    if not client.connect():
        raise SystemExit("MT5 connection failed (use --no-mt5 for journal-only stats)")

    def load(row: Dict[str, Any]) -> pd.DataFrame:
        ctx = json.loads(row.get("context_json") or "{}")
        tf = str(ctx.get("trigger_tf") or f"M{trigger_minutes_default}")
        minutes = int(tf[1:]) if tf[1:].isdigit() else trigger_minutes_default
        start = pd.Timestamp(row["ref_time_server"]) + timedelta(minutes=minutes)
        end = start + timedelta(hours=HORIZON_HOURS)
        # MT5 python treats datetimes as UTC epochs; bar times are server clock
        frame = client.get_rates_range("M1", start.to_pydatetime().replace(tzinfo=timezone.utc),
                                       end.to_pydatetime().replace(tzinfo=timezone.utc))
        return frame.loc[frame.index >= start] if not frame.empty else frame
    return load


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate the AI pre-trade gate journal (read-only).")
    ap.add_argument("--strategy", default="ichimoku_m15")
    ap.add_argument("--config", default="config/settings_ichimoku.yaml",
                    help="bot settings (exit rules, MT5 connection)")
    ap.add_argument("--journal", default=None, help="default: data/ai_gate_<strategy>.db")
    ap.add_argument("--days", type=int, default=0, help="only the last N days (0 = all)")
    ap.add_argument("--no-mt5", action="store_true", help="no simulation; journal and realised P/L only")
    ap.add_argument("--out", default="data/ai_gate_eval.csv")
    args = ap.parse_args(argv)

    from core.ai_gate.journal import DecisionJournal
    path = args.journal or os.path.join("data", f"ai_gate_{args.strategy}.db")
    if not os.path.exists(path):
        print(f"journal not found: {path}")
        return 1
    journal = DecisionJournal(path)
    since = None
    if args.days > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = journal.rows(since)

    loader, rules = None, None
    if not args.no_mt5:
        from core import Settings
        from core.risk_manager import RiskConfig
        settings = Settings.load(args.config)
        rc = RiskConfig.from_settings(settings)
        rules = ExitRules(rc.breakeven_trigger_atr, rc.breakeven_buffer_points,
                          rc.trailing_trigger_atr, rc.trailing_distance_atr)
        loader = _mt5_bars_loader(settings, 15)
    report = evaluate(rows, loader, rules)
    print(render(report, args.strategy))
    write_csv(args.out, report["items"])
    if report["items"]:
        print(f"CSV: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
