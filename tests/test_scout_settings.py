"""تنظیمات از داخل تلگرام: فقط ادمین، با حد پایین/بالا، تأیید برای لات، بدون ری‌استارت،
ذخیره در فایل جدا (YAML دست نمی‌خورد)، پوزیشن‌های باز دست نمی‌خورند."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd

from tests.test_scout_tracker import make_scout
from tools import scout_dashboard as dash
from tools import scout_settings as sset
from tools import scout_tracker as trk


class Pure(unittest.TestCase):
    def test_step_and_limits(self):
        self.assertEqual(sset.step("lot", 0.01, "+"), 0.02)
        self.assertEqual(sset.step("lot", 0.02, "+"), 0.03)              # بدون خطای اعشاری
        self.assertEqual(sset.step("lot", 0.01, "-"), 0.01)              # حداقل
        self.assertEqual(sset.step("lot", 0.10, "+"), 0.10)              # حداکثر
        self.assertEqual(sset.step("max_risk", 20, "+"), 25.0)
        self.assertEqual(sset.step("max_open", 2, "-"), 1)
        self.assertIsInstance(sset.step("max_open", 2, "-"), int)
        self.assertEqual(sset.step("expiry_min", 5, "-"), 5)
        self.assertEqual(sset.step("sl_cap", 0, "+"), 3.0)               # بیرون از بازه → لبه
        self.assertEqual(sset.fmt("lot", 0.02), "0.02")
        self.assertEqual(sset.fmt("sl_cap", 10), "$10")
        self.assertEqual(sset.fmt("expiry_min", 30), "30 دقیقه")

    def test_problems_and_confirm(self):
        self.assertEqual(sset.problems({"sl_cap": 10, "emerg_cap": 20, "max_risk": 20}), [])
        self.assertIn("سقف اضطراری", sset.problems({"sl_cap": 11, "emerg_cap": 10})[0])
        self.assertIn("سقف ریسک کل", sset.problems({"sl_cap": 10, "emerg_cap": 20, "max_risk": 5})[0])
        self.assertTrue(sset.needs_confirm("lot", 0.02, 0.01))           # لات همیشه
        self.assertFalse(sset.needs_confirm("max_risk", 20, 25))
        self.assertTrue(sset.needs_confirm("max_risk", 5, 15))

    def test_sanitize_load_save_diff(self):
        base = {"lot": 0.01, "sl_cap": 10.0, "emerg_cap": 20.0, "max_open": 2, "max_risk": 20.0, "expiry_min": 30}
        self.assertEqual(sset.sanitize({"lot": 0.02, "junk": 5}, base), ({"lot": 0.02}, None))
        self.assertEqual(sset.sanitize({"lot": 0.5}, base)[0], {})                 # بیرون از بازه: هیچ
        self.assertIn("بیرون از بازه", sset.sanitize({"lot": 0.5}, base)[1])
        self.assertEqual(sset.sanitize({"lot": "x"}, base)[0], {})
        self.assertEqual(sset.sanitize({"emerg_cap": 5}, base)[0], {})             # ناسازگار با سقف سیگنال
        self.assertEqual(sset.sanitize([1, 2], base), ({}, "فایل تنظیمات تلگرام خراب است"))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ov.json"
            self.assertEqual(sset.load(p, base), ({}, None))                       # فایل نیست
            p.write_text("{broken", encoding="utf-8")
            self.assertEqual(sset.load(p, base)[0], {})
            self.assertIsNotNone(sset.load(p, base)[1])
            sset.save(p, {"max_open": 3})
            self.assertEqual(sset.load(p, base), ({"max_open": 3}, None))
        self.assertEqual(sset.diff(dict(base, lot=0.02), base), {"lot": 0.02})


def setup_settings(sc, tmp):
    sc.sl_cap_usd, sc.emerg_cap_usd = 10.0, 20.0
    sc.sl_cap, sc.emerg_cap = 10.0, 20.0
    sc.settings_path = tmp / "scout_overrides.json"
    sc._settings_defaults = sc.current_settings()
    sc._settings_mid, sc._settings_ask, sc._settings_warn = None, None, None
    sc.admins = {1}


class Telegram(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tmp = Path(self._d.name)
        self.sc = make_scout(self.tmp)
        setup_settings(self.sc, self.tmp)
        self.sc.settings_show()
        self.mid = self.sc._settings_mid

    def tearDown(self):
        self._d.cleanup()

    def tap(self, data, mid="cur", uid=1):
        m = self.mid if mid == "cur" else mid
        cq = {"from": {"id": uid, "first_name": "Hamid"}, "data": data, "id": "q"}
        if m is not None:
            cq["message"] = {"message_id": m}
        self.sc.handle_update({"callback_query": cq})

    def events(self):
        if not self.sc.journal.exists():
            return []
        with self.sc.journal.open(encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_message_and_buttons(self):
        text, buttons = self.sc.tg.sent[-1]
        self.assertIn("🛠 <b>تنظیمات اسکات</b>", text)
        self.assertIn("لات: <b>0.01</b>", text)
        self.assertIn("سقف ریسک کل: <b>$20</b>", text)
        self.assertEqual(len(buttons), len(sset.SPECS) + 1)
        self.assertEqual([b["callback_data"] for b in buttons[0]], ["g|i|lot", "g|lot|-", "g|lot|+"])
        self.assertEqual([r[0]["text"] for r in buttons[:-1]],
                         ["لات 0.01", "حد ضرر $10", "اضطراری $20", "پوزیشن 2", "ریسک کل $20", "مهلت 30د"])
        self.assertEqual(buttons[-1][0]["callback_data"], "g|reset")

    def test_simple_change_applies_now_and_is_saved(self):
        self.tap("g|max_open|+")
        self.assertEqual(self.sc.max_open, 3)
        self.assertEqual(json.loads(self.sc.settings_path.read_text(encoding="utf-8")), {"max_open": 3})
        self.assertIn("پوزیشن هم‌زمان: 2 → <b>3</b>", self.sc.tg.sent[-1][0])
        self.assertIn("Hamid", self.sc.tg.sent[-1][0])
        self.assertIn("پوزیشن هم‌زمان: <b>3</b>", self.sc.tg.edits[-1][1])
        ev = [e for e in self.events() if e["event"] == "setting"][-1]
        self.assertIn("max_open:2->3", ev["note"])
        self.tap("g|expiry_min|-")
        self.assertEqual(self.sc.expiry, 25 * 60)

    def test_lot_needs_confirm_and_recomputes_caps(self):
        self.sc.open[5] = {"setup": "x", "side": "BUY", "entry": 4100.0, "sl": 4094.0, "risk": 6.0,
                           "emerg": 4080.0, "hold": False}
        self.tap("g|lot|+")
        self.assertEqual(self.sc.lot, 0.01)                                         # هنوز عوض نشده
        mid, text, buttons = self.sc.tg.edits[-1]
        self.assertIn("لات: 0.01 → 0.02", text)
        self.assertEqual(buttons[0][0]["callback_data"], "g|lot|ok|0.02")
        self.tap("g|lot|ok|0.03")                                                   # عدد دیگر (جعلی/قدیمی)
        self.assertEqual(self.sc.lot, 0.01)
        self.tap("g|lot|+")
        self.tap("g|lot|ok|0.02")
        self.assertEqual(self.sc.lot, 0.02)
        self.assertAlmostEqual(self.sc.sl_cap, 5.0)                                 # $10 با لات ۰.۰۲ = ۵ دلار قیمت
        self.assertAlmostEqual(self.sc.emerg_cap, 10.0)
        self.assertEqual(self.sc.open[5]["lot"], 0.01)                               # پوزیشن باز: لات خودش
        self.assertEqual(self.sc.open[5]["sl"], 4094.0)
        self.assertAlmostEqual(self.sc.open_risk_usd(), 6.0)
        self.assertEqual(trk.load_state(self.sc.state_path)[5]["lot"], 0.01)

    def test_back_cancels_question(self):
        self.tap("g|lot|+")
        self.tap("g|back")
        self.tap("g|lot|ok|0.02")
        self.assertEqual(self.sc.lot, 0.01)

    def test_limits_and_consistency(self):
        self.tap("g|lot|-")
        self.assertIn("حداقل", self.sc.tg.acks[-1])
        self.sc.emerg_cap_usd = 10.0
        self.tap("g|sl_cap|+")
        self.assertIn("سقف اضطراری", self.sc.tg.acks[-1])
        self.assertEqual(self.sc.sl_cap_usd, 10.0)

    def test_old_message_and_non_admin_refused(self):
        self.tap("g|max_open|+", mid=self.mid + 50)
        self.assertEqual(self.sc.max_open, 2)
        self.assertIn("قدیمی", self.sc.tg.acks[-1])
        self.tap("g|max_open|+", uid=9)
        self.assertEqual(self.sc.max_open, 2)
        self.sc.settings_show()                                                     # پیام قبلی پاک می‌شود
        self.assertIn(self.mid, self.sc.tg.deleted)

    def test_reset(self):
        self.tap("g|reset")
        self.assertIn("همین الان همه پیش‌فرض است", self.sc.tg.acks[-1])
        self.tap("g|max_risk|+")
        self.tap("g|max_open|-")
        self.tap("g|reset|ok")                                                      # بدون سؤال قبلی: معتبر نیست
        self.assertEqual(self.sc.max_open, 1)
        self.tap("g|reset")
        self.assertIn("همه به پیش‌فرض برگردد؟", self.sc.tg.edits[-1][1])
        self.tap("g|reset|ok")
        self.assertEqual((self.sc.max_open, self.sc.max_total_risk), (2, 20.0))
        self.assertEqual(json.loads(self.sc.settings_path.read_text(encoding="utf-8")), {})
        self.assertIn("تنظیمات به پیش‌فرض برگشت", self.sc.tg.sent[-1][0])

    def test_close_and_info(self):
        self.tap("g|i|lot")
        self.assertIn("بین 0.01 و 0.10", self.sc.tg.acks[-1])
        self.tap("g|close")
        self.assertIn(self.mid, self.sc.tg.deleted)
        self.assertIsNone(self.sc._settings_mid)

    def test_menu_and_command(self):
        self.assertIn("k|settings", str(self.sc.MENU_BUTTONS))
        self.sc.handle_update({"message": {"from": {"id": 1}, "text": "/settings"}})
        self.assertIn("🛠 <b>تنظیمات اسکات</b>", self.sc.tg.sent[-1][0])


class Persistence(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tmp = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_overrides_survive_restart(self):
        sc = make_scout(self.tmp)
        setup_settings(sc, self.tmp)
        sset.save(sc.settings_path, {"lot": 0.02, "max_risk": 30.0})
        sc2 = make_scout(self.tmp)                                                  # «ری‌استارت»
        setup_settings(sc2, self.tmp)
        ov, warn = sset.load(sc2.settings_path, sc2._settings_defaults)
        sc2._set_values(ov)
        self.assertEqual((sc2.lot, sc2.max_total_risk, warn), (0.02, 30.0, None))
        self.assertAlmostEqual(sc2.sl_cap, 5.0)
        self.assertIn("تنظیمات تلگرام فعال", sc2._settings_note())
        self.assertIn("لات 0.02", sc2._settings_note())

    def test_broken_file_keeps_defaults_and_warns(self):
        sc = make_scout(self.tmp)
        setup_settings(sc, self.tmp)
        sc.settings_path.write_text('{"lot": 9}', encoding="utf-8")
        ov, sc._settings_warn = sset.load(sc.settings_path, sc._settings_defaults)
        self.assertEqual(ov, {})
        self.assertEqual(sc.lot, 0.01)
        self.assertIn("پیش‌فرض‌های فایل YAML", sc._settings_note())

    def test_restore_keeps_levels_of_positions_with_lot(self):
        trk.save_state(self.tmp / "scout_state.json",
                       {7: {"setup": "tk_cross", "side": "SELL", "entry": 4171.23, "sl": 4187.06,
                            "emerg": 4218.57, "risk": 15.83, "hold": True, "lot": 0.01}})
        sc = make_scout(self.tmp)
        setup_settings(sc, self.tmp)
        sc.client.positions.return_value = [NS(ticket=7, type=1, price_open=4171.23, sl=4218.57, volume=0.01,
                                               price_current=4170.0, profit=0.0, swap=0.0, time=0,
                                               comment="scout_tk", magic=735777)]
        sc.restore()
        self.assertEqual((sc.open[7]["sl"], sc.open[7]["emerg"]), (4187.06, 4218.57))   # دست نخورد


class NewPositionsAndStats(unittest.TestCase):
    def test_new_position_records_lot(self):
        with tempfile.TemporaryDirectory() as d:
            sc = make_scout(Path(d))
            setup_settings(sc, Path(d))
            sc._set_values({"lot": 0.02})
            sc.client.get_tick.return_value = NS(time=0, bid=4099.8, ask=4100.1)
            sc.client.positions.return_value = []
            sc.client.send_market_order.return_value = NS(ok=True, position=77, order=77, deal=0,
                                                          price=4100.1, comment="")
            r = pd.Series({"setup": "rejection", "side": "BUY", "ref_close": 4100.0, "sl_dist": 4.0,
                           "tp_dist": 8.0, "atr": 5.0, "bar": pd.Timestamp("2026-10-06 09:15")})
            sc.alert(r, 27.0)
            sc.handle_update({"callback_query": {"from": {"id": 1}, "data": "a|1|y", "id": "q"}})
            self.assertEqual(sc.client.send_market_order.call_args.args[1], 0.02)
            self.assertEqual(sc.open[77]["lot"], 0.02)
            with sc.journal.open(encoding="utf-8") as fh:
                opened = [e for e in csv.DictReader(fh) if e["event"] == "opened"][-1]
            self.assertIn("lot=0.02", opened["note"])

    def _scout_with_alert(self, d):
        sc = make_scout(Path(d))
        setup_settings(sc, Path(d))
        sc.client.get_tick.return_value = NS(time=0, bid=4099.8, ask=4100.1)
        sc.client.positions.return_value = []
        sc.client.send_market_order.return_value = NS(ok=True, position=78, order=78, deal=0,
                                                      price=4100.1, comment="", volume=0.01)
        r = pd.Series({"setup": "rejection", "side": "BUY", "ref_close": 4100.0, "sl_dist": 10.0,
                       "tp_dist": 20.0, "atr": 5.0, "bar": pd.Timestamp("2026-10-06 09:15")})
        sc.alert(r, 27.0)
        return sc

    def test_pending_alert_keeps_its_lot(self):
        with tempfile.TemporaryDirectory() as d:
            sc = self._scout_with_alert(d)                                          # هشدار با لات ۰.۰۱ و ریسک $10
            sc._set_values({"lot": 0.05, "sl_cap": 30.0, "emerg_cap": 50.0, "max_risk": 50.0})
            sc.handle_update({"callback_query": {"from": {"id": 1}, "data": "a|1|y", "id": "q"}})
            args = sc.client.send_market_order.call_args
            self.assertEqual(args.args[1], 0.01)                                    # همان لات هشدار، نه ۰.۰۵
            self.assertEqual(sc.open[78]["lot"], 0.01)
            self.assertAlmostEqual(sc.open_risk_usd(), 10.0, places=1)
            # اضطراری با سقف ۵۰$ برای لات ۰.۰۱ (۵۰ دلار قیمت) ولی حداکثر ۳ برابر حد ضرر
            self.assertAlmostEqual(abs(sc.open[78]["entry"] - sc.open[78]["emerg"]), 30.0, places=1)

    def test_shorter_expiry_does_not_kill_sent_alerts(self):
        with tempfile.TemporaryDirectory() as d:
            sc = self._scout_with_alert(d)
            sc.pending["1"]["t"] -= 10 * 60                                         # ۱۰ دقیقه پیش آمده
            sc._set_values({"expiry_min": 5})
            sc.expire()
            self.assertIn("1", sc.pending)                                          # مهلت خودش (۳۰ دقیقه)
            sc.pending["1"]["t"] -= 25 * 60
            sc.expire()
            self.assertNotIn("1", sc.pending)

    def test_min_stop_distance_rule(self):
        self.assertIn("حداقل", sset.problems({"lot": 0.06, "sl_cap": 10, "emerg_cap": 20})[0])
        self.assertEqual(sset.problems({"lot": 0.05, "sl_cap": 10, "emerg_cap": 20, "max_risk": 20}), [])

    def test_forged_values(self):
        with tempfile.TemporaryDirectory() as d:
            sc = make_scout(Path(d))
            setup_settings(sc, Path(d))
            for data in ("g|max_open|ok|inf", "g|lot|ok|nan", "g|lot|ok|", "g|nope|+", "g|lot|x"):
                sc.handle_update({"callback_query": {"from": {"id": 1}, "data": data, "id": "q"}})
            self.assertEqual((sc.lot, sc.max_open), (0.01, 2))

    def test_dashboard_r_uses_trade_lot(self):
        ev = [{"event": "opened", "ticket": "1", "price": "4100", "sl": "4095", "note": "x=1;lot=0.02",
               "ts": "2026-10-06T10:00:00"},
              {"event": "gone", "ticket": "1", "profit": "10", "ts": "2026-10-06T11:00:00"},
              {"event": "opened", "ticket": "2", "price": "4100", "sl": "4095", "note": "",
               "ts": "2026-10-06T10:00:00"},
              {"event": "gone", "ticket": "2", "profit": "5", "ts": "2026-10-06T11:00:00"}]
        rows = dash.scout_trades(ev, 0.01)
        self.assertEqual([x["r"] for x in rows], [1.0, 1.0])                        # $10 با ۰.۰۲ = 1R


if __name__ == "__main__":
    unittest.main()
