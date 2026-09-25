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
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ai_gate.evaluate import (  # noqa: E402  (re-exported for callers and tests)
    BOOT_N, BOOT_SEED, CONF_BUCKETS, HORIZON_HOURS, MIN_OK, MIN_SKIP, ExitRules, SimResult,
    bootstrap_uplift, brier, evaluate, simulate, usd as _usd, verdict,
)


def render(report: Dict[str, Any], strategy: str) -> str:
    L = [f"🤖 ارزیابی دروازهٔ هوش مصنوعی — {strategy}",
         f"کل درخواست‌ها: {report['total']} · سالم: {report['ok']} · وضعیت‌ها: "
         + "، ".join(f"{k}={v}" for k, v in sorted(report['by_status'].items(), key=lambda kv: str(kv[0]))),
         f"هزینهٔ کل: ${report['cost']:.3f} · تأخیر p50/p95: {report['latency_p50']}/{report['latency_p95']} ms"]
    if not report["simulated"]:
        L.append("⚠️ هنوز شبیه‌سازی کامل‌شده‌ای نیست؛ فقط نتیجهٔ واقعی معاملاتِ باز شده:")
    else:
        t, s = report["take"], report["skip"]
        L.append("شبیه‌سازی یکسان روی M1 بروکر (همهٔ سیگنال‌ها، ۰.۰۱ لات):")
        L.append(f"  «وارد شو»: {t['n']} سیگنال · جمع {t['sum']:+.2f}$ · میانگین {_usd(t['mean'])}")
        L.append(f"  «وارد نشو»: {s['n']} سیگنال · جمع {s['sum']:+.2f}$ · میانگین {_usd(s['mean'])}")
        acc = report["accuracy"]
        if acc["pct"] is not None:
            L.append(f"  دقت: {acc['pct']}٪ ({acc['right']} درست، {acc['wrong']} غلط، {acc['neutral']} خنثی)")
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
    ap.add_argument("--no-mt5", action="store_true",
                    help="no MT5: use the simulations the bot already stored in the journal")
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
