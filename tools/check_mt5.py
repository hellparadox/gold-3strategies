import sys
from datetime import datetime, timezone
import MetaTrader5 as mt5

def main():
    print("=" * 60)
    print("🔍 MT5 DIAGNOSTIC & CONNECTION HEALTH CHECK")
    print("=" * 60)

    # 1. تست اتصال اولیه به ترمینال MT5
    if not mt5.initialize():
        print(f"❌ MT5 Initialize Failed! Error code: {mt5.last_error()}")
        sys.exit(1)
    
    print("✅ MT5 Terminal Initialized successfully.")

    # 2. بررسی وضعیت اکانت و اتصال به سرور بروکر
    terminal_info = mt5.terminal_info()
    account_info = mt5.account_info()

    if terminal_info is None or account_info is None:
        print("❌ Cannot retrieve Account/Terminal info.")
        mt5.shutdown()
        sys.exit(1)

    print(f"📡 Broker Connected: {'YES' if terminal_info.connected else 'NO (Check Internet/Server)'}")
    print(f"👤 Account Number  : {account_info.login}")
    print(f"🏢 Server Name     : {account_info.server}")
    print(f"💰 Account Balance : ${account_info.balance:.2f}")

    if not terminal_info.connected:
        print("\n⚠️ WARNING: Your terminal is disconnected from the broker server!")
        mt5.shutdown()
        sys.exit(0)

    # 3. بررسی ساعت سرور و بازار
    # نمادهای مختلف طلا برای شناسایی نماد فعال
    candidate_symbols = ["XAUUSD", "XAUUSD.a", "XAUUSDm", "XAUUSD_o", "XAUUSD.pro", "GOLD", "GOLDm", "XAUUSD.raw"]
    
    active_symbol = None
    print("-" * 60)
    print("🔍 Probing Gold (XAUUSD) Symbols...")

    for sym in candidate_symbols:
        info = mt5.symbol_info(sym)
        if info is not None:
            # مطمئن شدن از فعال بودن نماد در Market Watch
            if not info.visible:
                mt5.symbol_select(sym, True)
                info = mt5.symbol_info(sym)
            
            if info and info.visible:
                active_symbol = sym
                print(f"✅ Found active symbol on your broker: '{sym}'")
                print(f"   - Trade Mode : {info.trade_mode} (4 = Full Access)")
                print(f"   - Spread     : {info.spread} points")
                print(f"   - Bid Price  : {info.bid}")
                print(f"   - Ask Price  : {info.ask}")
                
                # بررسی آخرین تیک دریافتی
                tick = mt5.symbol_info_tick(sym)
                if tick:
                    tick_time = datetime.fromtimestamp(tick.time, tz=timezone.utc)
                    print(f"   - Last Tick Time (UTC): {tick_time.strftime('%Y-%m-%d %H:%M:%S')}")
                break

    if not active_symbol:
        print("❌ No matching Gold symbol found in your Market Watch!")
    
    print("=" * 60)
    mt5.shutdown()

if __name__ == "__main__":
    main()