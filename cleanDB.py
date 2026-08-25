import sqlite3

conn = sqlite3.connect("subscriptions.db")
conn.cursor().execute("DELETE FROM trades")
conn.commit()
conn.close()
print("تاریخچه تستی معاملات پاک شد و دیتابیس آماده لایو واقعی است.")