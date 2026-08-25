#!/usr/bin/env python3
"""Fetch historical high-impact USD economic events for backtesting."""

import json
from datetime import datetime, timezone
from pathlib import Path
import requests

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
OUTPUT_FILE = DATA_DIR / "historical_news.json"

# مخزن دیتای آرشیو تقویم اقتصادی
URL = "https://raw.githubusercontent.com/jmerle/forex-factory-calendar-scraper/master/data/events.json"

def download_and_filter():
    print("در حال دانلود آرشیو تاریخی اخبار اقتصادی...")
    try:
        res = requests.get(URL, timeout=15)
        res.raise_for_status()
        raw_events = res.json()
    except Exception as e:
        print(f"دانلود ناموفق بود: {e}")
        return

    high_impact_usd = []
    for item in raw_events:
        # فیلتر اخبار با تاثیر بالا روی دلار
        currency = str(item.get("currency", item.get("country", ""))).upper()
        impact = str(item.get("impact", "")).lower()
        date_str = str(item.get("date", ""))

        if currency == "USD" and impact == "high" and date_str:
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00")).astimezone(timezone.utc)
                high_impact_usd.append({
                    "title": item.get("title"),
                    "time": dt.isoformat(),
                    "impact": "High",
                    "currency": "USD"
                })
            except Exception:
                continue

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(high_impact_usd, f, indent=2)

    print(f"✅ تعداد {len(high_impact_usd)} رویداد با اهمیت بالا ذخیره شد در: {OUTPUT_FILE}")

if __name__ == "__main__":
    download_and_filter()