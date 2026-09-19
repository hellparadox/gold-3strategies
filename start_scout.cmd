@echo off
REM اسکات — دیدبان ستاپ‌ها با تأیید دستی در تلگرام.
REM توکن را اینجا ننویسید؛ از .env خوانده می‌شود (TELEGRAM_TOKEN_SCOUT).
cd /d C:\Users\Administrator\gold-3strategies
py -3.11 tools\scout_bot.py --config config\settings_scout.yaml
