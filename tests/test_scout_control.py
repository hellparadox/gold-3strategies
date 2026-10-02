"""Scout control panel: admin-only, price/status/pause/restart, feed watchdog, single instance."""
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pandas as pd

from tools import scout_control as ctl
from tests.test_scout_tracker import FakeTG, make_scout

ADMIN = 84078462


def cb(data, who=ADMIN):
    return {"callback_query": {"id": "q", "data": data, "from": {"id": who}}}


def msg(text, who=ADMIN):
    return {"message": {"text": text, "from": {"id": who}, "chat": {"id": who}}}


def scout(tmp: Path):
    sc = make_scout(tmp)
    sc.admins = {ADMIN}
    sc.flags_path = tmp / "flags.json"
    sc.paused = False
    sc.started = 0.0
    sc.health_hour = 5
    sc.stale_after = 600.0
    sc._last_data_ok = 0.0
    sc._feed_alert = False
    sc._health_day = None
    sc._last_alert_t = None
    sc.lock = None
    sc.client.symbol = "XAUUSD@"
    sc.client.get_tick.return_value = NS(time=1_790_000_000, bid=4170.10, ask=4170.37)
    sc.client.spread_points.return_value = 27.0
    sc.client.server_time.return_value = datetime.utcfromtimestamp(1_790_000_005)
    sc.client.get_rates.return_value = pd.DataFrame({"open": [4150.0], "high": [4180.0], "low": [4140.0],
                                                     "close": [4170.0]})
    sc.client.is_connected = True                  # property on the real MT5Client
    sc.client.account_info.return_value = NS(login=20277252, balance=426.01, equity=420.5)
    return sc


class Panel(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tmp = Path(self._d.name)
        self.sc = scout(self.tmp)

    def tearDown(self):
        self._d.cleanup()

    def test_non_admin_is_ignored(self):
        self.sc.handle_update(msg("/close 5", who=999))
        self.sc.handle_update(cb("c|5", who=999))
        self.sc.client.close_position.assert_not_called()
        self.assertEqual(self.sc.tg.sent, [])

    def test_menu_and_price(self):
        self.sc.handle_update(msg("/menu"))
        self.assertIn("پنل کنترل", self.sc.tg.sent[-1][0])
        self.assertEqual(self.sc.tg.sent[-1][1], self.sc.MENU_BUTTONS)
        self.sc.handle_update(cb("k|price"))
        t = self.sc.tg.sent[-1][0]
        for part in ("4170.37", "4170.10", "27 پوینت", "تغییر +20.10"):
            self.assertIn(part, t)

    def test_bottom_keyboard_buttons(self):
        self.sc.handle_update(msg("/menu"))
        self.assertEqual(self.sc.tg.keyboards[-1], self.sc.KEYBOARD)
        self.assertTrue(self.sc.KEYBOARD["is_persistent"])
        self.sc.open[5] = {"setup": "tk_cross", "side": "SELL", "entry": 4171.23, "sl": 4181.23,
                           "emerg": 4191.23, "risk": 10.0, "hold": True}
        self.sc.client.positions.return_value = [NS(ticket=5, price_current=4180.0, profit=-8.77, magic=735777)]
        self.sc.handle_update(msg("📋 پوزیشن‌ها"))
        text, buttons = self.sc.tg.sent[-1]
        for part in ("#5", "4180.00", "-8.77$", "نگه داشته شده"):
            self.assertIn(part, text)
        self.assertEqual([b["callback_data"] for b in buttons[0]], ["c|5", "u|5"])
        self.sc.handle_update(msg("💰 قیمت"))
        self.assertIn("4170.37", self.sc.tg.sent[-1][0])
        self.sc.handle_update(msg("📋 پوزیشن‌ها", who=999))      # still admin-only
        self.assertIn("4170.37", self.sc.tg.sent[-1][0])

    def test_status(self):
        self.sc.handle_update(msg("/status"))
        t = self.sc.tg.sent[-1][0]
        for part in ("اسکات سالم است", "MT5: وصل", "426.01", "۲۴ ساعت اخیر"):
            self.assertIn(part, t)

    def test_status_with_real_client_property(self):
        """Regression 2026-10-02: is_connected is a property; calling it showed 'قطع' while connected."""
        from core.mt5_client import MT5Client
        self.assertIsInstance(MT5Client.__dict__["is_connected"], property)
        self.sc.client.is_connected = False
        self.sc.handle_update(msg("/status"))
        self.assertIn("MT5: قطع", self.sc.tg.sent[-1][0])

    def test_pause_resume_persisted_and_alerts_silenced(self):
        self.sc.pending["1"] = {"row": NS(setup="x", side="BUY"), "mid": 1, "txt": "t", "t": 0}
        self.sc.handle_update(msg("/pause"))
        self.assertTrue(self.sc.paused)
        self.assertTrue(ctl.load_flags(self.sc.flags_path)["paused"])
        self.assertEqual(self.sc.pending, {})                       # unanswered alerts cancelled
        self.sc.handle_update(cb("k|resume"))
        self.assertFalse(ctl.load_flags(self.sc.flags_path)["paused"])

    def test_restart_needs_confirmation_and_relaunches(self):
        self.sc.handle_update(cb("k|restart"))
        self.assertIn("ری‌استارت اسکات؟", self.sc.tg.sent[-1][0])
        with unittest.mock.patch("tools.scout_bot.subprocess.Popen") as popen, \
                unittest.mock.patch("tools.scout_bot.os._exit") as ex:
            self.sc.handle_update(cb("k|restart_no"))
            popen.assert_not_called()
            self.sc.tg._offset = 42
            self.sc.tg._call = Mock()
            self.sc.handle_update(cb("k|restart_yes"))
            popen.assert_called_once()
            self.sc.tg._call.assert_any_call("getUpdates", offset=42, timeout=0)   # no restart loop
            self.assertIn("scout_bot.py", popen.call_args.args[0][1])
            ex.assert_called_once_with(0)

    def test_feed_watchdog_alerts_once_and_recovers(self):
        t0 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc).timestamp()   # Thursday, market open
        self.sc._last_data_ok = t0
        self.sc._health_day = "2026-10-01"
        self.sc.health_tick(False, now=t0 + 300)
        self.assertEqual(self.sc.tg.sent, [])
        self.sc.health_tick(False, now=t0 + 700)
        self.sc.health_tick(False, now=t0 + 900)
        self.assertEqual(sum("نمی‌رسد" in s[0] for s in self.sc.tg.sent), 1)
        self.sc.client.server_time.return_value = datetime.utcfromtimestamp(1_790_000_005)
        self.sc.health_tick(True, now=t0 + 1000)
        self.assertIn("برگشت", self.sc.tg.sent[-1][0])

    def test_daily_health_message(self):
        day = datetime(2026, 10, 2, 5, 1, tzinfo=timezone.utc).timestamp()
        self.sc._last_data_ok = day
        self.sc.health_tick(True, now=day)
        self.assertIn("اسکات سالم است", self.sc.tg.sent[-1][0])
        n = len(self.sc.tg.sent)
        self.sc.health_tick(True, now=day + 3600)
        self.assertEqual(len(self.sc.tg.sent), n)                   # once per day

    def test_late_start_does_not_send_daily(self):
        late = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc).timestamp()
        self.sc._last_data_ok = late
        self.sc.health_tick(True, now=late)
        self.assertEqual(self.sc.tg.sent, [])


class Pure(unittest.TestCase):
    def test_market_hours(self):
        f = lambda *a: ctl.market_open_utc(datetime(*a, tzinfo=timezone.utc))
        self.assertTrue(f(2026, 10, 1, 10))        # Thu
        self.assertFalse(f(2026, 10, 1, 21))       # daily break
        self.assertFalse(f(2026, 10, 3, 12))       # Sat
        self.assertFalse(f(2026, 10, 4, 20))       # Sun before open
        self.assertTrue(f(2026, 10, 4, 22))        # Sun open
        self.assertFalse(f(2026, 10, 2, 21))       # Fri close

    def test_journal_summary(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "j.csv"
            p.write_text("ts,event,profit\n2026-10-01T10:00:00,alert,\n2026-10-01T10:05:00,opened,\n"
                         "2026-10-01T11:00:00,gone,5.5\n2026-10-01T12:00:00,gone,-2\n"
                         "2026-09-01T12:00:00,gone,-99\n", encoding="utf-8")
            js = ctl.journal_summary(p, datetime(2026, 10, 1))
            self.assertEqual((js["alerts"], js["opened"], js["closed"], js["wins"], js["profit"]),
                             (1, 1, 2, 1, 3.5))

    def test_instance_lock_is_exclusive_across_processes(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "scout.lock"
            lock = ctl.InstanceLock(path)
            self.assertTrue(lock.acquire())
            code = ("import sys;sys.path.insert(0,%r);from tools.scout_control import InstanceLock;"
                    "from pathlib import Path;print(InstanceLock(Path(%r)).acquire(wait=0.5))"
                    % (os.getcwd(), str(path)))
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
            self.assertEqual(out.stdout.strip(), "False", out.stderr)
            lock.release()
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
            self.assertEqual(out.stdout.strip(), "True", out.stderr)


if __name__ == "__main__":
    unittest.main()
