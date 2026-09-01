"""Signal quality score (0-100) from historical backtest statistics.

The score answers one honest question: "when this exact setup fired in the
past, how often did it win?" — nothing more, nothing less.

How it works
------------
``tools/precompute_scores.py`` runs a backtest, buckets every historical trade
by (strategy, side, session, ATR regime) and writes ``data/signal_scores.json``.
At live time :class:`SignalScorer` looks the bucket up and applies Laplace
smoothing toward the strategy's overall win rate so tiny buckets cannot produce
silly 100/100 scores:

    p = (wins + PRIOR * overall_wr) / (trades + PRIOR)

Score = round(100 * p).  With no stats file at all the scorer degrades to a
neutral 50 and logs once — it can never block a signal.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from loguru import logger

__all__ = ["SignalScorer", "session_bucket"]

# strength of the shrinkage toward the overall win rate (in pseudo-trades)
PRIOR_STRENGTH = 6.0

# ATR tercile thresholds are written by the precompute script; sane fallbacks
# cover the "file missing" case so the scorer is always callable.
_DEFAULT_ATR_BANDS = (0.60, 1.20)


def session_bucket(hour: int) -> str:
    """Coarse trading-session bucket from the broker-server hour."""
    h = int(hour) % 24
    if 0 <= h < 8:
        return "asia"
    if 8 <= h < 14:
        return "europe"
    if 14 <= h < 21:
        return "us"
    return "late"


class SignalScorer:
    """Loads precomputed bucket stats and scores live signals."""

    def __init__(self, stats_path: str = "data/signal_scores.json") -> None:
        self.stats_path = Path(stats_path)
        self._meta: Dict[str, Any] = {}
        self._overall: Dict[str, float] = {"trades": 0.0, "wins": 0.0}
        self._buckets: Dict[str, Dict[str, float]] = {}
        self._atr_bands: Tuple[float, float] = _DEFAULT_ATR_BANDS
        self._warned = False
        self._load()

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        if not self.stats_path.exists():
            self._warn_missing()
            return
        try:
            with open(self.stats_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._meta = data.get("meta", {})
            self._overall = {
                "trades": float(data.get("overall", {}).get("trades", 0)),
                "wins": float(data.get("overall", {}).get("wins", 0)),
            }
            self._buckets = {
                str(k): {"trades": float(v.get("trades", 0)), "wins": float(v.get("wins", 0))}
                for k, v in data.get("buckets", {}).items()
            }
            bands = data.get("meta", {}).get("atr_bands")
            if isinstance(bands, (list, tuple)) and len(bands) == 2:
                self._atr_bands = (float(bands[0]), float(bands[1]))
            logger.info(
                "signal scores loaded | {} buckets | {} historical trades | atr_bands={}",
                len(self._buckets), int(self._overall["trades"]), self._atr_bands,
            )
        except Exception as exc:
            self._warn_missing()
            logger.warning("signal score file unreadable ({}): {}", self.stats_path, exc)

    def _warn_missing(self) -> None:
        if not self._warned:
            logger.info(
                "no signal score stats at '{}' — scores default to neutral 50. "
                "Run: python tools/precompute_scores.py",
                self.stats_path,
            )
            self._warned = True

    # ----------------------------------------------------------------- score
    def _atr_bucket(self, atr: float) -> str:
        low, high = self._atr_bands
        if atr <= low:
            return "low"
        if atr >= high:
            return "high"
        return "mid"

    @property
    def has_data(self) -> bool:
        return bool(self._buckets)

    def score(
        self,
        *,
        strategy: str,
        side: str,
        hour: int,
        atr: float,
    ) -> int:
        """Return the smoothed 0-100 quality score for one live setup."""
        overall_trades = self._overall.get("trades", 0.0)
        overall_wins = self._overall.get("wins", 0.0)
        if overall_trades <= 0 or not self._buckets:
            return 50

        overall_wr = overall_wins / overall_trades
        key = f"{strategy}|{side.upper()}|{session_bucket(hour)}|{self._atr_bucket(float(atr))}"
        bucket = self._buckets.get(key)
        if bucket is None:
            # unknown bucket: fall back to overall win rate, lightly smoothed
            p = overall_wr
        else:
            trades = bucket["trades"]
            wins = bucket["wins"]
            p = (wins + PRIOR_STRENGTH * overall_wr) / (trades + PRIOR_STRENGTH)
        return int(round(max(0.0, min(1.0, p)) * 100))

    def describe(
        self,
        *,
        strategy: str,
        side: str,
        hour: int,
        atr: float,
    ) -> Dict[str, Any]:
        """Score plus the evidence behind it (for logs / dashboard)."""
        key = f"{strategy}|{side.upper()}|{session_bucket(hour)}|{self._atr_bucket(float(atr))}"
        bucket = self._buckets.get(key)
        return {
            "score": self.score(strategy=strategy, side=side, hour=hour, atr=atr),
            "bucket": key,
            "bucket_trades": int(bucket["trades"]) if bucket else 0,
            "bucket_wins": int(bucket["wins"]) if bucket else 0,
            "overall_trades": int(self._overall.get("trades", 0)),
        }

    def reload(self) -> bool:
        """Re-read the stats file (called after re-running precompute)."""
        self._warned = False
        self._load()
        return self.has_data
