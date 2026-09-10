# 🥇 Gold 3-Strategies Bot

ربات معاملاتی خودکار طلا (XAUUSD) روی **MetaTrader 5** با سه استراتژی تست‌شده، مدیریت ریسک چندلایه، کنترل کامل از طریق تلگرام و موتور بک‌تست صادقانه.

> ⚠️ **سلب مسئولیت:** این پروژه فقط برای آموزش و پژوهش است. معامله با اهرم ریسک بالایی دارد. قبل از هر پول واقعی، حداقل ۱-۳ ماه روی دمو تست کنید.

---

## 📈 استراتژی‌ها

| نام | تایم‌فریم | منطق | مشخصه |
|---|---|---|---|
| `orb_gold` | M5 | **شکست رنج روزانه** — رنج اولیه ۱ ساعت + تأیید ۳ کندل، شکست = BUY/SELL | ~۱ معامله در روز، PF 1.21 (یکساله) |
| `ichimoku_m15` | M15 | **آیچیموکو** — Kijun pullback + Tenkan momentum، سشن نیویورک ۱۲-۲۰ | پارامترهای walk-forward، PF 1.30 |

> ℹ️ `kijun_pullback` دیگر به‌عنوان استراتژی مستقل قابل انتخاب نیست؛ کد آن فقط کلاس پایهٔ `ichimoku_m15` است.

سوییچ استراتژی از تلگرام: `/strategy` → دکمه → ذخیره خودکار در کانفیگ + ری‌استارت خودکار ربات.

---

## 🛡️ محافظ‌های ریسک (همیشه فعال)

| محافظ | پیش‌فرض | توضیح |
|---|---|---|
| محافظ tick_value | خودکار | اگر بروکر tick_value غلط گزارش دهد (مثل دموی MetaQuotes)، اصلاح + هشدار — جلوگیری از لات ۱۰ برابر |
| سقف ضرر روزانه | ۱۰٪ موجودی | رسیدن به سقف → قطع معاملات تا روز بعد + پیام تلگرام |
| سقف معاملات روزانه | ۱۰ | جلوگیری از overtrading |
| فیلتر اسپرد | ۳۰ پوینت | اسپرد بالاتر → معامله نمی‌کند |
| فیلتر اخبار | ۵/۵ دقیقه | توقف قبل/بعد اخبار High-Impact USD — قابل تنظیم در بخش `news_filter` |
| حداقل فاصله SL | ۴۰ پوینت | استاپ‌های غیرمنطقی رد می‌شوند |
| یک پوزیشن همزمان | ۱ | — |

---

## 🖥️ پیش‌نیازها

- **Windows** (MT5 روی لینوکس اجرا نمی‌شود)
- [Python 3.11+](https://www.python.org/downloads/) (تیک Add to PATH)
- ترمینال [MetaTrader 5](https://www.metatrader5.com/) + لاگین اکانت (دمو یا واقعی)
- Git

---

## ⚙️ نصب

```powershell
git clone https://github.com/hellparadox/gold-3strategies.git
cd gold-3strategies
python -m pip install -r requirements.txt
```

### فایل secrets (الزامی — هرگز commit نمی‌شود):

```powershell
Set-Content .env "TELEGRAM_TOKEN=توکن-بات-شما-از-BotFather"
```

| متغیر | کجا لازم است | توضیح |
|---|---|---|
| `TELEGRAM_TOKEN` | نمونه ۱ | از [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_TOKEN_ICHIMOKU` | فقط نمونه ۲ | توکن ربات دوم — با `token_env` در `settings_ichimoku.yaml` خوانده می‌شود (اولویت بالاتر از `TELEGRAM_TOKEN`) |
| `TELEGRAM_PROXY` | فقط سیستم ایران | مثال: `http://127.0.0.1:10808` (v2ray) — روی VPS خارج **ننویسید** |
| `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | اختیاری | اگر ترمینال از قبل لاگین باشد، خالی کافی است |

### ترمینال MT5:
1. لاگین به اکانت (دمو برای تست)
2. دکمه **Algo Trading** سبز باشد
3. نماد XAUUSD در Market Watch باشد

---

## ▶️ اجرا

```powershell
python main_live.py
```

### اجرای خودکار روی VPS (Task Scheduler — به‌صورت Administrator):

```powershell
$python = (Get-Command python).Source
$dir    = (Get-Location).Path
$action = New-ScheduledTaskAction -Execute $python -Argument "main_live.py" -WorkingDirectory $dir
$t1     = New-ScheduledTaskTrigger -AtStartup
$t2     = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$st     = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "GoldBot" -Action $action -Trigger $t1, $t2 -Settings $st -RunLevel Highest -Force
Start-ScheduledTask -TaskName "GoldBot"
```

- ری‌استارت VPS → ربات خودکار بالا می‌آید
- کرش → حداکثر ۵ دقیقه بعد خودش برمی‌گردد
- ری‌استارت دستی: `Stop-ScheduledTask GoldBot` سپس `Start-ScheduledTask GoldBot`
- ⛔ وقتی Task Scheduler فعال است، `main_live.py` را دستی اجرا نکنید (دوبل می‌شود)

---

## 📱 دستورات تلگرام

| دستور | دسترسی | کار |
|---|---|---|
| `/status` | ادمین/همه | ادمین: کارت کامل وضعیت موتور (اتصال MT5، سوییچ‌ها، فیلترها، حساب) — مشترک: وضعیت اشتراک |
| `/price` | همه | قیمت لحظه‌ای |
| `/strategy` | ادمین | نمایش/سوییچ استراتژی (ذخیره دائمی + ری‌استارت خودکار) |
| `/risk` | ادمین | تغییر درصد ریسک |
| `/cooldown` | ادمین | تغییر cooldown |
| `/extension [on\|off] [N]` | ادمین | فیلتر «حرکت کشیده» (anti-chase): جلوی سیگنال تنکان می‌گیرد وقتی قیمت بیش از N×ATR (پیش‌فرض 4) از کف/سقف روز فاصله دارد — «دنبال اتوبوس رفته ن دوید». لحظه‌ای اعمال می‌شود (بدون ری‌استارت) و ذخیره می‌ماند؛ فقط لایه تنکان را می‌بندد |
| `/toggle [on\|off]` | ادمین | توقف/ادامه ربات (kill-switch) — بدون آرگومان = تغییر وضعیت |
| `/close [ticket]` | ادمین | بستن دستی/اضطراری پوزیشن باز — بعدش «سایه» فعال می‌شود و وقتی قیمت به TP یا SL اصلی برسد، نتیجه‌ی نگه‌داشتن را به ادمین می‌گوید |
| `/backtest` | ادمین | بک‌تست استراتژی فعال |
| `/daily [YYYY-MM-DD]` | ادمین | کارت عملکرد روزانه (امروز یا تاریخ دلخواه) |
| `/addvip [uid] [days]` | ادمین | فعال‌سازی اشتراک VIP |
| `/removevip [uid]` | ادمین | لغو VIP |
| `/users` | ادمین | لیست اعضا و درآمد |
| `/start` `/help` | همه | شروع و راهنما |

> شناسه کاربر جدید: مشترک در بات `/start` کند → ادمین `/users` بزند → شناسه در لیست است. (یا مشترک از `@userinfobot` بگیرد)

---

## ✨ قابلیت‌های ویژه

| قابلیت | توضیح |
|---|---|
| **امتیاز کیفیت سیگنال (0-100)** | هر سیگنال بر اساس وین‌ریت تاریخیِ همان ستاپ (استراتژی/جهت/سشن/رژیم ATR) امتیاز می‌گیرد. ساخت آمار: `python tools/precompute_scores.py` — بدون فایل آمار، امتیاز خنثی ۵۰ است |
| **کارت عملکرد روزانه** | بعد از گردش روز UTC به‌صورت خودکار + دستور `/daily` — سود خالص، وین‌ریت، بهترین/بدترین، استریک + کارت تصویری دارک |
| **چارت سینمایی سیگنال** | ناحیه‌ی سود/ضرر رنگی، نردبان R-multiple، بج جهت روی آخرین کندل و بج امتیاز در عنوان |
| **داشبورد زنده وب** | `http://SERVER_IP:8080` — قیمت لحظه‌ای، اکوییتی، پوزیشن‌های باز، تاریخچه‌ی سیگنال‌ها و منحنی رشد (آپدیت ۵ ثانیه، بدون وابستگی خارجی) |

---

## 🔬 بک‌تست

```powershell
python main_backtest.py --bars 75000              # یک سال M5
python main_backtest.py --bars 350000             # ۵ سال کامل
python main_backtest.py --bars 25000 --spread 20  # با اسپرد دلخواه
python main_backtest.py --bars 75000 --output-dir exports/run-001 --save-bars
```

**نکات واقع‌بینی:**
- اسپرد واقعی بروکر خود را از تیک‌ها اندازه بگیرید و در `backtest.spread_points` بگذارید — لبه استراتژی به اسپرد فوق‌حساس است
- خروج پله‌ای (partial) با `--partial` یا `backtest.simulate_partial: true` شبیه‌سازی می‌شود (پارامترهای `partial_tp_rr` / `partial_frac` استراتژی) — قبل از اعتماد به آن در لایو، اثرش را روی PF اندازه بگیرید
- اعداد بک‌تست بدون اسپرد واقعی = داستان، نه داده
- `--output-dir` گزارش JSON، معاملات CSV و خلاصه را با هش داده‌ها ذخیره می‌کند؛ مسیر باید جدید باشد تا گزارش قبلی بازنویسی نشود

### توقف ایمن بعد از وضعیت نامعلوم سفارش

اگر MT5 پاسخ قطعی به ارسال سفارش ندهد، ربات همان سفارش را دوباره نمی‌فرستد و
ورودهای جدید را تا بررسی دستی متوقف می‌کند. ابتدا ربات را متوقف و تب‌های
Positions و History را در MT5 بررسی کنید، سپس دفتر وضعیت را ببینید:

```powershell
python -m tools.execution_journal --config config/settings.yaml
```

فقط بعد از تطبیق با MT5، کلید نمایش‌داده‌شده را تأیید کنید:

```powershell
python -m tools.execution_journal --config config/settings.yaml `
  --acknowledge 'EXACT_SIGNAL_KEY' --broker-reviewed --note 'checked MT5 history'
```

برای نمونهٔ ایچیموکو، همان دستورها را با `config/settings_ichimoku.yaml` اجرا کنید.

---

## ⚙️ کانفیگ (`config/settings.yaml`)

| بخش | مهم‌ترین کلیدها |
|---|---|
| `strategy.active` | استراتژی فعال (`orb_gold` / `ichimoku_m15`) |
| `strategy.params.*` | پارامترهای هر استراتژی |
| `risk` | `risk_percent` (پیشنهاد ۰.۷۵-۱٪)، `max_lot`، سقف ضرر روزانه، SL/TP بر اساس ATR |
| `session` | ساعت معاملاتی، `max_spread_points` |
| `news_filter` | ارزها و دقیقه‌های توقف اطراف اخبار |
| `telegram` | `admin_ids` (توکن از `.env`) |
| `backtest` | `bars`، `spread_points` |

---

## 📂 ساختار

```
├── main_live.py        # موتور اجرای لایو
├── main_backtest.py    # اجرای بک‌تست
├── core/               # اتصال MT5، ریسک، اخبار، تلگرام، اندیکاتور، دیتابیس
├── strategies/         # orb_gold، ichimoku_m15 (+ base و kijun_pullback به‌عنوان پایه)
├── backtest/           # موتور بک‌تست (بدون look-ahead)
├── config/             # settings.yaml
├── tools/              # چک MT5، دانلود اخبار تاریخی
├── data/               # اخبار تاریخی برای بک‌تست
└── .env                # توکن‌ها (commit نمی‌شود!)
```

---

## 🔒 امنیت

- توکن‌ها فقط در `.env` — هرگز در کد یا yaml
- `.env` در `.gitignore` است — push نمی‌شود
- اگر توکنی لو رفت: [@BotFather](https://t.me/BotFather) → `/revoke` → توکن جدید در `.env`

---

## 📜 License

MIT
