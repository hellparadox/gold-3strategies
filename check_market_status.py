# check_market_status.py — ابزار تشخیصی: اتصال MT5، نماد فعال، اسپرد،
# ساعت سرور بروکر و جهت روند (بازنویسی‌شده با APIهای فعلی پروژه).
from __future__ import annotations

import sys

from loguru import logger

from core import Settings
from core.indicators import IndicatorSpec, enrich
from core.mt5_client import MT5Client, MT5Config

logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")


def main() -> None:
    settings = Settings.load()
    client = MT5Client(MT5Config.from_settings(settings))
    if not client.connect():
        logger.error("اتصال به ترمینال MT5 برقرار نشد")
        return

    try:
        tick = client.get_tick()
        if tick is None:
            logger.error("تیک قیمت دریافت نشد (ترمینال/نماد را چک کنید)")
            return

        spec = IndicatorSpec(
            ema_periods=(50, 200), rsi_period=None, macd_params=None,
            bb_params=None, atr_period=14, adx_period=None,
        )
        h1 = enrich(client.get_rates("H1", 300), spec)
        h1_last = h1.iloc[-2]  # آخرین کندل بسته‌شده ([-1] کندلِ در حال تشکیل است)
        h1_close = float(h1_last["close"])
        h1_ema50 = float(h1_last["ema_50"])
        h1_ema200 = float(h1_last["ema_200"])

        print("\n" + "=" * 60)
        print("🔍 گزارش زنده وضعیت اتصال و فیلترهای ربات:")
        print("=" * 60)
        print(f"💰 {client.symbol}: bid={tick.bid:,.2f} | ask={tick.ask:,.2f} | spread={client.spread_points():.0f} pts")
        print(f"🧭 ساعت سرور بروکر: {client.server_time()}")
        print(f"📈 [H1] close={h1_close:,.2f} | EMA50={h1_ema50:,.2f} | EMA200={h1_ema200:,.2f}")
        if h1_close > h1_ema200 and h1_close > h1_ema50:
            print("   روند کلان: صعودی 🟢")
        elif h1_close < h1_ema200 and h1_close < h1_ema50:
            print("   روند کلان: نزولی 🔴")
        else:
            print("   روند کلان: خنثی/در حال تغییر 🟡")
        print("=" * 60 + "\n")
    finally:
        client.shutdown()


if __name__ == "__main__":
    main()