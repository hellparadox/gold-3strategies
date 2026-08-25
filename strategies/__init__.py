from __future__ import annotations

from typing import Any, Dict, List, Optional

from strategies.base import BaseStrategy
from strategies.kijun_pullback import KijunPullbackStrategy
from strategies.orb_gold import GoldOrbStrategy
from strategies.ichimoku_m15 import IchimokuM15Strategy


_STRATEGIES = {
    KijunPullbackStrategy.name: KijunPullbackStrategy,
    GoldOrbStrategy.name: GoldOrbStrategy,
    IchimokuM15Strategy.name: IchimokuM15Strategy,
}


def available_strategies() -> List[str]:
    return list(_STRATEGIES.keys())


def build_strategy(
    name: str,
    params: Optional[Dict[str, Any]] = None,
    atr_period: int = 14,
) -> BaseStrategy:
    try:
        strategy_cls = _STRATEGIES[str(name).strip().lower()]
    except KeyError as exc:
        available = ", ".join(available_strategies())
        raise KeyError(
            f"unknown strategy {name!r}; available: {available}"
        ) from exc

    return strategy_cls(params=params, atr_period=atr_period)


def build_from_settings(settings: Any) -> BaseStrategy:
    name = str(
        settings.get("strategy.active", "kijun_pullback")
    )

    params = settings.get(
        f"strategy.params.{name}",
        {},
    ) or {}

    atr_period = int(
        settings.get("risk.atr_period", 14)
    )

    return build_strategy(
        name=name,
        params=params,
        atr_period=atr_period,
    )


__all__ = [
    "BaseStrategy",
    "KijunPullbackStrategy",
    "GoldOrbStrategy",
    "IchimokuM15Strategy",
    "available_strategies",
    "build_strategy",
    "build_from_settings",
]
