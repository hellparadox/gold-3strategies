"""Windows PowerShell 5.1 فایل .ps1 بدون BOM را ANSI می‌خواند: کامنت فارسی خراب می‌شود و بایت‌هایی مثل 0x94
به «”» تبدیل می‌شوند که برای PowerShell علامت رشته است ← کل اسکریپت parse نمی‌شود (watchdog هرگز اجرا نشد)."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Ps1Encoding(unittest.TestCase):
    def test_non_ascii_ps1_files_have_utf8_bom(self):
        try:
            files = subprocess.run(["git", "ls-files", "*.ps1"], cwd=ROOT, capture_output=True, text=True,
                                   check=True).stdout.split()
        except (OSError, subprocess.CalledProcessError):
            files = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.ps1")]
        self.assertTrue(files)
        for f in files:
            raw = (ROOT / f).read_bytes()
            if any(b > 127 for b in raw):
                self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), f"{f}: UTF-8 BOM لازم است")
                raw[3:].decode("utf-8")                     # همچنان UTF-8 معتبر


if __name__ == "__main__":
    unittest.main()
