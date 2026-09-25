"""Evaluation of AI-gate verdicts (pure functions, shared by the dashboard and the CLI).

Method (pre-registered; do not tune after looking at results)
------------------------------------------------------------
* Every verdict with status ``ok`` is simulated identically — TAKE and SKIP,
  traded or not — on broker M1 bars from the close of the signal bar, with the
  bot's own exit rules (plan SL/TP, break-even and trailing from the bot's
  settings), the spread, conservative intrabar order (stop first; stop and
  target in the same bar = stop) and a 48 h horizon.
* A verdict is *right* when TAKE ended > +$0.50 or SKIP ended < −$0.50,
  *wrong* in the opposite case, *neutral* around break-even.
* uplift = P/L if only TAKE signals were traded − P/L of all signals
  (= −P/L of SKIP signals), 90 % bootstrap interval (10 000 resamples, seed 12345).
* Recommend ``live`` only if ALL hold: ≥ 60 evaluated verdicts, ≥ 15 SKIP,
  lower bound of uplift > 0, mean simulated P/L of SKIP < 0.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

MIN_OK, MIN_SKIP = 60, 15
BOOT_N, BOOT_SEED = 10_000, 12345
HORIZON_HOURS = 48
NEUTRAL_BAND = 0.5
CONF_BUCKETS = ((0, 59, "<60"), (60, 69, "60–69"), (70, 79, "70–79"), (80, 100, "80+"))
FINAL_EXITS = ("tp", "sl", "be", "trail")


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
            low, high = float(bar["low"]), float(bar["high"])                   # exit side = bid
        else:
            low, high = float(bar["low"]) + spread, float(bar["high"]) + spread  # exit side = ask
        if (low <= stop) if buy else (high >= stop):
            pnl = (stop - entry) if buy else (entry - stop)
            return SimResult(round(pnl * rules.usd_per_price, 2), stage, protected, i)
        if (high >= tp) if buy else (low <= tp):
            pnl = (tp - entry) if buy else (entry - tp)
            return SimResult(round(pnl * rules.usd_per_price, 2), "tp", True, i)
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
    if len(values) == 0:
        return {"uplift": 0.0, "lo": 0.0, "hi": 0.0}
    point = float(-values[is_skip].sum())
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(values), size=(n, len(values)))
    sims = -(values[idx] * is_skip[idx]).sum(axis=1)
    return {"uplift": round(point, 2), "lo": round(float(np.percentile(sims, 5)), 2),
            "hi": round(float(np.percentile(sims, 95)), 2)}


def usd(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:+.2f}$"


def verdict(n_ok: int, n_skip: int, boot: Dict[str, float], skip_mean: Optional[float]) -> Dict[str, Any]:
    checks = [
        (n_ok >= MIN_OK, f"حداقل {MIN_OK} تصمیم ارزیابی‌شده (الان {n_ok})"),
        (n_skip >= MIN_SKIP, f"حداقل {MIN_SKIP} بار «وارد نشو» (الان {n_skip})"),
        (boot["lo"] > 0, f"کران پایین اثر مثبت باشد (الان {boot['lo']:+.2f}$)"),
        (skip_mean is not None and skip_mean < 0,
         f"«وارد نشو»ها به‌طور میانگین ضررده باشند (الان {usd(skip_mean)})"),
    ]
    return {"go_live": all(c for c, _ in checks), "checks": [{"ok": c, "text": t} for c, t in checks]}


def brier(pairs: Sequence[tuple]) -> Optional[float]:
    pairs = [(float(p), 1.0 if o else 0.0) for p, o in pairs if p is not None]
    if not pairs:
        return None
    return round(sum((p - o) ** 2 for p, o in pairs) / len(pairs), 4)


def mean(xs: Sequence[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 2) if xs else None


def correctness(decision: str, profit: Optional[float]) -> Optional[str]:
    """right | wrong | neutral | None (not evaluated yet)."""
    if profit is None:
        return None
    if abs(profit) <= NEUTRAL_BAND:
        return "neutral"
    good = profit > 0
    return "right" if (good if decision == "TAKE" else not good) else "wrong"


def _stored_sim(row: Dict[str, Any]) -> Optional[SimResult]:
    if not row.get("sim_done") or row.get("sim_profit") is None or row.get("sim_exit") == "no_data":
        return None
    return SimResult(float(row["sim_profit"]), str(row.get("sim_exit") or ""),
                     bool(row.get("sim_protected")), 0)


def evaluate(rows: List[Dict[str, Any]],
             bars_for: Optional[Callable[[Dict[str, Any]], pd.DataFrame]] = None,
             rules: Optional[ExitRules] = None) -> Dict[str, Any]:
    """Aggregate statistics.  Simulations come from ``bars_for`` (CLI) or, when it is
    ``None``, from the finished simulations stored in the journal (dashboard)."""
    ok = [r for r in rows if r.get("status") == "ok"]
    report: Dict[str, Any] = {
        "total": len(rows), "ok": len(ok), "by_status": {},
        "latency_p50": None, "latency_p95": None,
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
    tally = {"right": 0, "wrong": 0, "neutral": 0}
    pending = 0
    for r in ok:
        item = {"id": r.get("id"), "created_utc": r.get("created_utc"), "side": r.get("side"),
                "layer": r.get("layer"), "decision": r.get("decision"),
                "confidence": r.get("confidence"), "p_protect": r.get("p_protect"),
                "action": r.get("action"), "ticket": r.get("ticket"),
                "actual_profit": r.get("profit"), "sim_profit": None, "sim_exit": None,
                "sim_protected": None, "correct": None}
        if r.get("profit") is not None and r.get("decision") in actual:
            actual[r["decision"]].append(float(r["profit"]))
        res: Optional[SimResult] = None
        if bars_for is not None and rules is not None:
            res = simulate(r["side"], float(r["entry_plan"]), float(r["sl_plan"]), float(r["tp_plan"]),
                           float(r["atr"]), bars_for(r), rules, float(r.get("spread_points") or 0.0))
            if res.exit == "no_data":
                res = None
        else:
            res = _stored_sim(r)
        if res is None:
            pending += 1
        else:
            item.update(sim_profit=res.profit_usd, sim_exit=res.exit, sim_protected=res.protected,
                        correct=correctness(r["decision"], res.profit_usd))
            tally[item["correct"]] += 1
            sims[r["decision"]].append(res.profit_usd)
            conf = int(r.get("confidence") or 0)
            label = next(lbl for lo, hi, lbl in CONF_BUCKETS if lo <= conf <= hi)
            buckets.setdefault(label, {"TAKE": [], "SKIP": []})[r["decision"]].append(res.profit_usd)
            brier_pairs.append((r.get("p_protect"), res.protected))
        report["items"].append(item)

    boot = bootstrap_uplift(sims["TAKE"], sims["SKIP"])
    skip_mean = mean(sims["SKIP"])
    decided = tally["right"] + tally["wrong"]
    report.update({
        "simulated": bars_for is not None or any(v for v in sims.values()),
        "pending": pending,
        "take": {"n": len(sims["TAKE"]), "sum": round(sum(sims["TAKE"]), 2), "mean": mean(sims["TAKE"])},
        "skip": {"n": len(sims["SKIP"]), "sum": round(sum(sims["SKIP"]), 2), "mean": skip_mean},
        "actual": {k: {"n": len(v), "sum": round(sum(v), 2), "mean": mean(v)} for k, v in actual.items()},
        "accuracy": {**tally, "pct": round(tally["right"] / decided * 100, 1) if decided else None},
        "uplift": boot,
        "buckets": {k: {d: {"n": len(v), "mean": mean(v)} for d, v in b.items()}
                    for k, b in sorted(buckets.items())},
        "brier": brier(brier_pairs),
        "verdict": verdict(len(sims["TAKE"]) + len(sims["SKIP"]), len(sims["SKIP"]), boot, skip_mean),
    })
    return report
