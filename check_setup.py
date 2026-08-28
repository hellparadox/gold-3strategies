#!/usr/bin/env python3
"""check_setup.py — پیش‌فلایت: بررسی کامل آمادگی VPS برای اجرای هر دو ربات.

اجرا:      python check_setup.py
خروجی:     چک‌لیست ✅/⚠️/❌  +  کد خروج 0 (همه اوکی) یا 1 (مشکل)
امنیت:     هیچ توکنی به‌صورت کامل چاپ نمی‌شود.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

results: list[tuple[str, bool]] = []


def check(title: str, ok: bool, detail: str = "", warn_only: bool = False) -> bool:
    mark = "✅" if ok else ("⚠️ " if warn_only else "❌")
    results.append((title, ok or warn_only))
    line = f"{mark} {title}"
    if detail:
        line += f"  — {detail}"
    print(line)
    return ok or warn_only


def mask(token: str | None) -> str:
    if not token:
        return "(خالی)"
    token = str(token)
    if len(token) <= 12:
        return token[:4] + "…"
    return f"{token[:8]}…{token[-4:]}  ({len(token)} کاراکتر)"


print("=" * 70)
print("🩺 CHECK SETUP — gold-3strategies (VPS pre-flight)")
print("=" * 70)

# ---------------------------------------------------------------- packages
print("\n📦 پکیج‌های پایتون:")
for mod in ("pandas", "numpy", "yaml", "loguru", "dotenv", "requests",
            "matplotlib", "mplfinance", "telegram", "MetaTrader5"):
    ok = importlib.util.find_spec(mod) is not None
    check(f"package {mod}", ok)

# ---------------------------------------------------------------- git
print("\n🌐 وضعیت گیت:")
try:
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                          text=True, cwd=PROJECT_ROOT, timeout=15).stdout.strip()
    remote = subprocess.run(["git", "ls-remote", "origin", "main"], capture_output=True,
                            text=True, cwd=PROJECT_ROOT, timeout=20).stdout.split()
    remote_head = remote[0][:7] if remote else "?"
    check("کد با گیت‌هاب همگام است", head == remote_head,
          f"local={head} | origin/main={remote_head}" +
          ("" if head == remote_head else "  →  git pull بزنید!"))
except Exception as exc:
    check("git", False, str(exc), warn_only=True)

# ---------------------------------------------------------------- .env
print("\n🔑 فایل secrets:")
env_path = PROJECT_ROOT / ".env"
env_exists = env_path.exists()
check(".env وجود دارد", env_exists,
      "" if env_exists else "از .env.example کپی کنید و دو توکن را بگذارید")

# ---------------------------------------------------------------- instance 1
print("\n🤖 نمونه ۱ — settings.yaml (ربات اصلی / ORB):")
from core import Settings  # noqa: E402

s1 = Settings.load()
check("active == orb_gold", s1.get("strategy.active") == "orb_gold",
      str(s1.get("strategy.active")))
check("risk_percent == 1.0", float(s1.get("risk.risk_percent", 0)) == 1.0,
      str(s1.get("risk.risk_percent")))
check("max_spread_points == 999", float(s1.get("session.max_spread_points", 0)) == 999,
      str(s1.get("session.max_spread_points")))
check("magic == 553311", int(s1.get("symbol.magic", 0)) == 553311,
      str(s1.get("symbol.magic")))
check("news_filter فعال", bool(s1.get("news_filter.enabled")),
      f"{s1.get('news_filter.currencies')} ±{s1.get('news_filter.pause_minutes_before')}/"
      f"{s1.get('news_filter.pause_minutes_after')}min")
tok1 = str(s1.get("telegram.token", "") or "")
check("توکن نمونه ۱ resolve شد", bool(tok1), mask(tok1))
check("admin_ids تنظیم شده", bool(s1.get("telegram.admin_ids")),
      str(s1.get("telegram.admin_ids")))

# ---------------------------------------------------------------- instance 2
print("\n🤖 نمونه ۲ — settings_ichimoku.yaml (ربات آیچیموکو):")
s2 = Settings.load(PROJECT_ROOT / "config" / "settings_ichimoku.yaml")
check("active == ichimoku_m15", s2.get("strategy.active") == "ichimoku_m15",
      str(s2.get("strategy.active")))
check("magic == 735511", int(s2.get("symbol.magic", 0)) == 735511,
      str(s2.get("symbol.magic")))
check("max_spread_points == 999", float(s2.get("session.max_spread_points", 0)) == 999,
      str(s2.get("session.max_spread_points")))
tok2 = str(s2.get("telegram.token", "") or "")
check("توکن نمونه ۲ resolve شد", bool(tok2), mask(tok2))
check("توکن دو ربات از هم جدا هستند", bool(tok1 and tok2 and tok1 != tok2),
      "⚠️ اگر یکی است، هر دو ربات با یک بات بالا می‌آیند!")
check("دیتابیس‌ها جدا هستند",
      str(s1.get("database.path")) != str(s2.get("database.path")),
      f"{s1.get('database.path')} | {s2.get('database.path')}")

# ---------------------------------------------------------------- strategies
print("\n🧠 استراتژی‌ها:")
from strategies import available_strategies, build_strategy  # noqa: E402

strats = available_strategies()
check("رجیستری = orb_gold + ichimoku_m15",
      strats == ["orb_gold", "ichimoku_m15"], str(strats))
for name in ("orb_gold", "ichimoku_m15"):
    try:
        params = s1.get(f"strategy.params.{name}", {}) or {}
        st = build_strategy(name=name, params=params,
                            atr_period=int(s1.get("risk.atr_period", 14)))
        check(f"build {name}", st.name == name, st.describe()[:60] + "…")
    except Exception as exc:
        check(f"build {name}", False, repr(exc))

# ---------------------------------------------------------------- MT5
print("\n🖥️ متاتریدر ۵:")
try:
    from core.mt5_client import MT5Client, MT5Config

    client = MT5Client(MT5Config.from_settings(s1))
    if client.connect():
        acc = client.account_info()
        tick = client.get_tick()
        check("اتصال ترمینال", True,
              f"login={getattr(acc, 'login', '?')} | server={getattr(acc, 'server', '?')} | "
              f"balance={getattr(acc, 'balance', 0):,.2f}")
        check("تیک نماد طلا", bool(tick and tick.bid > 0),
              f"bid={getattr(tick, 'bid', 0):,.2f} | spread={client.spread_points():.0f}pts")
        try:
            import MetaTrader5 as _mt5

            term = _mt5.terminal_info()
            check("Algo Trading فعال", bool(term and getattr(term, "trade_allowed", False)))
        except Exception:
            pass  # connect log خودش algo_trading را نشان می‌دهد
        client.shutdown()
    else:
        check("اتصال ترمینال", False, "MT5 باز نیست یا لاگین نیست")
except Exception as exc:
    check("MT5", False, repr(exc), warn_only=True)

# ---------------------------------------------------------------- telegram
print("\n📨 اعتبار توکن‌های تلگرام (شبکتی):")
try:
    import requests

    for label, tok in (("bot1 (نمونه ۱)", tok1), ("bot2 (نمونه ۲)", tok2)):
        if not tok:
            check(f"getMe {label}", False, "token خالی", warn_only=True)
            continue
        r = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=6)
        ok = r.ok and bool(r.json().get("ok"))
        username = f"@{r.json()['result']['username']}" if ok else "?"
        check(f"getMe {label}", ok, username)
except Exception as exc:
    check("getMe", False, f"شبکه در دسترس نیست: {exc}"[:80], warn_only=True)

# ---------------------------------------------------------------- misc
print("\n📁 فایل‌های جانبی:")
check("تقویم تاریخی اخبار (بک‌تست)",
      (PROJECT_ROOT / "data" / "historical_news.json").exists(), "", warn_only=True)
check("start_ichimoku.cmd (نمونه ۲)",
      (PROJECT_ROOT / "start_ichimoku.cmd").exists(),
      "اگر نمونه ۲ را با Task Scheduler اجرا می‌کنید، مهم نیست", warn_only=True)

# ---------------------------------------------------------------- summary
failed = [title for title, ok in results if not ok]
print("\n" + "=" * 70)
if not failed:
    print("🟢 ALL OK — همه بررسی‌ها پاس شد. آماده اجراست!")
else:
    print(f"🔴 {len(failed)} مورد مشکل دارد:")
    for title in failed:
        print(f"   ❌ {title}")
print("=" * 70)
sys.exit(0 if not failed else 1)