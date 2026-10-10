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


if __name__ == "__main__":
    unittest.main()
