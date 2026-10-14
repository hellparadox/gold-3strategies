"""تب «معاملات» داشبورد: جدول معاملات بسته‌شده با «چه کسی باز/بست»، و ثبت نام‌ها در ژورنال."""
import json
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

from tests.test_scout_tracker import ENTRY, FRAME, SOFT, make_scout, pos
from tools import scout_dashboard as dash
from tools import scout_tracker as trk


def ev(event, ts, **kw):
    d = {"ts": ts, "event": event}
    d.update({k: ("" if v is None else str(v)) for k, v in kw.items()})
    return d


def T(day, hm):                                   # با منطقه می‌نویسیم تا تست به ساعت ماشین بستگی نداشته باشد
    return f"2026-10-{day:02d}T{hm}:00+00:00"


def trade(tk, day, o_hm, c_hm, *, side="SELL", setup="rejection", price=4100.0, sl=4106.0, profit=-6.0,
          note_open="lot=0.01", gone_note="", extra=()):
    out = [ev("opened", T(day, o_hm), ticket=tk, side=side, setup=setup, price=price, sl=sl, note=note_open)]
    out += list(extra)
    out.append(ev("gone", T(day, c_hm), ticket=tk, side=side, setup=setup, entry=price, price=price + 6,
                  profit=profit, note=gone_note))
    return out


class Build(unittest.TestCase):
    def one(self, events, **kw):
        rows = dash.build_trades(events, **kw)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_new_manual_close_has_both_names(self):
        r = self.one(trade("11", 6, "10:00", "11:14", note_open="broker_sl=1;lot=0.01;by=Hellparadox",
                           gone_note=";how=manual;by=Hellparadox;cby=Ali", profit=-16.74,
                           price=4138.34, sl=4143.92))
        self.assertEqual(r["opened_by"], "شما (Hellparadox)")
        self.assertEqual(r["closed_by"], "شما (Ali)")
        self.assertEqual(r["closed_how"], "دکمهٔ بستن")
        self.assertEqual((r["side"], r["setup"], r["lot"], r["profit"]), ("SELL", "rejection", 0.01, -16.74))
        self.assertEqual(r["secs"], 74 * 60)
        self.assertAlmostEqual(r["r"], -3.0, places=2)             # 16.74 / (5.58 × 100 × 0.01)

    def test_emergency_stop_on_broker(self):
        r = self.one(trade("12", 6, "10:00", "11:00", gone_note="emergency_sl;how=;by=Hell;cby=;held=1"))
        self.assertEqual((r["closed_by"], r["closed_how"], r["close_code"]), ("بروکر", "حد ضرر اضطراری", "emergency_sl"))
        self.assertTrue(r["held"])

    def test_bot_stop_distinguishes_signal_and_emergency(self):
        sig = self.one(trade("13", 6, "10:00", "10:30", gone_note="signal_sl;how=stop;by=;cby="))
        self.assertEqual((sig["closed_by"], sig["closed_how"]), ("ربات", "حد ضرر سیگنال"))
        em = self.one(trade("14", 6, "10:00", "10:30", gone_note="emergency_sl;how=stop;by=;cby="))
        self.assertEqual((em["closed_by"], em["closed_how"]), ("ربات", "حد ضرر اضطراری"))

    def test_ai_reverse_and_user_exit_price(self):
        for how, who, text in (("ai", "هوش مصنوعی", "تصمیم در حد ضرر"), ("reverse", "شما", "عوض کردن جهت"),
                               ("exit_tp", "ربات", "قیمت خروج شما (بالا)"), ("exit_sl", "ربات", "قیمت خروج شما (پایین)")):
            r = self.one(trade("15", 6, "10:00", "10:30", gone_note=f"exit_tp;how={how};by=;cby="))
            self.assertEqual((r["closed_by"], r["closed_how"]), (who, text), how)

    def test_external_close(self):
        r = self.one(trade("16", 6, "10:00", "10:30", gone_note=""))
        self.assertEqual((r["closed_by"], r["close_code"]), ("بیرون از اسکات", "external"))

    def test_old_trades_guess_from_events(self):
        manual = self.one(trade("21", 2, "10:00", "10:30", gone_note="",
                                extra=[ev("closed", T(2, "10:29"), ticket="21", ok="True")]))
        self.assertEqual((manual["closed_by"], manual["close_code"], manual["opened_by"]),
                         ("شما", "manual", "شما (نام ثبت نشده)"))
        rev = self.one(trade("22", 2, "10:00", "10:30", extra=[ev("closed", T(2, "10:29"), ticket="22", ok="True", note="reverse")]))
        self.assertEqual(rev["close_code"], "reverse")
        ai = self.one(trade("23", 2, "10:00", "10:30", extra=[ev("ai_close", T(2, "10:29"), ticket="23")]))
        self.assertEqual(ai["close_code"], "ai")
        for note, code in (("رسیدن به حد ضرر سیگنال", "stop"), ("عبور از حد ضرر اضطراری", "emergency_bot"),
                           ("رسیدن به قیمت خروج شما", "exit_tp"), ("رسیدن به قیمت خروج پایین شما", "exit_sl"),
                           ("هوش مصنوعی در دسترس نیست", "ai")):
            r = self.one(trade("24", 2, "10:00", "10:30", extra=[ev("sl_close", T(2, "10:29"), ticket="24", ok="True", note=note)]))
            self.assertEqual(r["close_code"], code, note)
        failed = self.one(trade("25", 2, "10:00", "10:30", gone_note="emergency_sl",
                                extra=[ev("sl_close", T(2, "10:29"), ticket="25", ok="False", note="رسیدن به حد ضرر سیگنال")]))
        self.assertEqual(failed["close_code"], "emergency_sl")      # بستن ربات شکست خورد؛ بروکر بست
        no_name = self.one(trade("26", 2, "10:00", "10:30", gone_note="signal_sl"))
        self.assertEqual(no_name["close_code"], "signal_sl")

    def test_manual_name_from_closed_event_when_gone_lacks_it(self):
        r = self.one(trade("27", 2, "10:00", "10:30", gone_note=";how=manual;by=;cby=",
                           extra=[ev("closed", T(2, "10:29"), ticket="27", ok="True", note="by=Ali")]))
        self.assertEqual(r["closed_by"], "شما (Ali)")

    def test_newest_first_and_day_grouping(self):
        events = (trade("31", 3, "10:00", "10:30") + trade("32", 5, "09:00", "09:30") + trade("33", 4, "20:00", "21:00")
                  + trade("34", 5, "22:00", "23:50"))
        rows = dash.build_trades(events)
        self.assertEqual([r["ticket"] for r in rows], ["34", "32", "33", "31"])
        # روز به وقت تهران (UTC+3:30): 23:50 UTC روز بعد است و 21:00 UTC هم (00:30 تهران)
        self.assertEqual([r["day"] for r in rows], ["2026-10-06", "2026-10-05", "2026-10-05", "2026-10-03"])
        self.assertTrue(all(r["day_fa"] for r in rows))

    def test_open_positions_and_unprofited_rows_are_not_listed(self):
        only_open = [ev("opened", T(5, "10:00"), ticket="41", side="BUY", setup="x", price=1, sl=1)]
        no_profit = [ev("gone", T(5, "10:00"), ticket="42", side="BUY", setup="x", profit="")]
        self.assertEqual(dash.build_trades(only_open + no_profit), [])
        rows = dash.build_trades(no_profit, actual_by_ticket={"42": 3.5})     # سود واقعی از MT5
        self.assertEqual(rows[0]["profit"], 3.5)

    def test_duplicate_gone_counts_once_and_adopted(self):
        events = trade("51", 5, "10:00", "10:30") + [ev("gone", T(5, "10:31"), ticket="51", profit=1.0)]
        self.assertEqual(len(dash.build_trades(events)), 1)
        ad = [ev("adopted", T(5, "10:00"), ticket="52", side="BUY", setup="kijun_pullback", entry=4100, sl=4094),
              ev("gone", T(5, "10:40"), ticket="52", side="BUY", setup="kijun_pullback", entry=4100, price=4112, profit=12.0)]
        r = self.one(ad)
        self.assertIn("بیرون از اسکات", r["opened_by"])
        self.assertAlmostEqual(r["r"], 2.0, places=2)

    def test_contra_lot_and_garbage_rows(self):
        r = self.one(trade("61", 5, "10:00", "10:30", note_open="contra=1;lot=0.02", profit=-12.0))
        self.assertTrue(r["contra"])
        self.assertEqual(r["lot"], 0.02)
        self.assertAlmostEqual(r["r"], -1.0, places=2)                        # 12 / (6 × 100 × 0.02)
        junk = [{"event": "gone", "ticket": "7", "ts": "not-a-date", "profit": "1"}, {"event": "opened"}, {}]
        self.assertEqual(dash.build_trades(junk), [])

    def test_stats_carry_trades_and_limit(self):
        events = []
        for i in range(30):
            events += trade(str(100 + i), 5, "10:00", "10:30")
        st = dash.build_stats(events, lot=0.01)
        self.assertEqual(len(st["trades"]), 30)
        self.assertEqual(len(dash.build_trades(events, limit=10)), 10)


class Journal(unittest.TestCase):
    """ثبت نام‌ها در ژورنال ربات."""

    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tmp = Path(self._d.name)
        self.sc = make_scout(self.tmp)
        self.sc.client.get_tick.return_value = NS(time=0, bid=4175.4, ask=4175.69)
        self.sc.client.send_market_order.return_value = NS(ok=True, position=9, order=9, price=4175.69, comment="")
        self.sc.client.positions.return_value = []

    def tearDown(self):
        self._d.cleanup()

    def rows(self):
        return dash.read_journal(self.sc.journal)

    def approve(self, who="Hellparadox"):
        import pandas as pd
        row = pd.Series({"setup": "kijun_pullback", "side": "BUY", "ref_close": 4175.5})
        self.sc.pending["1"] = {"row": row, "sl": 4169.65, "mid": 1, "txt": "t", "t": 0}
        self.sc._who = who
        self.sc.decide("1", True, "cb")

    def test_opened_note_has_the_approver(self):
        self.approve("Hell &amp; Co;x=1")
        note = next(r for r in self.rows() if r["event"] == "opened")["note"]
        kv = dash._kv(note)
        self.assertEqual(kv["by"], "Hell & Co x 1")                # بدون ; و = که یادداشت را می‌شکنند
        self.assertEqual(kv["lot"], "0.01")                        # خواندن قبلی لات سالم است
        self.assertEqual(dash._note_lot(note, 0.5), 0.01)

    def test_manual_close_records_who_and_gone_note(self):
        self.approve("Hellparadox")
        self.sc._who = "Ali"
        self.sc.client.close_position.return_value = NS(ok=True, price=4170.0, comment="")
        self.sc.client.positions.return_value = [pos(tk=9, price=4170.0, profit=-5.0, sl=SOFT)]
        self.sc.close(9, "cb")
        self.assertEqual(self.sc.open[9]["cby"], "Ali")
        closed = next(r for r in self.rows() if r["event"] == "closed")
        self.assertEqual(dash._kv(closed["note"])["by"], "Ali")
        self.sc.client.positions.return_value = []
        self.sc.realized = Mock(return_value=(-5.0, 4170.0))
        with unittest.mock.patch("tools.scout_bot.time.time", return_value=5000.0):
            self.sc.monitor(FRAME)
        gone = next(r for r in self.rows() if r["event"] == "gone")
        kv = dash._kv(gone["note"])
        self.assertEqual((kv["how"], kv["by"], kv["cby"]), ("manual", "Hellparadox", "Ali"))
        t = dash.build_trades(self.rows())[0]
        self.assertEqual((t["opened_by"], t["closed_by"], t["closed_how"]), ("شما (Hellparadox)", "شما (Ali)", "دکمهٔ بستن"))
        self.assertEqual((t["ticket"], t["side"], t["setup"]), ("9", "BUY", "kijun_pullback"))

    def test_hold_logs_who_and_marks_held(self):
        self.approve("Hellparadox")
        self.sc._who = "Hellparadox"
        self.sc.hold(9)
        h = next(r for r in self.rows() if r["event"] == "hold")
        self.assertEqual(dash._kv(h["note"])["by"], "Hellparadox")

    def test_gone_note_keeps_kind_as_first_token(self):
        self.approve()
        self.sc.client.positions.return_value = []
        self.sc.realized = Mock(return_value=(-16.0, 4157.5))
        self.sc.open[9]["hold"] = True
        with unittest.mock.patch("tools.scout_bot.time.time", return_value=5000.0):
            self.sc.monitor(FRAME)
        note = next(r for r in self.rows() if r["event"] == "gone")["note"]
        self.assertEqual(note.split(";")[0], "emergency_sl")        # قالب قدیمی: توکن اول = نوع خروج
        self.assertEqual(dash._kv(note)["held"], "1")

    def test_cby_survives_restart(self):
        self.assertIn("cby", trk.STATE_KEYS)


class Page(unittest.TestCase):
    def test_tab_and_columns_exist(self):
        for part in ('id="t-trades"', 'id="p-trades"', 'id="trbody"', "renderTrades(st.trades)",
                     "const PANES=['dash','trades','audit','guide']", "چه کسی باز کرد", "چه کسی بست"):
            self.assertIn(part, dash.PAGE)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_page_javascript_parses_and_renders_sample_rows(self):
        page = dash.PAGE.replace("<!--GUIDE-->", dash.GUIDE)
        js = page.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        rows = dash.build_trades(trade("9", 6, "10:00", "11:14", gone_note=";how=manual;by=H;cby=H", profit=-16.74,
                                       note_open="lot=0.01;by=H")
                                 + trade("8", 5, "09:00", "09:30", side="BUY", profit=4.2, gone_note="exit_tp;how=exit_tp;by=H;cby="))
        harness = r"""
const els={};const mk=id=>els[id]||(els[id]={id,innerHTML:'',textContent:'',value:'',hidden:false,
  setAttribute(){},addEventListener(){},scrollIntoView(){}});
global.document={getElementById:mk,querySelectorAll:()=>[]};
global.window={scrollTo(){}};global.location={hash:'',pathname:'/',search:''};global.history={replaceState(){}};
global.fetch=()=>new Promise(()=>{});global.setInterval=()=>0;
""" + js + r"""
renderTrades(%s);
const out=els.trbody.innerHTML;
if(!out.includes('#9')||!out.includes('#8'))throw new Error('rows missing');
if(out.indexOf('#9')>out.indexOf('#8'))throw new Error('order');
if(!out.includes('dayrow'))throw new Error('day header');
if(!els.trtiles.innerHTML.includes('2'))throw new Error('tiles');
TR.res='win';renderTrades(%s);
if(els.trbody.innerHTML.includes('#9'))throw new Error('filter');
console.log('OK');
""" % (json.dumps(rows), json.dumps(rows))
        res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("OK", res.stdout)


if __name__ == "__main__":
    unittest.main()
