"""داشبورد روی لپ‌تاپ ضعیف باید باز بماند بدون این‌که CPU را بسوزاند: هیچ انیمیشن همیشگیِ پرهزینه،
پس‌زمینهٔ WebGL فقط چند لحظه حرکت می‌کند، آپدیت هر ۵ ثانیه و در تب مخفی هیچ، جدول معاملات حداکثر ۲۰۰ ردیف،
و هر بخش فقط وقتی عوض شده دوباره نوشته می‌شود."""
import json
import re
import shutil
import subprocess
import unittest

from tools import scout_dashboard as dash
from tests.test_scout_trades_table import trade

JS = dash.PAGE.replace("<!--GUIDE-->", dash.GUIDE).split("<script>", 1)[1].rsplit("</script>", 1)[0]
CSS = dash.PAGE.split("<style>", 1)[1].split("</style>", 1)[0]


class Static(unittest.TestCase):
    def test_no_endless_animation_except_hover_and_tape(self):
        lines = [ln for ln in CSS.splitlines() if "infinite" in ln]
        for ln in lines:
            self.assertTrue(":hover" in ln or ".tape-in" in ln, ln)

    def test_polling_and_visibility(self):
        self.assertIn("const POLL_MS=5000;", JS)
        self.assertNotIn("setInterval(tick", JS)
        self.assertIn("visibilitychange", JS)
        self.assertIn("visibilityState", JS)
        self.assertIn("if(busy)return;", JS)                      # درخواست‌ها روی هم سوار نمی‌شوند

    def test_webgl_is_not_a_forever_loop(self):
        self.assertIn("SCALE=.5", JS)
        self.assertIn("if(now<until||pulse>.02)raf=requestAnimationFrame(loop)", JS)
        self.assertIn("now-pulseT<30000", JS)

    def test_sections_go_through_change_check(self):
        self.assertEqual(re.findall(r"\$\('[A-Za-z0-9]+'\)\.innerHTML=", JS), [])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class Browserless(unittest.TestCase):
    HARNESS = r"""
const els={};let writes={};const mk=id=>els[id]||(els[id]=(()=>{const o={id,textContent:'',value:'',hidden:false,
  setAttribute(){},addEventListener(){},scrollIntoView(){}};let h='';
  Object.defineProperty(o,'innerHTML',{get:()=>h,set:v=>{h=v;writes[id]=(writes[id]||0)+1;}});return o;})());
global.document={getElementById:mk,querySelectorAll:()=>[],documentElement:{classList:{add(){},toggle(){}}}};
global.addEventListener=()=>{};global.scrollY=0;global.setTimeout=()=>0;global.clearTimeout=()=>{};
global.window={scrollTo(){}};global.location={hash:'',pathname:'/',search:''};global.history={replaceState(){}};
global.fetch=()=>new Promise(()=>{});global.setInterval=()=>0;
"""

    def run_js(self, body):
        res = subprocess.run(["node", "-"], input=self.HARNESS + JS + body, capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("OK", res.stdout)

    def test_trades_table_capped_and_skipped_when_unchanged(self):
        ev = []
        for i in range(260):
            ev += trade(str(5000 + i), 1 + i // 20, "10:00", "10:30", profit=5.0 if i % 2 else -4.0,
                        note_open="lot=0.01;by=Groktraid")
        rows = dash.build_trades(ev)
        self.run_js(r"""
const rows=%s;
renderTrades(rows);
const b=els.trbody.innerHTML, n=(b.match(/<tr><td class="mono/g)||[]).length;
if(n!==200)throw new Error('rows '+n);
if(!b.includes('260'))throw new Error('cap note');
if(!els.trtiles.innerHTML.includes('260'))throw new Error('tiles count all trades');
const w=writes.trbody; renderTrades(rows);
if(writes.trbody!==w)throw new Error('rewritten without change');
TR.res='win'; renderTrades(rows);
if(writes.trbody===w)throw new Error('filter must re-render');
console.log('OK');
""" % json.dumps(rows))

    def test_section_written_only_when_html_changes(self):
        self.run_js(r"""
H('events').html='<li>a</li>'; H('events').html='<li>a</li>';
if(writes.events!==1)throw new Error('dup write');
H('events').html='<li>b</li>'; if(writes.events!==2)throw new Error('change lost');
console.log('OK');
""")


if __name__ == "__main__":
    unittest.main()
