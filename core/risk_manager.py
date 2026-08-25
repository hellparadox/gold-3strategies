"""Position sizing, SL/TP placement, break-even and ATR trailing engine.

The class is deliberately free of any MetaTrader5 import: it operates on a
plain :class:`SymbolSpec` value object.  That is what lets the backtest engine
reuse the exact same math as the live loop.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional, Tuple

from loguru import logger

__all__ = [
    "ManageAction",
    "PositionView",
    "RiskConfig",
    "RiskManager",
    "SymbolSpec",
    "TradeLevels",
]

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class SymbolSpec:
    """Broker contract specification, decoupled from the MT5 objects."""

    name: str = "XAUUSD"
    digits: int = 2
    point: float = 0.01
    tick_size: float = 0.01
    tick_value: float = 1.0        # account currency per tick per 1.00 lot
    contract_size: float = 100.0
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    stops_level_points: float = 0.0

    @classmethod
    def from_mt5(cls, info: Any) -> "SymbolSpec":
        tick_value = float(getattr(info, "trade_tick_value_loss", 0.0) or 0.0)
        if tick_value <= 0.0:
            tick_value = float(getattr(info, "trade_tick_value", 0.0) or 0.0)
        tick_size = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
        if tick_size <= 0.0:
            tick_size = float(getattr(info, "point", 0.01) or 0.01)
        contract_size = float(getattr(info, "trade_contract_size", 100.0) or 100.0)
        # Some brokers (notably public demo servers) report a bogus tick_value.
        # Cross-check against contract-size math and prefer the computation when
        # the reported value is implausible (off by more than 2x / 0.5x), which
        # also leaves room for legitimate quote-currency conversion drift.
        expected_tick_value = tick_size * contract_size
        plausible = 0.5 * expected_tick_value <= tick_value <= 2.0 * expected_tick_value
        if tick_value <= 0.0 or (expected_tick_value > 0.0 and not plausible):
            logger.warning(
                "broker tick_value {:.4f} implausible vs tick_size*contract_size "
                "{:.4f}; using computed value (sizing guard)",
                tick_value, expected_tick_value,
            )
            tick_value = expected_tick_value
        return cls(
            name=str(getattr(info, "name", "XAUUSD")),
            digits=int(getattr(info, "digits", 2)),
            point=float(getattr(info, "point", 0.01) or 0.01),
            tick_size=tick_size,
            tick_value=tick_value,
            contract_size=float(getattr(info, "trade_contract_size", 100.0) or 100.0),
            volume_min=float(getattr(info, "volume_min", 0.01) or 0.01),
            volume_max=float(getattr(info, "volume_max", 100.0) or 100.0),
            volume_step=float(getattr(info, "volume_step", 0.01) or 0.01),
            stops_level_points=float(getattr(info, "trade_stops_level", 0.0) or 0.0),
        )

    @classmethod
    def gold_default(cls) -> "SymbolSpec":
        """Standard XAUUSD contract, used by the backtester and unit tests."""
        return cls()

    def money_per_lot(self, price_distance: float) -> float:
        """Account-currency P/L for a 1.00 lot move of ``price_distance``."""
        if self.tick_size <= 0.0:
            return 0.0
        return (abs(float(price_distance)) / self.tick_size) * self.tick_value

    def points(self, price_distance: float) -> float:
        return abs(float(price_distance)) / self.point if self.point else 0.0

    def price_from_points(self, points: float) -> float:
        return float(points) * self.point


@dataclass
class RiskConfig:
    """All tunables from the ``risk:`` section of settings.yaml."""

    base_balance: float = 533.0
    risk_percent: float = 1.0
    min_lot: float = 0.01
    max_lot: float = 0.05
    max_positions_per_symbol: int = 1
    atr_period: int = 14
    sl_atr_multiplier: float = 2.0
    tp_atr_multiplier: float = 2.0
    breakeven_trigger_atr: float = 1.0
    breakeven_buffer_points: float = 10.0
    trailing_trigger_atr: float = 1.5
    trailing_distance_atr: float = 1.2
    commission_per_lot: float = 6.0
    min_sl_points: float = 60.0
    include_commission_in_risk: bool = True

    @classmethod
    def from_settings(cls, settings: Any) -> "RiskConfig":
        return cls(
            base_balance=float(settings.get("risk.base_balance", 533.0)),
            risk_percent=float(settings.get("risk.risk_percent", 1.0)),
            min_lot=float(settings.get("risk.min_lot", 0.01)),
            max_lot=float(settings.get("risk.max_lot", 0.05)),
            max_positions_per_symbol=int(settings.get("risk.max_positions_per_symbol", 1)),
            atr_period=int(settings.get("risk.atr_period", 14)),
            sl_atr_multiplier=float(settings.get("risk.sl_atr_multiplier", 2.0)),
            tp_atr_multiplier=float(settings.get("risk.tp_atr_multiplier", 2.0)),
            breakeven_trigger_atr=float(settings.get("risk.breakeven_trigger_atr", 1.0)),
            breakeven_buffer_points=float(settings.get("risk.breakeven_buffer_points", 10.0)),
            trailing_trigger_atr=float(settings.get("risk.trailing_trigger_atr", 1.5)),
            trailing_distance_atr=float(settings.get("risk.trailing_distance_atr", 1.2)),
            commission_per_lot=float(settings.get("risk.commission_per_lot", 6.0)),
            min_sl_points=float(settings.get("risk.min_sl_points", 60.0)),
            include_commission_in_risk=bool(
                settings.get("risk.include_commission_in_risk", True)
            ),
        )


@dataclass(frozen=True)
class TradeLevels:
    """Fully resolved entry package, ready to be sent to the broker."""

    side: Side
    entry: float
    sl: float
    tp: float
    lot: float
    atr: float
    sl_points: float
    tp_points: float
    risk_money: float
    reward_money: float
    rr: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "side": self.side, "entry": self.entry, "sl": self.sl, "tp": self.tp,
            "lot": self.lot, "atr": self.atr, "sl_points": self.sl_points,
            "tp_points": self.tp_points, "risk_money": self.risk_money,
            "reward_money": self.reward_money, "rr": self.rr,
        }


@dataclass
class PositionView:
    """Minimal position projection shared by live positions and backtest trades."""

    ticket: int
    side: Side
    volume: float
    price_open: float
    sl: float
    tp: float
    breakeven_done: bool = False
    trailing_active: bool = False

    @classmethod
    def from_mt5(cls, position: Any) -> "PositionView":
        return cls(
            ticket=int(position.ticket),
            side="BUY" if int(position.type) == 0 else "SELL",
            volume=float(position.volume),
            price_open=float(position.price_open),
            sl=float(position.sl or 0.0),
            tp=float(position.tp or 0.0),
        )


@dataclass(frozen=True)
class ManageAction:
    """Instruction emitted by the trailing/break-even engine."""

    kind: Literal["breakeven", "trailing"]
    ticket: int
    new_sl: float
    old_sl: float
    reason: str

    @property
    def emoji(self) -> str:
        return "\U0001F6E1️" if self.kind == "breakeven" else "\U0001F4C8"


class RiskManager:
    """Sizing + protective-stop brain. Stateless apart from the config."""

    def __init__(self, config: RiskConfig, spec: Optional[SymbolSpec] = None) -> None:
        self.config = config
        self.spec = spec or SymbolSpec.gold_default()

    def update_spec(self, spec: SymbolSpec) -> None:
        self.spec = spec

    # ------------------------------------------------------------- rounding
    def _round_price(self, price: float) -> float:
        return round(float(price), self.spec.digits)

    def round_volume(self, volume: float) -> float:
        step = self.spec.volume_step or 0.01
        steps = math.floor((float(volume) + step * 1e-6) / step)
        vol = max(1, steps) * step
        decimals = 2
        step_str = f"{step:.10f}".rstrip("0")
        if "." in step_str:
            decimals = max(0, len(step_str.split(".")[1]))
        return round(vol, decimals)

    # ------------------------------------------------------------- distances
    def sl_distance(self, atr_value: float) -> float:
        """SL distance in price terms, floored by the broker stop level."""
        raw = float(atr_value) * self.config.sl_atr_multiplier
        floor_points = max(self.config.min_sl_points, self.spec.stops_level_points + 5.0)
        return max(raw, self.spec.price_from_points(floor_points))

    def tp_distance(self, atr_value: float) -> float:
        return float(atr_value) * self.config.tp_atr_multiplier

    # ---------------------------------------------------------------- sizing
    def calculate_lot(self, balance: float, sl_price_distance: float) -> Tuple[float, float]:
        """Return ``(lot, risk_money)`` for a 1%-of-balance risk budget.

        The lot is quantised down to the volume step and hard-clamped to the
        configured 0.01 .. 0.05 band (then to the broker's own band).
        """
        bal = float(balance) if balance and balance > 0 else self.config.base_balance
        budget = bal * (self.config.risk_percent / 100.0)
        loss_per_lot = self.spec.money_per_lot(sl_price_distance)
        if self.config.include_commission_in_risk:
            loss_per_lot += float(self.config.commission_per_lot)
        if loss_per_lot <= 0.0:
            logger.error("loss_per_lot resolved to 0; falling back to min lot")
            return self.config.min_lot, 0.0

        raw_lot = budget / loss_per_lot
        lot = self.round_volume(raw_lot)
        lot = max(self.config.min_lot, min(self.config.max_lot, lot))
        lot = max(self.spec.volume_min, min(self.spec.volume_max, lot))
        lot = self.round_volume(lot)

        risk_money = self.spec.money_per_lot(sl_price_distance) * lot
        risk_money += self.config.commission_per_lot * lot
        if raw_lot < self.config.min_lot:
            logger.warning(
                "risk budget ${:.2f} implies {:.4f} lots (< min {:.2f}); "
                "trading min lot risks ${:.2f} ({:.2f}% of balance)",
                budget, raw_lot, self.config.min_lot, risk_money,
                (risk_money / bal * 100.0) if bal else 0.0,
            )
        return lot, risk_money

    # ---------------------------------------------------------------- levels
    def build_levels(
        self,
        side: Side,
        entry_price: float,
        atr_value: float,
        balance: float,
    ) -> Optional[TradeLevels]:
        """Compose the complete entry package, or ``None`` if ATR is unusable."""
        if not atr_value or atr_value <= 0 or not math.isfinite(float(atr_value)):
            logger.error("invalid ATR ({}), refusing to size a trade", atr_value)
            return None

        sl_dist = self.sl_distance(atr_value)
        tp_dist = self.tp_distance(atr_value)
        entry = self._round_price(entry_price)

        if side == "BUY":
            sl = self._round_price(entry - sl_dist)
            tp = self._round_price(entry + tp_dist)
        else:
            sl = self._round_price(entry + sl_dist)
            tp = self._round_price(entry - tp_dist)

        lot, risk_money = self.calculate_lot(balance, sl_dist)
        reward_money = self.spec.money_per_lot(tp_dist) * lot - self.config.commission_per_lot * lot
        return TradeLevels(
            side=side,
            entry=entry,
            sl=sl,
            tp=tp,
            lot=lot,
            atr=float(atr_value),
            sl_points=self.spec.points(sl_dist),
            tp_points=self.spec.points(tp_dist),
            risk_money=risk_money,
            reward_money=reward_money,
            rr=(reward_money / risk_money) if risk_money > 0 else 0.0,
        )

    # -------------------------------------------------- protective stop logic
    @staticmethod
    def _profit_distance(side: Side, entry: float, price: float) -> float:
        return (price - entry) if side == "BUY" else (entry - price)

    def breakeven_price(self, side: Side, entry: float) -> float:
        buffer_price = self.spec.price_from_points(self.config.breakeven_buffer_points)
        target = entry + buffer_price if side == "BUY" else entry - buffer_price
        return self._round_price(target)

    def trailing_price(self, side: Side, market_price: float, atr_value: float) -> float:
        distance = float(atr_value) * self.config.trailing_distance_atr
        target = market_price - distance if side == "BUY" else market_price + distance
        return self._round_price(target)

    def evaluate_position(
        self,
        position: PositionView,
        bid: float,
        ask: float,
        atr_value: float,
    ) -> Optional[ManageAction]:
        """Decide whether the SL of ``position`` must move. Evaluated every tick.

        Order of precedence: trailing wins when armed (it is always further in
        profit than break-even); otherwise break-even fires once.
        Stops are **monotonic**: they can only move in the profitable direction.
        """
        if not atr_value or atr_value <= 0:
            return None

        side: Side = position.side
        # A position is closed against bid (long) / ask (short).
        market = float(bid) if side == "BUY" else float(ask)
        profit = self._profit_distance(side, position.price_open, market)
        if profit <= 0:
            return None

        atr = float(atr_value)
        current_sl = float(position.sl or 0.0)
        tick = self.spec.tick_size or 0.01

        def improves(candidate: float) -> bool:
            if current_sl <= 0:
                return True
            return (
                candidate > current_sl + tick / 2
                if side == "BUY"
                else candidate < current_sl - tick / 2
            )

        # ---- trailing stop (armed at 1.5 ATR, trails 1.2 ATR behind) --------
        if profit >= atr * self.config.trailing_trigger_atr:
            candidate = self.trailing_price(side, market, atr)
            be = self.breakeven_price(side, position.price_open)
            # never trail to a worse-than-break-even level
            candidate = max(candidate, be) if side == "BUY" else min(candidate, be)
            if improves(candidate) and self._respects_stop_level(side, candidate, market):
                return ManageAction(
                    kind="trailing",
                    ticket=position.ticket,
                    new_sl=candidate,
                    old_sl=current_sl,
                    reason=(
                        f"profit {profit:.2f} >= {self.config.trailing_trigger_atr}xATR "
                        f"({atr:.2f}) → trail {self.config.trailing_distance_atr}xATR"
                    ),
                )
            return None

        # ---- break-even (armed at 1.0 ATR, SL = entry +/- 10 points) --------
        if not position.breakeven_done and profit >= atr * self.config.breakeven_trigger_atr:
            candidate = self.breakeven_price(side, position.price_open)
            if improves(candidate) and self._respects_stop_level(side, candidate, market):
                return ManageAction(
                    kind="breakeven",
                    ticket=position.ticket,
                    new_sl=candidate,
                    old_sl=current_sl,
                    reason=(
                        f"profit {profit:.2f} >= {self.config.breakeven_trigger_atr}xATR "
                        f"({atr:.2f}) → risk-free at entry+{self.config.breakeven_buffer_points:.0f}pts"
                    ),
                )
        return None

    def _respects_stop_level(self, side: Side, sl_price: float, market: float) -> bool:
        """Broker rejects stops closer than ``stops_level`` to the market."""
        min_gap = self.spec.price_from_points(max(self.spec.stops_level_points, 1.0))
        gap = (market - sl_price) if side == "BUY" else (sl_price - market)
        if gap < min_gap:
            logger.debug("candidate SL {} too close to market {}", sl_price, market)
            return False
        return True

    # ------------------------------------------------------------ gate checks
    def can_open(self, open_positions: int) -> bool:
        return int(open_positions) < int(self.config.max_positions_per_symbol)

    def describe(self) -> str:
        c = self.config
        return (
            f"risk {c.risk_percent:.2f}% | lots {c.min_lot}-{c.max_lot} | "
            f"SL/TP {c.sl_atr_multiplier}x/{c.tp_atr_multiplier}xATR({c.atr_period}) | "
            f"BE {c.breakeven_trigger_atr}xATR+{c.breakeven_buffer_points:.0f}pts | "
            f"trail {c.trailing_trigger_atr}x/{c.trailing_distance_atr}xATR"
        )
