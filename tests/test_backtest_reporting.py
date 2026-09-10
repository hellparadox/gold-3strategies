import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from backtest.reporting import export_run
from tests.test_backtest_timing import engine


class Reporting(unittest.TestCase):
    def test_export_has_inputs_parameters_and_trades(self):
        e = engine("config/settings.yaml")
        idx = pd.date_range("2026-01-01", periods=160, freq="5min")
        frame = pd.DataFrame(dict(open=100., high=101., low=99., close=100., tick_volume=1), index=idx)
        result = e.run(frame)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "run"
            export_run(output, e, result, {"m5": frame}, save_bars=True)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["datasets"]["m5"]["rows"], 160)
            self.assertEqual(len(report["datasets"]["m5"]["sha256"]), 64)
            self.assertEqual(report["strategy"], "orb_gold")
            self.assertTrue((output / "trades.csv").is_file())
            self.assertTrue((output / "m5.csv").is_file())
            with self.assertRaises(FileExistsError):
                export_run(output, e, result, {"m5": frame})


if __name__ == "__main__":
    unittest.main()
