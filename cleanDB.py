"""پاک‌سازی تاریخچه سیگنال‌ها قبل از شروع لایو واقعی.

FIX(#8): جدول «trades» در اسکیما وجود ندارد (users/payments/signals) و
نسخه قبلی با خطای «no such table: trades» کرش می‌کرد. جدول درست: signals.
"""
import sqlite3

conn = sqlite3.connect("subscriptions.db")
conn.cursor().execute("DELETE FROM signals")
conn.commit()
conn.close()
print("تاریخچه سیگنال‌ها پاک شد و دیتابیس آماده لایو واقعی است.")