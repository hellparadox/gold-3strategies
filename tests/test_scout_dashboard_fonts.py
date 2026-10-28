"""داشبورد نباید منتظر Google Fonts بماند (در ایران بدون فیلترشکن باز نمی‌شود و صفحه روی «در حال دریافت» می‌ماند)."""
import re
import unittest

from tools import scout_dashboard as dash


class Fonts(unittest.TestCase):
    def test_font_stylesheet_does_not_block(self):
        links = re.findall(r"<link[^>]+fonts\.googleapis\.com[^>]*>", dash.PAGE)
        self.assertEqual(len(links), 1)
        self.assertIn('media="print"', links[0])
        self.assertIn("onload=\"this.media='all'\"", links[0])
        self.assertIn("Tahoma", dash.PAGE)                 # فونت جایگزین فارسی


class Layout(unittest.TestCase):
    def test_people_table_keeps_pnl_visible_on_phone(self):
        """در موبایل جدول نفرات نباید برای دیدن «سود و ضرر» اسکرول افقی بخواهد."""
        self.assertIn("#pptab{min-width:0}", dash.PAGE)
        self.assertIn('id="pptab"', dash.PAGE)
        # ستون‌های پنهان‌شده در موبایل: برد، میانگین هر معامله، بهترین، بدترین — سود و ضرر (۵) و R (۷) می‌مانند
        for n in (4, 6, 8, 9):
            self.assertIn(f"#pptab th:nth-child({n})", dash.PAGE)
        for n in (5, 7):
            self.assertNotIn(f"#pptab th:nth-child({n})", dash.PAGE)

    def test_guide_has_a_title_and_page_is_one_document(self):
        page = dash.PAGE.replace("<!--GUIDE-->", dash.GUIDE)
        self.assertIn("راهنمای اسکات", page)
        self.assertEqual(page.count("<!doctype html>"), 1)
        self.assertEqual(page.count("</html>"), 1)
        self.assertNotIn("<!--GUIDE-->", page)


    def test_effects_are_self_contained(self):
        """افکت‌ها (WebGL طلای مذاب، نوار معاملات) هیچ اسکریپت یا فایل بیرونی ندارند؛ در ایران CDN ممکن است باز نشود."""
        page = dash.PAGE
        self.assertNotIn("<script src", page)
        self.assertEqual(re.findall(r'src="https?://', page), [])
        self.assertIn("getContext('webgl'", page)
        self.assertIn('id="gold"', page)
        self.assertIn('id="tapein"', page)
        self.assertIn("prefers-reduced-motion", page)


if __name__ == "__main__":
    unittest.main()
