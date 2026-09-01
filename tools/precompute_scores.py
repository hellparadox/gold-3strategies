"""Precompute signal-quality bucket stats from a real backtest.

Fetches live MT5 history, runs the configured strategy through the backtest
engine, buckets every trade by (strategy, side, session, ATR regime) and
writes ``data/signal_scores.json`` for the live ``SignalScorer``.

Usage:
    python tools/precompute_scores.py                          # active strategy
    python tools/precompute_scores.py --config config/settings_ichimoku.yaml
    python tools/precompute_scores.py --bars 350000            # ~5 years
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict

# make repo root importable when run as `python tools/precompute_scores.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from loguru import logger  # noqa: E402

from backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from core import Settings  # noqa: E402
from core.mt5_client import MT5Client, MT5Config  # noqa: E402
from core.risk_manager import RiskConfig, SymbolSpec  # noqa: E402
from core.signal_score import session_bucket  # noqa: E402
from strategies import build_from_settings  # noqa: E402


def build_buckets(trades: list) -> Dict[str, Any]:
    overall_wins = sum(1 for t in trades if t.net_profit > 0)

    # ATR terciles across all trades -> low / mid / high regime bands
    atrs = np.array([t.atr for t in trades], dtype="float64")
    if atrs.size >= 6:
        low, high = (float(x) for x in np.percentile(atrs, [33.3, 66.7]))
    else:
        low, high = 0.60, 1.20

    buckets: Dict[str, Dict[str, int]] = defaultdict(lambda: {"trades": 0, "wins": 0})
    for t in trades:
        atr_bucket = "low" if t.atr <= low else ("high" if t.atr >= high else "mid")
        key = f"{t.side.upper()}|{session_bucket(t.entry_time.hour)}|{atr_bucket}"
        buckets[key]["trades"] += 1
        if t.net_profit > 0:
            buckets[key]["wins"] += 1

    # final keys carry the strategy name so multiple bots can share one file
    return {
        "meta": {
            "atr_bands": [round(low, 4), round(high, 4)],
            "total_trades": len(trades),
        },
        "overall": {"trades": len(trades), "wins": overall_wins},
        "buckets": dict(buckets),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Precompute signal quality scores")
    parser.add_argument("--config", default=None, help="settings yaml (default: config/settings.yaml)")
    parser.add_argument("--strategy", default=None, help="override strategy.active")
    parser.add_argument("--bars", type=int, default=250_000, help="M5 bars of history (more = better stats)")
    parser.add_argument("--out", default="data/signal_scores.json", help="output json path")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")

    settings = Settings.load(args.config)
    if args.strategy:
        settings.set("strategy.active", args.strategy)
    strategy = build_from_settings(settings)
    risk_config = RiskConfig.from_settings(settings)
    bt_config = BacktestConfig.from_settings(settings)

    client = MT5Client(MT5Config.from_settings(settings))
    if not client.connect():
        logger.error("MT5 connection failed")
        return
    m5 = client.get_history_bars("M5", args.bars)
    m15 = client.get_history_bars("M15", max(60_000, args.bars // 3))
    h1 = client.get_history_bars("H1", max(10_000, args.bars // 12))
    client.shutdown()
    logger.info("data | M5={} M15={} H1={}", len(m5), len(m15), len(h1))

    engine = BacktestEngine(
        strategy=strategy,
        risk_config=risk_config,
        backtest_config=bt_config,
        spec=SymbolSpec.gold_default(),
        symbol="XAUUSD",
        timeframe="M5",
    )
    result = engine.run(m5, m15, h1)
    if not result.trades:
        logger.error("no trades produced — cannot build score stats")
        return

    payload = build_buckets(result.trades)
    # stamp the strategy name into every bucket key
    payload["buckets"] = {
        f"{strategy.name}|{k}": v for k, v in payload["buckets"].items()
    }
    payload["meta"]["strategy"] = strategy.name
    payload["meta"]["period"] = [str(result.period_start), str(result.period_end)]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    wr = payload["overall"]["wins"] / max(1, payload["overall"]["trades"]) * 100
    logger.success(
        "wrote {} | {} buckets | overall WR {:.1f}% | atr_bands {}",
        out, len(payload["buckets"]), wr, payload["meta"]["atr_bands"],
    )


if __name__ == "__main__":
    main()
