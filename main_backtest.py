# main_backtest.py — backtest runner driven by a settings yaml.
# Usage:
#   python main_backtest.py                                    # config/settings.yaml (instance 1)
#   python main_backtest.py --config config/settings_ichimoku.yaml --bars 25000
#   python main_backtest.py --strategy ichimoku_m15            # override active strategy
from __future__ import annotations

import argparse
import sys
import time

from loguru import logger

from backtest.engine import BacktestConfig, BacktestEngine
from core import Settings
from core.mt5_client import MT5Client, MT5Config
from core.risk_manager import RiskConfig, SymbolSpec
from strategies import build_from_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="XAUUSD backtest on real MT5 history")
    parser.add_argument("--config", default=None, help="path to settings yaml (default: config/settings.yaml)")
    parser.add_argument("--strategy", default=None, help="override strategy.active from the config")
    parser.add_argument("--bars", type=int, default=75000, help="M5 bars to test (~288 per day)")
    parser.add_argument("--spread", type=float, default=None, help="override spread points")
    parser.add_argument("--slippage", type=float, default=None, help="override slippage points")
    parser.add_argument("--commission", type=float, default=None, help="override commission $/lot")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")

    settings = Settings.load(args.config)
    if args.strategy:
        settings.set("strategy.active", args.strategy)
    strategy = build_from_settings(settings)
    risk_config = RiskConfig.from_settings(settings)
    bt_config = BacktestConfig.from_settings(settings)
    if args.spread is not None:
        bt_config.spread_points = args.spread
    if args.slippage is not None:
        bt_config.slippage_points = args.slippage
    if args.commission is not None:
        bt_config.commission_per_lot = args.commission

    logger.info(
        "strategy={} | risk={}% | spread={}pts slip={}pts comm=${}/lot | session {}-{}",
        strategy.name, risk_config.risk_percent, bt_config.spread_points,
        bt_config.slippage_points, bt_config.commission_per_lot,
        bt_config.session_start_hour, bt_config.session_end_hour,
    )

    client = MT5Client(MT5Config.from_settings(settings))
    if not client.connect():
        logger.error("MT5 connection failed")
        return
    t0 = time.time()
    m5 = client.get_history_bars("M5", args.bars)
    m15 = client.get_history_bars("M15", max(12_000, args.bars // 3))
    h1 = client.get_history_bars("H1", max(4_000, args.bars // 12))
    client.shutdown()
    logger.info("data in {:.0f}s | M5={} M15={} H1={} | range {} .. {}",
                time.time() - t0, len(m5), len(m15), len(h1), m5.index[0], m5.index[-1])

    engine = BacktestEngine(
        strategy=strategy,
        risk_config=risk_config,
        backtest_config=bt_config,
        spec=SymbolSpec.gold_default(),
        symbol="XAUUSD",
        timeframe="M5",
    )
    result = engine.run(m5, m15, h1)

    print()
    for line in result.summary_lines():
        print(line)


if __name__ == "__main__":
    main()
