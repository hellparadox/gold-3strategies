# check_market_status.py
import yaml
from core.mt5_client import MT5Client
from core.indicators import IndicatorCalculator

with open("config/settings.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

client = MT5Client(config["symbol"])
if client.connect():
    calc = IndicatorCalculator(config)
    
    # دریافت ۳۰۰ کندل برای پر شدن دوره ۲۰۰ کندلی EMA
    df_m5 = calc.add_all_indicators(client.get_rates("M5", count=300))
    df_m15 = calc.add_all_indicators(client.get_rates("M15", count=300))
    df_h1 = calc.add_all_indicators(client.get_rates("H1", count=300))
    
    tick = client.get_current_tick()
    client.disconnect()
    
    m5_last = df_m5.iloc[-2]
    m15_last = df_m15.iloc[-2]
    h1_last = df_h1.iloc[-2]
    
    h1_ema_trend = h1_last.get("ema_trend", h1_last.get("ema_slow", 0.0))
    m15_ema_slow = m15_last.get("ema_slow", 0.0)
    
    h1_trend_status = "صعودی 🟢" if h1_last["close"] > h1_ema_trend else "نزولی 🔴"
    m15_trend_status = "صعودی 🟢" if m15_last["close"] > m15_ema_slow else "نزولی 🔴"
    
    print("\n" + "=" * 60)
    print("🔍 گزارش زنده وضعیت فیلترهای ورود ربات:")
    print("=" * 60)
    print(f"💰 قیمت لحظه‌ای طلا: {tick.bid:,.2f}")
    print(f"📈 [H1] قیمت: {h1_last['close']:.2f} | EMA Trend: {h1_ema_trend:.2f} -> روند کلان: {h1_trend_status}")
    print(f"📊 [M15] قیمت: {m15_last['close']:.2f} | EMA 21: {m15_ema_slow:.2f} -> شتاب میانی: {m15_trend_status}")
    print(f"🎯 [M5] EMA 9: {m5_last.get('ema_fast',0):.2f} | EMA 21: {m5_last.get('ema_slow',0):.2f} | MACD: {m5_last.get('macd',0):.3f}")
    print("=" * 60 + "\n")