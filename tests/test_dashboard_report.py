"""Report tab of the web dashboard: log translation, statistics, event feed."""
import json
import os
import sys
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger  # noqa: E402

from core.dashboard import (  # noqa: E402
    DashboardServer, _log_glob, build_report, parse_log_lines, read_log_files, translate_log_event,
)

NOW = datetime(2026, 9, 25, 10, 0, 0)          # UTC -> 13:30 Tehran
START = NOW - timedelta(hours=5)


def row(ticket, side, opened, closed, entry, close, sl, tp, profit, reason="Kijun pullback"):
    return {"ticket": ticket, "side": side, "strategy": "ichimoku_m15", "reason": reason,
            "entry": entry, "sl": sl, "tp": tp,
            "created_at": opened.strftime("%Y-%m-%d %H:%M:%S"),
            "closed_at": closed.strftime("%Y-%m-%d %H:%M:%S") if closed else None,
            "close_price": close, "profit": profit}


ROWS = [
    # today (Tehran): TP win, SL loss
    row(1, "BUY", NOW - timedelta(hours=3), NOW - timedelta(hours=2), 4300, 4330, 4284, 4330, 29.7),
    row(2, "SELL", NOW - timedelta(hours=2), NOW - timedelta(hours=1), 4320, 4336, 4336, 4290, -16.3,
        reason="Tenkan momentum"),
    # 21:00 UTC yesterday = 00:30 Tehran today -> counts as today in Tehran
    row(3, "SELL", NOW - timedelta(hours=14), NOW - timedelta(hours=13), 4310, 4310.05, 4325, 4280, 0.05),
    # 5 days ago: trailing exit with profit
    row(4, "BUY", NOW - timedelta(days=5, hours=2), NOW - timedelta(days=5), 4200, 4209, 4185, 4245, 8.9),
    # still open
    row(5, "BUY", NOW - timedelta(minutes=30), None, 4340, None, 4325, 4385, None),
]


class TranslateTests(unittest.TestCase):
    def test_signal(self):
        ev = translate_log_event("🎯 سیگنال شناسایی شد: 2026-09-25 11:45:00 → SELL (Tenkan momentum)", "INFO")
        self.assertEqual(ev["kind"], "signal")
        self.assertIn("فروش", ev["text"])
        self.assertIn("Tenkan momentum", ev["text"])

    def test_news_before_after(self):
        ev = translate_log_event("📰 بلاک خبری فعال — ورود جدید ممنوع | ⚠️ 5 mins before High-Impact USD: "
                                 "Unemployment Claims", "INFO")
        self.assertEqual(ev["kind"], "news")
        self.assertIn("5 دقیقه مانده به خبر Unemployment Claims", ev["text"])
        ev = translate_log_event("📰 بلاک خبری فعال — ورود جدید ممنوع | ⚠️ 0 mins after Unemployment Claims "
                                 "(Volatility Cooldown)", "INFO")
        self.assertIn("0 دقیقه بعد از خبر Unemployment Claims", ev["text"])

    def test_guard_close_manage(self):
        self.assertEqual(translate_log_event("🛑 risk guard: min-lot would force ... SKIPPING entry",
                                             "WARNING")["kind"], "guard")
        self.assertEqual(translate_log_event("DAILY GUARD ACTIVE -> loss cap | no new entries", "WARNING")["kind"],
                         "guard")
        ev = translate_log_event("position #77 closed | LOSS | $-12.90", "SUCCESS")
        self.assertEqual(ev["kind"], "close")
        self.assertIn("#77", ev["text"])
        self.assertIn("12.90", ev["text"])
        ev = translate_log_event("breakeven #77 SL 4295.59 -> 4282.71", "SUCCESS")
        self.assertIn("سربه‌سر", ev["text"])

    def test_ignored_and_errors(self):
        self.assertIsNone(translate_log_event("closure #5 awaiting complete broker deal history", "WARNING"))
        self.assertIsNone(translate_log_event("signal score 55/100 | x", "INFO"))
        self.assertEqual(translate_log_event("something broke", "ERROR")["kind"], "error")
        # the "all" view keeps unknown lines, as raw text
        self.assertEqual(translate_log_event("closure #5 awaiting complete broker deal history", "WARNING",
                                             keep_raw=True)["kind"], "warn")
        self.assertEqual(translate_log_event("signal score 55/100 | x", "INFO", keep_raw=True)["kind"], "system")

    def test_broker_and_orders(self):
        ev = translate_log_event("MT5 connected | login=1 server=X balance=398.10 USD | symbol=XAUUSD@", "SUCCESS")
        self.assertEqual(ev["kind"], "conn")
        self.assertIn("398.10", ev["text"])
        ev = translate_log_event("SELL 0.01 lots @ 4282.76 | sl=4295.59 tp=4253.14 ticket=109296030", "SUCCESS")
        self.assertEqual(ev["kind"], "open")
        self.assertIn("فروش", ev["text"])
        self.assertIn("#109296030", ev["text"])
        self.assertEqual(translate_log_event("heartbeat: link LOST", "ERROR")["kind"], "error")
        self.assertEqual(translate_log_event("order rejected: (10019) No money", "ERROR")["kind"], "error")

    def test_news_calendar_is_technical(self):
        ev = translate_log_event("calendar fetch failed (HTTP 429); falling back to historical file", "WARNING")
        self.assertEqual(ev["kind"], "system")
        self.assertIsNone(translate_log_event("loaded 162 matching events from historical calendar x", "INFO"))

    def test_token_never_shown(self):
        self.assertIsNone(translate_log_event("📊 dashboard online → http://0.0.0.0:8081/?token=SECRET", "SUCCESS"))
        ev = translate_log_event("📊 dashboard online → http://0.0.0.0:8081/?token=SECRET", "SUCCESS",
                                 keep_raw=True)
        self.assertNotIn("SECRET", ev["text"])
        ev = translate_log_event("weird thing token=SECRET", "ERROR")
        self.assertNotIn("SECRET", ev["text"])


class BuildReportTests(unittest.TestCase):
    def setUp(self):
        tel = {"connected": True, "engine_running": True, "positions": []}
        self.rep = build_report(ROWS, [], tel, NOW, START)

    def test_periods(self):
        p = {x["key"]: x for x in self.rep["periods"]}
        self.assertEqual(p["today"]["trades"], 3)                     # includes 00:30 Tehran trade
        self.assertEqual((p["today"]["wins"], p["today"]["losses"], p["today"]["be"]), (1, 1, 1))
        self.assertAlmostEqual(p["today"]["net"], 13.45, places=2)
        self.assertEqual(p["today"]["win_rate"], 50.0)               # break-even excluded
        self.assertEqual(p["7d"]["trades"], 4)
        self.assertEqual(p["all"]["trades"], 4)                      # open trade not counted
        self.assertAlmostEqual(p["all"]["net"], 22.35, places=2)

    def test_exit_kinds_and_order(self):
        t = self.rep["trades"]
        self.assertEqual([x["ticket"] for x in t], [2, 1, 3, 4])      # newest first
        kinds = {x["ticket"]: x["exit"] for x in t}
        self.assertEqual(kinds[1], "حد سود")
        self.assertEqual(kinds[2], "حد ضرر")
        self.assertEqual(kinds[3], "سربه‌سر")
        self.assertEqual(kinds[4], "تریل با سود")
        self.assertEqual(len(self.rep["open"]), 1)

    def test_days_and_cumulative(self):
        days = self.rep["days"]
        self.assertEqual(days[0]["date"], "2026-09-25")
        self.assertEqual(days[0]["trades"], 3)
        self.assertAlmostEqual(days[0]["cum"], 22.35, places=2)
        self.assertTrue(all(d["weekday"] for d in days))

    def test_groups(self):
        names = {g["name"]: g for g in self.rep["groups"]}
        self.assertIn("خرید — Kijun pullback", names)
        self.assertEqual(names["فروش — Tenkan momentum"]["trades"], 1)

    def test_headline(self):
        text = "\n".join(self.rep["headline"])
        self.assertIn("روشن است", text)
        self.assertIn("امروز 3 معامله", text)
        self.assertIn("0 سیگنال", text)
        self.assertIn("شرایط ورود استراتژی پیش نیامده", text)

    def test_disconnected_and_open_position(self):
        tel = {"connected": False, "positions": [{"side": "BUY", "price_open": 4340, "profit": -2.5}]}
        text = "\n".join(build_report(ROWS, [], tel, NOW, START)["headline"])
        self.assertIn("قطع", text)
        self.assertIn("پوزیشن باز: خرید", text)

    def test_empty_database(self):
        rep = build_report([], [], {"connected": True}, NOW, START)
        self.assertEqual(rep["periods"][3]["trades"], 0)
        self.assertEqual(rep["trades"], [])


def _stamp(utc: datetime) -> str:
    """Log time stamp as loguru writes it: this machine's local time."""
    local = utc.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return local.strftime("%Y-%m-%d %H:%M:%S.123")


def _line(utc, level, name, msg, thread="MainThread"):
    return f"{_stamp(utc)} | {level:<8} | {thread} | {name}:fn:1 - {msg}"


T0 = datetime(2026, 9, 24, 12, 25, 1)      # UTC
LOG = [
    _line(T0 - timedelta(hours=2), "SUCCESS", "core.mt5_client",
          "MT5 connected | login=20277252 server=WMMarkets-Demo balance=398.10 USD | symbol=XAUUSD@"),
    _line(T0 - timedelta(hours=2), "SUCCESS", "core.dashboard", "📊 dashboard online → http://0.0.0.0:8081/?token=SECRET"),
    _line(T0 - timedelta(hours=1), "WARNING", "core.news_filter",
          "calendar fetch failed (HTTP 429); falling back to historical file", thread="news-filter-refresh"),
    _line(T0, "INFO", "__main__", "📰 بلاک خبری فعال — ورود جدید ممنوع | ⚠️ 5 mins before High-Impact USD: "
          "Unemployment Claims"),
    _line(T0 + timedelta(minutes=5), "INFO", "__main__", "📰 بلاک خبری فعال — ورود جدید ممنوع | ⚠️ 5 mins "
          "before High-Impact USD: Unemployment Claims"),                    # repeat within 15 min -> dropped
    "Traceback (most recent call last):",                                      # continuation line -> skipped
    _line(T0 + timedelta(minutes=30), "INFO", "__main__",
          "🎯 سیگنال شناسایی شد: 2026-09-24 15:45:00 → BUY (Kijun pullback)"),
]


class LogFileTests(unittest.TestCase):
    def test_parse_important(self):
        items = parse_log_lines(LOG, "important")
        self.assertEqual([i["kind"] for i in items], ["conn", "news", "signal"])
        self.assertEqual(items[1]["time"], "15:55")             # 12:25 UTC -> 15:55 Tehran
        self.assertEqual(items[1]["day"], "2026-09-24")
        self.assertEqual(items[1]["weekday"], "پنجشنبه")

    def test_parse_all_hides_token(self):
        items = parse_log_lines(LOG, "all")
        self.assertIn("system", [i["kind"] for i in items])
        self.assertFalse(any("SECRET" in i["text"] for i in items))

    def test_glob_and_read_files(self):
        self.assertTrue(_log_glob("logs/ichimoku_{time:YYYY-MM-DD}.log").endswith(
            os.path.join("logs", "ichimoku_*.log")))
        self.assertIsNone(_log_glob(None))
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "ichimoku_2026-09-24.log"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(LOG) + "\n")
            with open(os.path.join(tmp, "bot_2026-09-24.log"), "w", encoding="utf-8") as fh:
                fh.write(_line(T0, "INFO", "__main__", "GOLD M5 BOT starting | other bot") + "\n")
            rep = read_log_files(_log_glob(os.path.join(tmp, "ichimoku_{time:YYYY-MM-DD}.log")))
            self.assertTrue(rep["ok"])
            self.assertEqual(rep["files"], ["ichimoku_2026-09-24.log"])     # other bot's log not mixed in
            self.assertEqual(rep["items"][0]["kind"], "signal")               # newest first
            self.assertNotIn("ts", rep["items"][0])
            missing = read_log_files(os.path.join(tmp, "nothing_*.log"))
            self.assertFalse(missing["ok"])
        self.assertFalse(read_log_files(None)["ok"])


class _DB:
    def recent_signals(self, limit=10):
        return list(reversed(ROWS))[:limit]

    def signal_performance(self):
        return {}

    def stats(self):
        return {}


class ServerTests(unittest.TestCase):
    def setUp(self):
        import core.dashboard as dash
        self._saved = dash._LOG_SOURCES
        dash._LOG_SOURCES = dash._LOG_SOURCES + (__name__,)   # accept log lines from this test module

    def tearDown(self):
        import core.dashboard as dash
        dash._LOG_SOURCES = self._saved

    def test_event_feed_and_endpoint(self):
        server = DashboardServer(lambda: {"connected": True, "positions": []}, _DB(),
                                 host="127.0.0.1", port=0, token="tok")
        self.assertTrue(server.start())
        try:
            port = server._httpd.server_address[1]
            logger.info("🎯 سیگنال شناسایی شد: {} → {} ({})", "2026-09-25 11:45:00", "BUY", "Kijun pullback")
            for _ in range(3):   # repeated news lines collapse into one event
                logger.info("📰 بلاک خبری فعال — ورود جدید ممنوع | ⚠️ 5 mins before High-Impact USD: CPI")
            logger.info("signal score 50/100 | ignored")
            kinds = [e["kind"] for e in server._events]
            self.assertEqual(kinds, ["signal", "news"])

            base = f"http://127.0.0.1:{port}"
            with self.assertRaises(Exception):
                urllib.request.urlopen(base + "/api/report")                  # no token -> 403
            rep = json.loads(urllib.request.urlopen(base + "/api/report?token=tok").read().decode("utf-8"))
            self.assertIn("headline", rep)
            self.assertEqual(len(rep["events"]), 2)
            log = json.loads(urllib.request.urlopen(base + "/api/log?token=tok&view=all").read().decode("utf-8"))
            self.assertFalse(log["ok"])                                        # no log_path given
            page = urllib.request.urlopen(base + "/?token=tok").read().decode("utf-8")
            self.assertIn('id="tab-report"', page)
            self.assertIn('id="tab-log"', page)
            self.assertIn("/api/report", page)
        finally:
            server.stop()
        logger.info("🎯 سیگنال شناسایی شد: x → SELL (y)")        # sink removed after stop
        self.assertEqual(len(server._events), 2)


if __name__ == "__main__":
    unittest.main()
