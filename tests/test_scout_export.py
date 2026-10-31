"""/export: دفتر کامل اسکات به‌صورت zip در گروه (برای بررسی بیرونی، بدون کپی از Remote Desktop)."""
import io
import zipfile

from tests.test_scout_text_commands import HUMAN, Base


class Export(Base):
    def setUp(self):
        super().setUp()
        self.docs = []
        self.sc.tg.send_document = lambda data, name, cap="", reply_to=None: self.docs.append((data, name, cap)) or 77

    def test_human_gets_zipped_journal(self):
        self.sc.log(event="alert", alert="1", side="BUY", price=4100.0, sl=4090.0, setup="engulfing")
        self.sc.log(event="opened", alert="1", ticket="5", side="BUY", price=4100.2, note="lot=0.01;by=Hamid")
        raw = self.sc.journal.read_bytes()
        self.human("/export")
        self.assertEqual(len(self.docs), 1)
        data, name, cap = self.docs[0]
        self.assertTrue(name.startswith("scout_journal_") and name.endswith(".zip"))
        z = zipfile.ZipFile(io.BytesIO(data))
        self.assertIsNone(z.testzip())
        self.assertEqual(z.read(self.sc.journal.name), raw)          # دقیقاً همان دفتر، نه نسخهٔ خالی
        self.assertIn("سطر", cap)
        self.assertIn("export", [e["event"] for e in self.events()])

    def test_bots_cannot_export(self):
        self.sc.log(event="alert", alert="1", side="BUY", price=4100.0, sl=4090.0, setup="engulfing")
        self.bot("/export@HellTreid_bot")
        self.assertEqual(self.docs, [])
        self.assertIn("فقط برای آدم‌هاست", self.last())

    def test_missing_journal(self):
        if self.sc.journal.exists():
            self.sc.journal.unlink()
        self.human("/export")
        self.assertEqual(self.docs, [])
        self.assertIn("هنوز ساخته نشده", self.last())

    def test_menu_lists_export(self):
        from tools import scout_control as ctl
        self.assertIn("export", [c["command"] for c in ctl.MENU_COMMANDS])
        self.assertEqual(HUMAN, 1)
