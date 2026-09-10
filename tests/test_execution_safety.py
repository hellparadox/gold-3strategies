"""Regression tests: no terminal, credentials, HTTP calls or live orders."""
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from core.database import Database
from core import Settings
from core.risk_manager import RiskConfig, RiskManager, SymbolSpec
from core.mt5_client import MT5Client, MT5Config, MT5ReadError, OrderResult
import core.mt5_client as module
from main_live import LiveBot, TrackedPosition


def broker_result(code=10009, volume=0.03):
    return NS(retcode=code, comment="test", order=123, deal=456, position=123,
              price=100.0, volume=volume)


class BrokerReads(unittest.TestCase):
    def test_errors_are_not_empty_accounts(self):
        c = MT5Client(MT5Config())
        c._guarded = Mock(return_value=None)
        for read in (c.positions, c.open_position_count,
                     lambda: c.position_by_ticket(123),
                     lambda: c.deals_since(datetime.now()),
                     lambda: c.deals_for_position(123),
                     lambda: c.position_id_for_deal(456)):
            with self.subTest(read=read), self.assertRaises(MT5ReadError):
                read()

    def test_successfully_empty_is_valid(self):
        c = MT5Client(MT5Config())
        c._guarded = Mock(return_value=())
        self.assertEqual(c.positions(), [])
        self.assertEqual(c.deals_for_position(123), [])
        self.assertIsNone(c.position_by_ticket(123))
        self.assertIsNone(c.position_id_for_deal(456))

    def test_position_history_includes_manual_exit(self):
        c = MT5Client(MT5Config())
        entry = NS(position_id=123, magic=c.magic)
        manual = NS(position_id=123, magic=0)
        c._guarded = Mock(return_value=(entry, manual, NS(position_id=999)))
        self.assertEqual(c.deals_for_position(123), [entry, manual])

    def test_position_id_resolves_from_deal(self):
        c = MT5Client(MT5Config())
        c._guarded = Mock(return_value=(NS(position_id=789),))
        self.assertEqual(c.position_id_for_deal(456), 789)


class OrderSending(unittest.TestCase):
    def setUp(self):
        self.api = NS(ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1, TRADE_ACTION_DEAL=1,
                      ORDER_TIME_GTC=0, TRADE_RETCODE_DONE=10009,
                      TRADE_RETCODE_REQUOTE=10004, TRADE_RETCODE_PRICE_CHANGED=10020,
                      TRADE_RETCODE_PRICE_OFF=10021, TRADE_RETCODE_INVALID_FILL=10030,
                      order_send=Mock(), last_error=lambda: (-1, "test"))
        self.patches = [patch.object(module, "mt5", self.api),
                        patch.object(module, "MT5_AVAILABLE", True),
                        patch.object(module.time, "sleep")]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.c = MT5Client(MT5Config())
        self.c.ensure_connected = Mock(return_value=True)
        self.c.symbol_info = Mock(return_value=NS(digits=2, volume_step=.01, volume_min=.01, volume_max=10))
        self.c.get_tick = Mock(return_value=NS(bid=100., ask=100.08))
        self.c._filling_modes = Mock(return_value=[0, 1, 2])

    def test_unknown_never_retries(self):
        for response in (None, RuntimeError("IPC"), broker_result(10008), broker_result(10012),
                         broker_result(10028), broker_result(10031)):
            with self.subTest(response=response):
                self.api.order_send.reset_mock()
                self.api.order_send.side_effect = [response, broker_result()]
                result = self.c.send_market_order("BUY", .03)
                self.assertTrue(result.uncertain)
                self.assertFalse(result.ok)
                self.assertEqual(self.api.order_send.call_count, 1)

    def test_partial_fill_is_not_resent(self):
        self.api.order_send.return_value = broker_result(10010, .01)
        result = self.c.send_market_order("BUY", .03)
        self.assertTrue(result.ok)
        self.assertTrue(result.uncertain)
        self.assertEqual(result.volume, .01)
        self.assertEqual(self.api.order_send.call_count, 1)

    def test_explicit_requote_can_retry(self):
        self.api.order_send.side_effect = [broker_result(10004), broker_result()]
        self.assertTrue(self.c.send_market_order("BUY", .03).ok)
        self.assertEqual(self.api.order_send.call_count, 2)

    def test_invalid_fill_can_fallback(self):
        self.api.order_send.side_effect = [broker_result(10030), broker_result()]
        self.assertTrue(self.c.send_market_order("BUY", .03).ok)
        self.assertEqual(self.api.order_send.call_count, 2)

    def test_rejected_order_is_not_retried(self):
        self.api.order_send.return_value = broker_result(10019)
        self.assertFalse(self.c.send_market_order("BUY", .03).ok)
        self.assertEqual(self.api.order_send.call_count, 1)

    def test_close_unknown_never_falls_through_filling_modes(self):
        self.c.position_by_ticket = Mock(return_value=NS(type=0, symbol="XAUUSD", volume=.03))
        self.api.order_send.return_value = None
        result = self.c.close_position(123)
        self.assertTrue(result.uncertain)
        self.assertEqual(self.api.order_send.call_count, 1)


class DurableJournal(unittest.TestCase):
    def test_restart_preserves_unknown_and_consumed_signals(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "test.db")
            with Database(path) as db:
                self.assertTrue(db.claim_execution("account", "first"))
            with Database(path) as db:
                self.assertFalse(db.claim_execution("account", "second"))
                self.assertFalse(db.claim_execution("account", "first"))
                db.finish_execution("first", "reviewed", "broker checked")
                self.assertFalse(db.claim_execution("account", "first"))
                self.assertTrue(db.claim_execution("account", "second"))

    def test_connections_cannot_claim_twice(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "test.db")
            with Database(path) as a, Database(path) as b:
                self.assertTrue(a.claim_execution("account", "same"))
                self.assertFalse(b.claim_execution("account", "same"))
                a.finish_execution("same", "accepted")
                self.assertFalse(b.claim_execution("account", "same"))

    def test_unrelated_account_is_not_blocked(self):
        with Database(":memory:") as db:
            db.claim_execution("a", "a1")
            self.assertTrue(db.claim_execution("b", "b1"))


def deal(entry, volume, profit=0., magic=553311, time=1):
    return NS(position_id=123, entry=entry, volume=volume, profit=profit,
              commission=0., swap=0., fee=0., price=103., ticket=time,
              time_msc=time, magic=magic, symbol="XAUUSD", order=123)


class Reconciliation(unittest.TestCase):
    def setUp(self):
        self.bot = LiveBot.__new__(LiveBot)
        b = self.bot
        b._lock = threading.RLock()
        b.current_atr = 1.
        b.tracked = {123: TrackedPosition(123, "BUY", .03, 100., 98., 103., 1., position_id=123)}
        b.client = Mock()
        b.client.positions.return_value = []
        b.client.position_id_for_deal.return_value = 123
        b.client.deals_for_position.return_value = []
        b.db = Mock()
        b.telegram = Mock()
        b._sync_daily = Mock()

    def test_position_read_failure_preserves_state(self):
        self.bot.client.positions.side_effect = MT5ReadError("IPC")
        with self.assertRaises(MT5ReadError):
            self.bot._sync_positions()
        self.assertIn(123, self.bot.tracked)
        self.bot.db.close_signal.assert_not_called()

    def test_delayed_history_waits_then_closes_once(self):
        b = self.bot
        self.assertFalse(b._sync_positions())
        self.assertIn(123, b.tracked)
        b.db.close_signal.assert_not_called()
        b.client.deals_for_position.return_value = [deal(0, .03), deal(1, .03, 9, magic=0, time=2)]
        self.assertTrue(b._sync_positions())
        self.assertTrue(b._sync_positions())
        b.db.close_signal.assert_called_once_with(123, 103., 9., "WIN")
        b.telegram.broadcast_close.assert_called_once()

    def test_partial_history_is_not_final_close(self):
        self.bot.client.deals_for_position.return_value = [deal(0, .03), deal(1, .01, 2)]
        self.assertFalse(self.bot._sync_positions())
        self.bot.db.close_signal.assert_not_called()

    def test_history_failure_preserves_state(self):
        self.bot.client.deals_for_position.side_effect = MT5ReadError("history")
        with self.assertRaises(MT5ReadError):
            self.bot._sync_positions()
        self.assertIn(123, self.bot.tracked)
        self.bot.db.close_signal.assert_not_called()

    def test_account_failure_does_not_commit_closure(self):
        self.bot.client.deals_for_position.return_value = [deal(0, .03), deal(1, .03, 9)]
        self.bot._sync_daily.side_effect = MT5ReadError("account")
        with self.assertRaises(MT5ReadError):
            self.bot._sync_positions()
        self.assertIn(123, self.bot.tracked)
        self.bot.db.close_signal.assert_not_called()


class DailyGuard(unittest.TestCase):
    def test_restart_counts_unique_entries_not_partial_fills(self):
        b = LiveBot.__new__(LiveBot)
        b._lock = threading.RLock()
        b.daily_date = None
        b.max_daily_loss_pct = 10
        b.max_daily_trades = 10
        b.client = Mock()
        b.client.symbol = "XAUUSD"
        b.client.server_time.return_value = datetime(2026, 9, 9, 12)
        b.client.account_info.return_value = NS(balance=533.)
        entries = [deal(0, .01) for _ in range(10)]
        for i, d in enumerate(entries):
            d.position_id = i + 1
        b.client.deals_since.return_value = entries + [entries[0]]
        b._sync_daily()
        self.assertEqual(b.daily_trades, 10)
        self.assertTrue(b._daily_guard()[0])


class LiveEntryIntegration(unittest.TestCase):
    def setUp(self):
        b = self.bot = LiveBot.__new__(LiveBot)
        b._lock = threading.RLock()
        b.db = Database(":memory:")
        self.addCleanup(b.db.close)
        b.risk_config = RiskConfig.from_settings(Settings.load("config/settings_ichimoku.yaml"))
        b.risk = RiskManager(b.risk_config, SymbolSpec.gold_default())
        b.tracked = {}; b.signals_sent = 0; b.daily_trades = 0; b.telegram = None
        b.client = Mock()
        b.client.config = MT5Config()
        b.client.symbol = "XAUUSD"; b.client.magic = 553311
        b.client.symbol_info.return_value = NS(name="XAUUSD", point=.01, digits=2,
            trade_tick_value=1., trade_tick_size=.01, trade_contract_size=100.,
            volume_min=.01, volume_max=100., volume_step=.01, trade_stops_level=0)
        b.client.get_tick.return_value = NS(bid=100.,ask=100.08)
        b.client.account_info.return_value = NS(balance=533.,login=123,server="demo")
        b.client.positions.return_value = []
        self.signal = NS(strategy="ichimoku_m15",ref_time="2026-09-09 12:00",side="BUY",is_long=True,atr=1.,reason="test")

    def test_unknown_order_blocks_later_signal(self):
        b = self.bot
        b.client.send_market_order.return_value = OrderResult(False, uncertain=True)
        self.assertIsNone(b._open_trade(self.signal))
        self.signal.ref_time = "2026-09-09 12:15"
        self.assertIsNone(b._open_trade(self.signal))
        b.client.send_market_order.assert_called_once()
        self.assertEqual(len(b.db.unresolved_executions()),1)

    def test_confirmed_order_cannot_repeat_same_signal(self):
        b = self.bot
        b.client.send_market_order.return_value = OrderResult(True,order=123,position=123,price=100.08,volume=.03)
        self.assertIsNotNone(b._open_trade(self.signal))
        self.assertIsNone(b._open_trade(self.signal))
        b.client.send_market_order.assert_called_once()

    def test_partial_fill_records_actual_volume_and_holds(self):
        b = self.bot
        b.client.send_market_order.return_value = OrderResult(True,order=123,position=123,price=100.08,volume=.01,uncertain=True)
        levels = b._open_trade(self.signal)
        self.assertEqual(levels.lot,.01)
        self.assertEqual(b.tracked[123].volume,.01)
        self.assertEqual(b.db.recent_signals()[0]["lot"],.01)
        self.assertEqual(len(b.db.unresolved_executions()),1)

    def test_confirmed_order_survives_followup_position_read_error(self):
        b = self.bot
        b.client.send_market_order.return_value = OrderResult(True,order=123,position=123,price=100.08,volume=.03)
        b.client.positions.side_effect = MT5ReadError("IPC")
        self.assertIsNotNone(b._open_trade(self.signal))
        self.assertIn(123,b.tracked)
        self.assertEqual(len(b.db.recent_signals()),1)

    def test_deal_only_result_resolves_real_position(self):
        b = self.bot
        b.client.send_market_order.return_value = OrderResult(True, order=456, deal=777,
                                                               price=100.08, volume=.03)
        b.client.position_id_for_deal.return_value = 789
        levels = b._open_trade(self.signal)
        self.assertIsNotNone(levels)
        self.assertIn(789, b.tracked)

    def test_confirmed_but_unresolved_position_blocks_without_false_ticket(self):
        b = self.bot
        b.client.send_market_order.return_value = OrderResult(True, order=456, deal=777,
                                                               price=100.08, volume=.03)
        b.client.position_id_for_deal.return_value = None
        self.assertIsNone(b._open_trade(self.signal))
        self.assertEqual(b.tracked, {})
        self.assertEqual(len(b.db.recent_signals()), 0)
        self.assertEqual(len(b.db.unresolved_executions()), 1)


if __name__ == "__main__":
    unittest.main()
