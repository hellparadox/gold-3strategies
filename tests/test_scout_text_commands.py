"""هر دکمهٔ اسکات یک دستور متنی هم دارد تا بات ادمین (Muse) بتواند مثل مالک عمل کند: تأیید/رد هشدار (با
reverse / both / force)، خلاف جهت، نگه‌دار، بی‌ضرر، قیمت خروج، تنظیمات، حالت حد ضرر، هشدار قیمت.
جواب‌هایی که فقط با ویرایش کارت می‌آمد (و بات نمی‌بیند) پیام تازه هم می‌شود. آدم‌ها هم می‌توانند از این دستورها استفاده کنند."""
import csv
import html
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from tests.test_scout_exits import Base as ExitBase
from tests.test_scout_exits import ENTRY, EMERG, T0, held, live
from tests.test_scout_multi import row
from tests.test_scout_settings import setup_settings
from tests.test_scout_tracker import FakeTG, make_scout
from tools import scout_control as ctl
from tools.scout_bot import TEXT_CB, TG

BOT, HUMAN = 555, 1


class EchoTG(FakeTG):
    """FakeTG که مثل TG واقعی ویرایش‌ها را (وقتی echo_edits روشن است) و toastها را پیام می‌کند."""
    echo_edits = False

    def edit(self, mid, text, buttons=None):
        r = super().edit(mid, text, buttons)
        if self.echo_edits:
            self.send(ctl.echo_text(text, buttons))
        return r

    def ack(self, cb, text=""):
        super().ack(cb, text)
        if cb == TEXT_CB and text:
            self.send(html.escape(str(text)))


class Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tmp = Path(self._d.name)
        self.sc = make_scout(self.tmp)
        self.sc.tg = EchoTG()
        setup_settings(self.sc, self.tmp)
        self.sc.admins = {HUMAN, BOT}
        self.sc.client.get_tick.return_value = NS(time=0, bid=4175.4, ask=4175.69)
        self.sc.client.positions.return_value = []
        self.sc.client.close_position.return_value = NS(ok=True, price=4175.4, comment="")
        self.n = 0

        def order(side, lot, sl=None, tp=None, comment=""):
            self.n += 1
            return NS(ok=True, position=100 + self.n, order=100 + self.n, deal=0,
                      price=4175.69 if side == "BUY" else 4175.4, comment="")
        self.sc.client.send_market_order.side_effect = order

    def tearDown(self):
        self._d.cleanup()

    def bot(self, text, uid=BOT):
        self.sc.handle_update({"message": {"message_id": 900, "text": text,
                                           "from": {"id": uid, "is_bot": True, "first_name": "Alpha"}}})

    def human(self, text):
        self.sc.handle_update({"message": {"message_id": 5, "text": text,
                                           "from": {"id": HUMAN, "first_name": "Hamid"}}})

    def last(self):
        return self.sc.tg.sent[-1][0]

    def all_text(self):
        return "\n".join(m[0] for m in self.sc.tg.sent)

    def events(self):
        if not self.sc.journal.exists():
            return []
        with self.sc.journal.open(encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def alert(self, side="BUY", ref=4175.5):
        self.sc.alert(row(side=side, ref=ref), 27.0)
        return str(self.sc.n)


class Alerts(Base):
    def test_alert_card_tells_a_bot_the_number(self):
        aid = self.alert()
        self.assertIn(f"/yes {aid} · /no {aid}", self.sc.pending[aid]["txt"])
        self.assertIn(f"/yes {aid}", self.sc.tg.sent[0][0])

    def test_yes_without_number_when_only_one_alert(self):
        self.alert()
        self.bot("/yes@HellTreid_bot")
        self.sc.client.send_market_order.assert_called_once()
        self.assertEqual(self.sc.pending, {})
        opened = [e for e in self.events() if e["event"] == "opened"][-1]
        self.assertIn("by=Alpha", opened["note"])

    def test_yes_with_number_and_hash(self):
        a1 = self.alert()
        self.sc.n += 1
        self.alert(side="SELL")
        self.bot(f"/yes #{a1}")
        self.assertEqual(self.sc.client.send_market_order.call_args.args[0], "BUY")

    def test_yes_needs_a_number_when_several_or_none(self):
        self.bot("/yes")
        self.assertIn("الان هشدار بازی نیست", self.last())
        self.alert()
        self.sc.n += 1
        self.alert(side="SELL")
        self.bot("/yes")
        self.assertIn("شمارهٔ هشدار را بنویسید", self.last())
        self.assertIn("#1 BUY", self.last())
        self.sc.client.send_market_order.assert_not_called()

    def test_stale_number_explains(self):
        self.alert()
        self.bot("/yes 99")
        self.assertIn("هشدار #99 دیگر معتبر نیست", self.last())
        self.sc.client.send_market_order.assert_not_called()

    def test_no_rejects(self):
        aid = self.alert()
        self.bot(f"/no {aid}")
        self.sc.client.send_market_order.assert_not_called()
        self.assertEqual(self.sc.pending, {})
        self.assertIn("rejected", [e["event"] for e in self.events()])
        self.assertIn("رد شد", self.all_text())

    def test_human_can_use_text_commands_too(self):
        aid = self.alert()
        self.human(f"/yes {aid}")
        self.sc.client.send_market_order.assert_called_once()

    def test_unlisted_sender_ignored(self):
        aid = self.alert()
        self.bot(f"/yes {aid}", uid=999)
        self.sc.client.send_market_order.assert_not_called()
        self.assertIn(aid, self.sc.pending)

    def test_hedge_question_reaches_the_bot_and_reverse_answers_it(self):
        a1 = self.alert()
        self.bot(f"/yes {a1}")                                   # #101 BUY
        self.assertIn(101, self.sc.open)
        self.sc.n += 1
        a2 = self.alert(side="SELL", ref=4175.4)
        self.sc.tg.sent.clear()
        self.bot(f"/yes {a2}")
        self.assertIn(a2, self.sc.pending)                       # هنوز باز نشده؛ سؤال پرسیده شد
        q = self.all_text()
        self.assertIn("پوزیشن خرید باز است", q)                  # سؤال فقط با ویرایش کارت نبود: پیام شد
        self.assertIn(f"/yes {a2} reverse", q)
        self.assertIn(f"/yes {a2} both", q)
        self.assertIn(f"/no {a2}", q)
        self.sc.client.close_position.assert_not_called()
        self.bot(f"/yes {a2} reverse")
        self.sc.client.close_position.assert_called_once()
        self.assertEqual(self.sc.open[102]["side"], "SELL")

    def test_both_keeps_both_open(self):
        a1 = self.alert()
        self.bot(f"/yes {a1}")
        self.sc.n += 1
        a2 = self.alert(side="SELL", ref=4175.4)
        self.bot(f"/yes {a2}")
        self.bot(f"/yes {a2} both")
        self.sc.client.close_position.assert_not_called()
        self.assertEqual({i["side"] for i in self.sc.open.values()}, {"BUY", "SELL"})

    def test_limit_question_then_force(self):
        self.sc.max_open = 1
        a1 = self.alert()
        self.bot(f"/yes {a1}")
        self.sc.n += 1
        a2 = self.alert(ref=4180.0)
        self.sc.tg.sent.clear()
        self.bot(f"/yes {a2}")
        self.assertIn(a2, self.sc.pending)
        self.assertIn(f"/yes {a2} force", self.all_text())
        self.assertEqual(len(self.sc.open), 1)
        self.bot(f"/yes {a2} force")
        self.assertEqual(len(self.sc.open), 2)
        self.assertIn("over_cap=owner", [e for e in self.events() if e["event"] == "opened"][-1]["note"])

    def test_contra_preview_then_yes_then_no(self):
        aid = self.alert(side="BUY")
        self.bot(f"/contra {aid}")
        self.assertIn("SELL به‌جای BUY", self.last())
        self.assertIn(f"/contra {aid} yes", self.last())
        self.sc.client.send_market_order.assert_not_called()
        self.bot(f"/contra {aid} no")
        self.assertIn(aid, self.sc.pending)
        self.bot(f"/contra {aid} yes")
        self.assertEqual(self.sc.client.send_market_order.call_args.args[0], "SELL")
        self.assertTrue(self.sc.open[101]["contra"])

    def test_analyze_unavailable_says_so(self):
        aid = self.alert()
        self.sc._ai = False
        self.bot(f"/analyze {aid}")
        self.assertIn("تحلیل در دسترس نیست", self.all_text())


class Positions(ExitBase):
    def setUp(self):
        super().setUp()
        self.sc.tg = EchoTG()
        setup_settings(self.sc, self.tmp)
        self.sc.admins = {1, 555}
        self.sc.emerg_cap = 20.0

    def bot(self, text):
        self.sc.handle_update({"message": {"message_id": 7, "text": text,
                                           "from": {"id": 555, "is_bot": True, "first_name": "Alpha"}}})

    def test_hold_unhold_lock_without_ticket_when_one_open(self):
        self.sc.open[5]["hold"] = False
        self.bot("/hold")
        self.assertTrue(self.sc.open[5]["hold"])
        self.assertIn("#5 نگه داشته شد", self.last())
        self.bot("/unhold 5")
        self.assertFalse(self.sc.open[5]["hold"])
        self.assertIn("نگه داشتن لغو شد", self.last())

    def test_ticket_with_bot_suffix_and_unknown_ticket(self):
        self.sc.open[5]["hold"] = False
        self.bot("/hold@HellTreid_bot 5")
        self.assertTrue(self.sc.open[5]["hold"])
        self.bot("/hold 117628599")
        self.assertIn("پوزیشن #117628599 باز نیست", self.last())

    def test_exit_requires_hold_then_sets_and_over_cap_asks(self):
        self.sc.open[5]["hold"] = False
        self.bot("/exit 5 4120 4100")
        self.assertIn("اول /hold 5".replace("/hold 5", "<code>/hold 5</code>"), self.last())
        self.sc.client.modify_sltp.assert_not_called()
        self.sc.open[5]["hold"] = True
        self.bot("/exit 5 4120 4100")
        self.sc.client.modify_sltp.assert_called_with(5, sl=4100.0, tp=4120.0)
        self.bot("/exit 5 4120 4095")                             # ۲۲$ ضرر > سقف ۲۰$
        self.assertIn("22.18$", self.last())
        self.assertEqual(self.sc.client.modify_sltp.call_args.kwargs["sl"], 4100.0)   # هنوز عوض نشده
        self.bot("/exitno 5")
        self.assertEqual(self.sc.client.modify_sltp.call_args.kwargs["sl"], 4100.0)
        self.bot("/exit 5 4120 4095")
        self.bot("/exitok 5")
        self.sc.client.modify_sltp.assert_called_with(5, sl=4095.0, tp=4120.0)
        self.bot("/exitdel 5")
        self.assertIsNone(self.sc.open[5]["xsl"])

    def test_exit_without_numbers_shows_usage(self):
        self.bot("/exit 5")
        self.assertIn("/exit 5 4120 4095", self.last())
        self.sc.client.modify_sltp.assert_not_called()

    def test_lock_moves_stop_to_entry_without_target(self):
        self.sc.client.get_tick.return_value = NS(time=T0 + 600, bid=ENTRY + 7.0, ask=ENTRY + 7.3)
        self.bot("/lock 5")
        self.sc.client.modify_sltp.assert_called_with(5, sl=round(ENTRY, 2))
        self.assertTrue(self.sc.open[5]["locked"])

    def test_close_forms(self):
        self.sc.client.positions.return_value = [live(), NS(ticket=117628567)]
        self.bot("/close@HellTreid_bot 117628567")                # تیکت روی بروکر، حتی اگر در حافظه نباشد
        self.assertEqual(self.sc.client.close_position.call_args.args[0], 117628567)
        self.sc.client.close_position.reset_mock()
        self.sc._closing_t.clear()
        self.bot("/close 117628567@HellTreid_bot")                 # شکل اشتباهی که قبلاً بی‌صدا نادیده می‌شد
        self.assertEqual(self.sc.client.close_position.call_args.args[0], 117628567)
        self.sc._closing_t.clear()
        self.bot("/close")                                         # یک پوزیشن باز → همان
        self.assertEqual(self.sc.client.close_position.call_args.args[0], 5)
        self.assertEqual(self.last().count("بستن #5"), 1)           # نتیجه یک بار گفته می‌شود

    def test_close_needs_ticket_when_several(self):
        self.sc.open[6] = held()
        self.bot("/close")
        self.assertIn("شمارهٔ تیکت را بنویسید", self.last())
        self.sc.client.close_position.assert_not_called()


class Settings(Base):
    def test_show_and_plain_set_within_limits(self):
        self.bot("/set")
        self.assertIn("سقف حد ضرر سیگنال", self.last())
        self.bot("/set max_risk 25")
        self.assertEqual(self.sc.max_total_risk, 25.0)
        self.assertIn("تنظیم عوض شد", self.last())
        self.assertIn("by=Alpha", [e for e in self.events() if e["event"] == "setting"][-1]["note"])

    def test_lot_needs_setok(self):
        self.bot("/set lot 0.02")
        self.assertEqual(self.sc.lot, 0.01)
        self.assertIn("/setok", self.last())
        self.bot("/setok")
        self.assertEqual(self.sc.lot, 0.02)

    def test_setok_without_question_and_bad_values(self):
        self.bot("/setok")
        self.assertIn("سؤالی برای تأیید نیست", self.last())
        self.bot("/set lot 5")
        self.assertIn("باید بین", self.last())
        self.bot("/set lot abc")
        self.assertIn("عدد نیست", self.last())
        self.bot("/set colour 3")
        self.assertIn("کلیدها", self.last())
        self.bot("/set emerg_cap 6")                                # کمتر از سقف حد ضرر سیگنال (۱۰)
        self.assertIn("سقف اضطراری", self.last())
        self.assertEqual((self.sc.lot, self.sc.emerg_cap_usd), (0.01, 20.0))

    def test_big_jump_asks_and_reset(self):
        self.bot("/set max_risk 60")                                # بیش از دو برابر ۲۰
        self.assertEqual(self.sc.max_total_risk, 20.0)
        self.assertIn("/setok", self.last())
        self.bot("/setok")
        self.assertEqual(self.sc.max_total_risk, 60.0)
        self.bot("/set reset")
        self.assertEqual(self.sc.max_total_risk, 60.0)
        self.bot("/setok")
        self.assertEqual(self.sc.max_total_risk, 20.0)

    def test_mode(self):
        self.bot("/mode hold")
        self.assertEqual(self.sc.mode, "hold")
        self.assertIn("در حد ضرر", self.last())
        self.bot("/mode banana")
        self.assertEqual(self.sc.mode, "hold")
        self.assertIn("یکی از این‌ها", self.last())
        self.bot("/mode")                                           # بدون عدد: منوی دکمه‌ها مثل قبل
        self.assertIsNotNone(self.sc.tg.sent[-1][1])

    def test_price_alert_list_has_delete_commands(self):
        self.sc._palerts = [{"id": "a1", "level": 4150.0, "dir": "up", "note": ""}]
        self.bot("/alerts")
        self.assertIn("/alertdel a1", self.last())
        self.bot("/alertdel a1")
        self.assertEqual(self.sc._palerts, [])
        self.assertIn("حذف شد", self.last())

    def test_cmds_page(self):
        self.bot("/cmds")
        self.assertEqual(self.last(), ctl.HELP_CMDS)
        for c in ("/yes", "/no", "/hold", "/lock", "/exit", "/set", "/mode", "/contra", "/alertdel"):
            self.assertIn(c, ctl.HELP_CMDS)

    def test_unknown_text_is_ignored(self):
        self.bot("hello there")
        self.bot("/nonsense 1")
        self.assertEqual(self.sc.tg.sent, [])


class Pure(unittest.TestCase):
    def test_every_button_kind_has_a_command(self):
        cases = {"a|7|y": "/yes 7", "a|7|n": "/no 7", "a|7|r": "/yes 7 reverse", "a|7|b": "/yes 7 both",
                 "a|7|o": "/yes 7 force", "a|7|q": "/yes 7 both force", "a|7|x": "/yes 7 reverse force",
                 "x|7": "/analyze 7", "v|7": "/contra 7", "v|7|y": "/contra 7 yes", "v|7|n": "/contra 7 no",
                 "c|5": "/close 5", "h|5": "/hold 5", "u|5": "/unhold 5", "z|5": "/lock 5",
                 "e|5|d": "/exitdel 5", "e|5|y": "/exitok 5", "e|5|n": "/exitno 5", "m|ai": "/mode ai",
                 "p|a1": "/alertdel a1"}
        for data, cmd in cases.items():
            self.assertEqual(ctl.button_command(data), cmd, data)
        self.assertIn("/exit 5", ctl.button_command("e|5"))
        for data in ("g|lot|+", "k|status", "a|7|zz", "", None):
            self.assertIsNone(ctl.button_command(data), data)

    def test_buttons_as_commands_and_echo(self):
        b = [[{"text": "✅ تأیید", "callback_data": "a|7|y"}, {"text": "⚙", "callback_data": "g|x"}]]
        self.assertIn("✅ تأیید ← <code>/yes 7</code>", ctl.buttons_as_commands(b))
        self.assertEqual(ctl.buttons_as_commands([[{"text": "x", "callback_data": "g|x"}]]), "")
        self.assertEqual(ctl.buttons_as_commands(None), "")
        out = ctl.echo_text("card line 1\ncard line 2\n\n⚠️ سؤال تازه", b)
        self.assertTrue(out.startswith("⚠️ سؤال تازه"))
        self.assertNotIn("card line", out)
        self.assertIn("/yes 7", out)

    def test_help_pages_fit_in_one_telegram_message(self):
        self.assertLess(len(ctl.HELP_CMDS), 4096)
        help_ = ctl.HELP_TEXT.format(max_open=2, risk=20, sl=10, em=20, status=5)
        self.assertLess(len(help_), 4096)
        self.assertIn("/cmds", help_)
        names = [c["command"] for c in ctl.MENU_COMMANDS]
        self.assertEqual(len(names), len(set(names)))
        for c in ("yes", "no", "hold", "unhold", "lock", "exit", "set", "cmds"):
            self.assertIn(c, names)


class RealTG(unittest.TestCase):
    def mk(self):
        tg = TG.__new__(TG)
        tg.token, tg.chat, tg.default_reply, tg.echo_edits, tg.last_mid, tg.last_error = "t", 1, None, False, None, ""
        tg.calls = []

        def call(method, **kw):
            tg.calls.append((method, kw))
            return {"message_id": 9}
        tg._call = call
        return tg

    def test_text_ack_becomes_escaped_message_and_empty_stays_silent(self):
        tg = self.mk()
        tg.ack("", "x")
        tg.ack(TEXT_CB, "")
        self.assertEqual(tg.calls, [])
        tg.ack(TEXT_CB, "قیمت <5 & بالا")
        self.assertEqual(tg.calls[0][0], "sendMessage")
        self.assertIn("&lt;5 &amp; بالا", tg.calls[0][1]["text"])
        tg.ack("real-callback-id", "ok")
        self.assertEqual(tg.calls[-1][0], "answerCallbackQuery")

    def test_edit_echo_only_when_asked(self):
        tg = self.mk()
        tg.edit(3, "card\n\nresult", [[{"text": "OK", "callback_data": "a|1|y"}]])
        self.assertEqual([c[0] for c in tg.calls], ["editMessageText"])
        tg.calls.clear()
        tg.echo_edits = True
        tg.default_reply = 77
        tg.edit(3, "card\n\nresult", [[{"text": "OK", "callback_data": "a|1|y"}]])
        self.assertEqual([c[0] for c in tg.calls], ["editMessageText", "sendMessage"])
        sent = tg.calls[1][1]
        self.assertTrue(sent["text"].startswith("result"))
        self.assertIn("/yes 1", sent["text"])
        self.assertEqual(sent["reply_parameters"]["message_id"], 77)


if __name__ == "__main__":
    unittest.main()
