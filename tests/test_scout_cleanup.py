"""سؤال‌های یک‌باره بعد از جواب بی‌دکمه می‌شوند: ری‌استارت («بله» = یک خط سابقه، «نه» = پاک)، منوی حالت حد ضرر
و سؤال «بیشتر از سقف» قیمت خروج. دکمهٔ سؤال قدیمی دیگر کاری نمی‌کند (مثلاً ری‌استارت دوباره)."""
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace as NS

from tests.test_scout_control import ADMIN, scout
from tests.test_scout_exits import EMERG, held, live


def tap(data, mid=None, who=ADMIN, name="Hamid"):
    cq = {"id": "q", "data": data, "from": {"id": who, "first_name": name}}
    if mid is not None:
        cq["message"] = {"message_id": mid}
    return {"callback_query": cq}


class Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = scout(Path(self._d.name))
        self.popen = unittest.mock.patch("tools.scout_bot.subprocess.Popen").start()
        self.exit = unittest.mock.patch("tools.scout_bot.os._exit").start()

    def tearDown(self):
        unittest.mock.patch.stopall()
        self._d.cleanup()

    def ask_restart(self):
        self.sc.handle_update(tap("k|restart"))
        return self.sc._restart_mid


class Restart(Base):
    def test_no_deletes_question(self):
        mid = self.ask_restart()
        n = len(self.sc.tg.sent)
        self.sc.handle_update(tap("k|restart_no", mid))
        self.assertIn(mid, self.sc.tg.deleted)
        self.assertEqual(len(self.sc.tg.sent), n)                 # پیام اضافه نمی‌آید
        self.assertIn("ری‌استارت لغو شد", self.sc.tg.acks)
        self.popen.assert_not_called()
        self.sc.handle_update(tap("k|restart_yes", mid))           # کلیک بعدی روی همان سؤال
        self.popen.assert_not_called()

    def test_yes_leaves_one_line_record_without_buttons(self):
        mid = self.ask_restart()
        self.sc.handle_update(tap("k|restart_yes", mid))
        self.popen.assert_called_once()
        self.exit.assert_called_once_with(0)
        emid, text, buttons = self.sc.tg.edits[-1]
        self.assertEqual(emid, mid)
        self.assertIn("ری‌استارت شد", text)
        self.assertIn("Hamid", text)
        self.assertIsNone(buttons)
        self.assertFalse(any("در حال ری‌استارت است" in t for t, _ in self.sc.tg.sent))

    def test_stale_question_after_restart_does_nothing(self):
        self.sc._restart_mid = None                                # نسخهٔ تازه بعد از ری‌استارت
        self.sc.handle_update(tap("k|restart_yes", 555))
        self.popen.assert_not_called()
        self.assertEqual(self.sc.tg.edits[-1][0], 555)
        self.assertIsNone(self.sc.tg.edits[-1][2])
        self.assertIn("قدیمی", self.sc.tg.acks[-1])

    def test_second_question_replaces_first(self):
        first = self.ask_restart()
        second = self.ask_restart()
        self.assertIn(first, self.sc.tg.deleted)
        self.sc.handle_update(tap("k|restart_yes", first))
        self.popen.assert_not_called()
        self.sc.handle_update(tap("k|restart_yes", second))
        self.popen.assert_called_once()

    def test_command_without_message_id_still_works(self):
        self.ask_restart()
        self.sc.handle_update(tap("k|restart_no"))
        self.assertIn("ری‌استارت لغو شد.", self.sc.tg.sent[-1][0])


class ModeAndExit(Base):
    def test_mode_menu_loses_buttons(self):
        self.sc.mode_menu()
        mid = self.sc.tg.last_mid
        self.sc.handle_update(tap("m|hold", mid))
        self.assertEqual(self.sc.mode, "hold")
        emid, text, buttons = self.sc.tg.edits[-1]
        self.assertEqual(emid, mid)
        self.assertIn("<b>نگه داشته شود</b> ✅", text)
        self.assertIn("Hamid", text)
        self.assertIsNone(buttons)
        self.assertIn("حالت ثبت شد", self.sc.tg.sent[-1][0])       # پیام تأیید مثل قبل

    def test_over_cap_question_loses_buttons_and_old_tap_is_harmless(self):
        self.sc.emerg_cap = 20.0
        self.sc.client.get_tick.return_value = NS(time=0, bid=4105.4, ask=4105.7)
        self.sc.client.positions.return_value = [live()]
        self.sc.open[5] = held()
        self.sc.handle_update(tap("e|5"))
        prompt = self.sc.open[5]["xprompt"]
        self.sc.handle_update({"message": {"from": {"id": ADMIN}, "text": "4120 4095",
                                           "reply_to_message": {"message_id": prompt}}})
        qmid = self.sc.tg.last_mid
        self.sc.handle_update(tap("e|5|y", qmid))
        self.sc.client.modify_sltp.assert_called_once_with(5, sl=4095.0, tp=4120.0)
        edit = [e for e in self.sc.tg.edits if e[0] == qmid][-1]
        self.assertIn("تأیید شد", edit[1])
        self.assertIsNone(edit[2])
        self.sc.handle_update(tap("e|5|y", qmid))                  # کلیک دوباره
        self.sc.client.modify_sltp.assert_called_once()
        self.assertIn("دیگر معتبر نیست", self.sc.tg.edits[-1][1])
        self.assertNotEqual(self.sc.open[5]["xsl"], EMERG)


if __name__ == "__main__":
    unittest.main()
