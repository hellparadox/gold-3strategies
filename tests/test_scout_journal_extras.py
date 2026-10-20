"""فقط نمایش و ثبت: خط روند روی کارت، زمان تصمیم، لغزش ورود، سه فیلد تازهٔ هشدار و برش‌های تازهٔ کارنامه.
هیچ‌کدام سیگنال، سفارش، حد ضرر یا تأیید را عوض نمی‌کند."""
import csv
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import numpy as np
import pandas as pd

from tests.test_scout_tracker import make_scout
from tools import scout_dashboard as dash
from tools import scout_signal_audit as sa
from tools.scout_bot import JOURNAL_FIELDS, Scout

BAR = pd.Timestamp("2026-10-06 09:15")      # سه‌شنبه


def row(side="BUY"):
    return pd.Series({"setup": "rejection", "side": side, "ref_close": 4100.0, "sl_dist": 6.0,
                      "tp_dist": 12.0, "atr": 5.0, "bar": BAR})


def frame(close=4100.0, h1=4090.0, ema=4080.0):
    return pd.DataFrame({"close": [close], "h1_ema": [h1], "ema200": [ema], "cloud_top": [4095.0],
                         "cloud_bot": [4085.0]})


class TrendLine(unittest.TestCase):
    def test_states(self):
        self.assertEqual(Scout.trend_line({"h1_with": True, "ema_with": True}), "📐 هم‌جهت با H1 و EMA200")
        self.assertEqual(Scout.trend_line({"h1_with": False, "ema_with": True}), "↕️ خلاف روند H1")
        self.assertEqual(Scout.trend_line({"h1_with": True, "ema_with": False}), "↕️ خلاف EMA200")
        self.assertEqual(Scout.trend_line({"h1_with": False, "ema_with": False}), "↕️ خلاف H1 و EMA200")
        self.assertEqual(Scout.trend_line({"h1_with": np.True_, "ema_with": np.True_}), "📐 هم‌جهت با H1 و EMA200")

    def test_missing_keys_skip_silently(self):
        for w in ({}, None, {"h1_with": True}, {"ema_with": False}, "junk"):
            self.assertEqual(Scout.trend_line(w), "")


class Bot(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = make_scout(Path(self._d.name))
        self.sc.admins = {1}
        self.sc.client.get_tick.return_value = NS(time=0, bid=4100.4, ask=4100.7)
        self.sc.client.positions.return_value = []
        self.sc.client.send_market_order.return_value = NS(ok=True, position=77, order=77, deal=0,
                                                           price=4100.7, comment="")

    def tearDown(self):
        self._d.cleanup()

    def events(self):
        with self.sc.journal.open(encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def kv(self, ev):
        return sa._note_kv(ev["note"])

    def tap(self, data):
        self.sc.handle_update({"callback_query": {"from": {"id": 1, "first_name": "Hamid"}, "data": data, "id": "q"}})

    # ---- خط روند
    def test_card_gets_one_trend_line_and_nothing_else_changes(self):
        self.sc._frame = None
        self.sc.alert(row(), 27.0)
        plain = self.sc.tg.sent[-1][0]
        self.sc._frame = frame()
        self.sc.alert(row(), 27.0)
        with_trend = self.sc.tg.sent[-1][0]
        self.assertIn("📐 هم‌جهت با H1 و EMA200", with_trend)
        same = lambda t: t.replace("/yes 2 · /no 2", "/yes 1 · /no 1")     # فقط شمارهٔ هشدار در خط «با متن» فرق دارد
        self.assertEqual(same(with_trend.replace("\n📐 هم‌جهت با H1 و EMA200", "")), plain)
        self.assertNotIn("📐", plain)

    def test_against_h1_on_sell(self):
        self.sc._frame = frame()                      # قیمت بالای H1 و EMA ← فروش خلاف هر دو
        self.sc.alert(row("SELL"), 27.0)
        self.assertIn("↕️ خلاف H1 و EMA200", self.sc.tg.sent[-1][0])

    def test_context_failure_never_blocks_alert(self):
        self.sc._frame = frame()
        with mock.patch("tools.scout_bot.fu.context_flags", side_effect=RuntimeError("boom")):
            self.sc.alert(row(), 27.0)
        self.assertEqual(len(self.sc.pending), 1)
        self.assertNotIn("📐", self.sc.tg.sent[-1][0])
        self.assertIn("alert", [e["event"] for e in self.events()])

    # ---- سه فیلد هشدار
    def test_alert_note_fields(self):
        self.sc.alert(row(), 27.0, news="NFP", late_bars=2)
        ev = [e for e in self.events() if e["event"] == "alert"][-1]
        kv = self.kv(ev)
        self.assertEqual(kv["late_bars"], "2")
        self.assertEqual(kv["news_near"], "1")
        self.assertAlmostEqual(float(kv["spread_sl"]), 27.0 / 600.0, places=3)   # ۲۷ پوینت ÷ ۶۰۰ پوینت
        self.sc.alert(row(), 27.0)
        kv = self.kv([e for e in self.events() if e["event"] == "alert"][-1])
        self.assertEqual((kv["late_bars"], kv["news_near"]), ("0", "0"))

    def test_alert_note_guards(self):
        self.assertNotIn("spread_sl", Scout.alert_note(0, 27.0, 0.0, ""))           # تقسیم بر صفر نه
        self.assertNotIn("spread_sl", Scout.alert_note(0, None, 6.0, ""))
        self.assertEqual(Scout.alert_note("x", 27.0, 6.0, "")[:8], "spread_s")      # late_bars خراب حذف

    # ---- زمان تصمیم و لغزش
    def test_reject_writes_latency(self):
        self.sc.alert(row(), 27.0)
        aid = str(self.sc.n)
        self.sc.pending[aid]["t"] = time.time() - 42
        self.tap(f"a|{aid}|n")
        ev = [e for e in self.events() if e["event"] == "rejected"][-1]
        self.assertAlmostEqual(int(self.kv(ev)["latency_s"]), 42, delta=2)

    def test_approve_writes_latency_and_slip_and_order_unchanged(self):
        self.sc.alert(row(), 27.0)
        aid = str(self.sc.n)
        self.sc.pending[aid]["t"] = time.time() - 9
        self.tap(f"a|{aid}|y")
        args = self.sc.client.send_market_order.call_args
        self.assertEqual((args.args[0], args.args[1]), ("BUY", 0.01))
        self.assertAlmostEqual(self.sc.open[77]["sl"], 4100.7 - 6.0, places=2)      # حد ضرر مثل قبل
        ev = [e for e in self.events() if e["event"] == "opened"][-1]
        kv = self.kv(ev)
        self.assertAlmostEqual(int(kv["latency_s"]), 9, delta=2)
        self.assertEqual(kv["slip"], "70")                                         # ۴۱۰۰.۷ − ۴۱۰۰ = ۷۰ پوینت بدتر
        self.assertEqual(kv["lot"], "0.01")                                        # فیلدهای قبلی سر جایشان
        self.assertIn("by=Hamid", ev["note"])

    def test_sell_slip_sign(self):
        self.sc.alert(row("SELL"), 27.0)
        aid = str(self.sc.n)
        self.sc.client.get_tick.return_value = NS(time=0, bid=4100.5, ask=4100.8)  # بالاتر = بهتر برای فروش
        self.tap(f"a|{aid}|y")
        kv = self.kv([e for e in self.events() if e["event"] == "opened"][-1])
        self.assertEqual(kv["slip"], "-50")

    def test_expire_writes_latency(self):
        self.sc.alert(row(), 27.0)
        aid = str(self.sc.n)
        self.sc.pending[aid]["t"] = time.time() - 1801
        self.sc.expire()
        ev = [e for e in self.events() if e["event"] == "expired"][-1]
        self.assertGreaterEqual(int(self.kv(ev)["latency_s"]), 1800)

    def test_logging_failure_never_blocks_order(self):
        self.sc.alert(row(), 27.0)
        aid = str(self.sc.n)
        with mock.patch.object(Scout, "latency_note", side_effect=RuntimeError("boom")):
            self.tap(f"a|{aid}|y")
        self.assertIn(77, self.sc.open)
        self.assertIn("opened", [e["event"] for e in self.events()])


def write_journal(path, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=JOURNAL_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def m1(path, start="2026-10-06 09:30"):
    idx = pd.date_range(start, periods=len(path), freq="1min")
    c = np.array(path, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 0.1, "low": c - 0.1, "close": c}, index=idx)


class Audit(unittest.TestCase):
    def journal(self, d):
        p = Path(d) / "j.csv"
        base = {"setup": "rejection", "side": "BUY", "bar": "2026-10-06 09:15:00", "price": "4100", "sl": "4094"}
        write_journal(p, [
            dict(base, ts="2026-10-06T09:30:00", event="alert", alert="1"),                       # ردیف قدیمی
            dict(base, ts="2026-10-06T09:31:00", event="opened", alert="1", note="lot=0.01;by=H"),
            dict(base, ts="2026-10-06T09:30:00", event="alert", alert="2",
                 note="late_bars=0;spread_sl=0.040;news_near=0"),
            dict(base, ts="2026-10-06T09:30:20", event="opened", alert="2", note="lot=0.01;by=H;latency_s=20;slip=30"),
            dict(base, ts="2026-10-06T09:30:00", event="alert", alert="3",
                 note="late_bars=1;spread_sl=0.120;news_near=1"),
            dict(base, ts="2026-10-06T09:31:00", event="rejected", alert="3", note="latency_s=60"),
        ])
        return p

    def test_read_alerts_parses_and_old_rows_are_nan(self):
        with tempfile.TemporaryDirectory() as d:
            df = sa.read_alerts(self.journal(d))
        self.assertTrue(np.isnan(df.late_bars[0]) and np.isnan(df.slip[0]) and np.isnan(df.latency_s[0]))
        self.assertEqual((df.late_bars[1], df.spread_sl[1], df.news_near[1], df.latency_s[1], df.slip[1]),
                         (0.0, 0.04, 0.0, 20.0, 30.0))
        self.assertEqual((df.late_bars[2], df.news_near[2], df.latency_s[2]), (1.0, 1.0, 60.0))
        self.assertTrue(np.isnan(df.slip[2]))                                      # رد شده: لغزش ندارد

    def test_report_has_new_slices_with_sane_values(self):
        with tempfile.TemporaryDirectory() as d:
            df = sa.read_alerts(self.journal(d))
        res = sa.audit(df, m1(list(np.linspace(4100, 4110, 61)) + [4110.0] * 200))
        rep = sa.audit_report(res, offset_min=30)
        self.assertEqual([x["key"] for x in rep["weekday"]], ["Tuesday"])
        self.assertEqual(rep["weekday"][0]["fa"], "سه‌شنبه")
        self.assertEqual(rep["weekday"][0]["n"], 3)
        late = {x["key"]: x["n"] for x in rep["late"]}
        self.assertEqual(late, {"on_time": 1, "late": 1})                         # ردیف قدیمی حساب نمی‌شود
        sp = {x["key"]: x["n"] for x in rep["spread"]}
        self.assertEqual(sp, {"<5%": 1, "5-10%": 0, ">=10%": 1})
        self.assertEqual({x["key"]: x["n"] for x in rep["news"]}, {"news": 1, "no_news": 1})
        self.assertEqual(rep["slip"], {"n": 1, "avg": 30.0, "lat_n": 2, "lat_med": 40})
        json.dumps(rep)                                                            # برای داشبورد
        text = sa.summarize(res, offset_min=30)
        for part in ("== BY WEEKDAY", "== ON TIME vs LATE", "== BY SPREAD", "== NEWS NEAR", "slip avg +30.0 pts"):
            self.assertIn(part, text)

    def test_old_journal_and_old_csv_never_crash(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "old.csv"
            write_journal(p, [{"ts": "2026-10-04T10:00:00", "event": "alert", "alert": "1", "side": "SELL",
                               "setup": "x", "bar": "2026-10-04 09:00:00", "price": "4100", "sl": "4108"}])
            res = sa.audit(sa.read_alerts(p), None)
            empty = sa.audit(sa.read_alerts(p).iloc[0:0], None)
        rep = sa.audit_report(res)
        self.assertEqual(rep["slip"]["avg"], None)
        self.assertEqual(sum(x["n"] for x in rep["late"]), 0)
        self.assertIn("slip: no data yet", sa.summarize(res))
        legacy = res.drop(columns=sa.EXTRA_COLS)                                    # CSV قدیمی بدون ستون‌ها
        sa.audit_report(legacy)
        sa.summarize(legacy)
        sa.audit_report(empty)
        sa.summarize(empty)

    def test_weekday_follows_tehran_time(self):
        # جمعه ۲۳:۴۵ سرور (زمستان، ۹۰ دقیقه) ← شنبه ۰۱:۳۰ تهران
        self.assertEqual(sa.tehran_weekday("2026-12-04 23:45:00", offset_min=90), 5)
        self.assertEqual(sa.tehran_weekday("2026-10-06 09:15:00", offset_min=30), 1)       # سه‌شنبه
        self.assertIsNone(sa.tehran_weekday("not a date", offset_min=30))


class Page(unittest.TestCase):
    def test_page_has_new_sections(self):
        for part in ('id="auweek"', 'id="aucond"', "لغزش ورود", "a.weekday", "a.slip"):
            self.assertIn(part, dash.PAGE)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_render_audit_javascript(self):
        page = dash.PAGE.replace("<!--GUIDE-->", dash.GUIDE)
        js = page.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        g = {"n": 6, "first": 0.6, "plus": 3, "minus": 2, "up15": 0.5, "up60": 0.5, "r60": 0.2, "mfe": 1.1, "mae": 0.4}
        rep = {"all": g, "days": 14, "updated": "x", "label": [], "setup": [dict(g, key="rejection")], "hour": [],
               "decision": [], "side": [], "weekday": [dict(g, key="Monday", fa="دوشنبه")],
               "late": [dict(g, key="late", fa="دیرهنگام (کندل قبل)")], "spread": [], "news": [],
               "slip": {"n": 4, "avg": 12.5, "lat_n": 4, "lat_med": 30}}
        harness = r"""
const els={};const mk=id=>els[id]||(els[id]={id,innerHTML:'',textContent:'',value:'',hidden:false,
  setAttribute(){},addEventListener(){},scrollIntoView(){}});
global.document={getElementById:mk,querySelectorAll:()=>[]};
global.window={scrollTo(){}};global.location={hash:'',pathname:'/',search:''};global.history={replaceState(){}};
global.fetch=()=>new Promise(()=>{});global.setInterval=()=>0;
""" + js + r"""
renderAudit(%s);
if(!els.auweek.innerHTML.includes('دوشنبه'))throw new Error('weekday');
if(!els.aucond.innerHTML.includes('دیرهنگام'))throw new Error('cond');
if(!els.autiles.innerHTML.includes('+12.5 پوینت'))throw new Error('slip tile');
if(!els.ausetup.innerHTML.includes('1.10 / 0.40'))throw new Error('mae column');
renderAudit({all:%s});
if(!els.autiles.innerHTML.includes('هنوز داده‌ای نیست'))throw new Error('no slip');
console.log('OK');
""" % (json.dumps(rep), json.dumps(g))
        res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("OK", res.stdout)


if __name__ == "__main__":
    unittest.main()
