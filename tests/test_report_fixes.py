"""Reporting fixes: shadow P/L sign + verdict, and daily open-position count.

Bug 1 — SELL shadow used (sl - entry) so a winning SL exit printed negative.
Bug 2 — daily open_now = opened - trades went negative when a prior-day
position closed today; now broker positions_get wins, max(0, ·) is fallback.
"""
import threading
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

from main_live import (
    TrackedPosition,
    LiveBot,
    shadow_pl,
    shadow_outcome,
    shadow_verdict_text,
    format_shadow_verdict,
)
from core.daily_digest import build_daily_stats


class ShadowPL(unittest.TestCase):
    def test_sell_to_sl_is_positive_win(self):
        # Reported example: entry 4346.88, SL 4342.08 → held = +$4.80 WIN
        pl = shadow_pl("SELL", 4346.88, 4342.08, 0.01)
        self.assertAlmostEqual(pl, 4.80, places=2)
        self.assertEqual(shadow_outcome(pl), "WIN")

    def test_buy_to_sl_is_negative_loss(self):
        # BUY entry 4346.88, SL 4342.08 (below entry) → held = -$4.80 LOSS
        pl = shadow_pl("BUY", 4346.88, 4342.08, 0.01)
        self.assertAlmostEqual(pl, -4.80, places=2)
        self.assertEqual(shadow_outcome(pl), "LOSS")

    def test_sell_to_tp_is_positive_win(self):
        # SELL entry 4351.68, TP 4346.88 → held = +$4.80 WIN
        pl = shadow_pl("SELL", 4351.68, 4346.88, 0.01)
        self.assertAlmostEqual(pl, 4.80, places=2)
        self.assertEqual(shadow_outcome(pl), "WIN")

    def test_sell_to_sl_as_requested(self):
        # User's exact case 3: SELL 4346.88 → 4351.68 = -4.80 LOSS
        pl = shadow_pl("SELL", 4346.88, 4351.68, 0.01)
        self.assertAlmostEqual(pl, -4.80, places=2)
        self.assertEqual(shadow_outcome(pl), "LOSS")


class ShadowVerdict(unittest.TestCase):
    def test_manual_close_better(self):
        self.assertEqual(
            shadow_verdict_text(9.94, 4.80), "بستن دستی بهتر بود"
        )

    def test_holding_better(self):
        self.assertEqual(
            shadow_verdict_text(2.00, 8.00), "نگه داشتن بهتر بود"
        )

    def test_format_prints_both_numbers(self):
        line = format_shadow_verdict(9.94, 4.80)
        self.assertIn("9.94", line)
        self.assertIn("4.80", line)
        self.assertIn("بستن دستی بهتر بود", line)


class DailyOpenCount(unittest.TestCase):
    def _db(self, rows):
        db = Mock()
        db.recent_signals.return_value = rows
        return db

    def test_negative_derived_clamps_to_zero(self):
        # Prior-day open closed today: opened=0, trades=1 → was -1, now 0
        rows = [
            {
                "created_at": "2026-09-20 10:00:00",
                "closed_at": "2026-09-21 11:00:00",
                "profit": -1.0,
            }
        ]
        stats = build_daily_stats(self._db(rows), day=date(2026, 9, 21))
        self.assertEqual(stats["open_now"], 0)
        self.assertEqual(stats["open_now_source"], "derived")

    def test_broker_count_wins(self):
        rows = [
            {
                "created_at": "2026-09-20 10:00:00",
                "closed_at": "2026-09-21 11:00:00",
                "profit": -1.0,
            }
        ]
        stats = build_daily_stats(
            self._db(rows), day=date(2026, 9, 21), open_positions=2
        )
        self.assertEqual(stats["open_now"], 2)
        self.assertEqual(stats["open_now_source"], "broker")

    def test_broker_zero_is_zero(self):
        stats = build_daily_stats(self._db([]), day=date(2026, 9, 21), open_positions=0)
        self.assertEqual(stats["open_now"], 0)


class ManualCloseFillsShadow(unittest.TestCase):
    def test_handle_closed_position_sets_actual_profit(self):
        bot = LiveBot.__new__(LiveBot)
        bot._lock = threading.RLock()
        bot.tracked = {
            123: TrackedPosition(
                123, "SELL", 0.01, 4346.88, 4342.08, 4356.88, 10.0, position_id=123
            )
        }
        bot._shadows = [
            {
                "ticket": 123,
                "side": "SELL",
                "entry": 4346.88,
                "sl": 4342.08,
                "tp": 4356.88,
                "volume": 0.01,
                "actual_profit": None,
            }
        ]
        bot.client = Mock()
        bot.client.deals_for_position.return_value = [
            SimpleNamespace(entry=0, volume=0.01, price=4346.88, ticket=1,
                            time_msc=1, profit=0.0, commission=0.0, swap=0.0, fee=0.0),
            SimpleNamespace(entry=1, volume=0.01, price=4342.08, ticket=2,
                            time_msc=2, profit=4.80, commission=0.0, swap=0.0, fee=0.0),
        ]
        bot.db = Mock()
        bot.telegram = Mock()
        bot._sync_daily = Mock()

        ok = bot._handle_closed_position(123)
        self.assertTrue(ok)
        self.assertEqual(bot._shadows[0]["actual_profit"], 4.80)
        bot.db.close_signal.assert_called_once()
        bot._sync_daily.assert_called_once()
        self.assertNotIn(123, bot.tracked)


if __name__ == "__main__":
    unittest.main()
