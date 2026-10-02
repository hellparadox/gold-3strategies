"""Scout «away» mode, AI option: when an approved position reaches its signal stop-loss
and the owner chose «AI decides», ask the model HOLD or CLOSE.

Uses the provider / model / fallback models / key of config/ai_gate.yaml (defaults),
so no extra setup.  Fail-safe: anything but a confident HOLD means CLOSE.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd

SYSTEM = (
    "You manage an open XAUUSD (gold) position for a discretionary trader who is away. "
    "Price has just reached the trade's planned stop-loss. Decide whether to CLOSE now "
    "(accept the planned loss) or HOLD (keep it open; a broker emergency stop exists further away). "
    "HOLD only when the M15/H1 structure clearly still supports the trade direction and the move "
    "against it looks like a shallow pullback; otherwise CLOSE. Ignore sunk cost. "
    "Answer ONLY with a JSON object: "
    '{"decision": "HOLD" or "CLOSE", "confidence": 0-100, "reason": "<one short sentence in Persian>"}'
)


@dataclass(frozen=True)
class AIVerdict:
    decision: str            # HOLD | CLOSE
    confidence: int
    reason: str
    model: str = ""
    error: str = ""

    @property
    def hold(self) -> bool:
        return self.decision == "HOLD"


def build_message(info: Dict[str, Any], price: float, profit: float, f: Optional[pd.DataFrame],
                  bars: int = 24) -> str:
    lines = [
        f"position: {info['side']} 0.01 lot, entry {float(info['entry']):.2f}, now {price:.2f}, "
        f"P/L {profit:+.2f} USD",
        f"planned stop-loss {float(info['sl']):.2f} (reached), emergency stop {float(info.get('emerg') or 0):.2f}",
        f"setup: {info.get('setup', '')}",
    ]
    if f is not None and not f.empty:
        cols = [c for c in ("open", "high", "low", "close", "tenkan", "kijun", "ema200", "h1_ema", "atr")
                if c in f.columns]
        tail = f[cols].tail(bars).round(2)
        lines.append("last closed M15 bars (server time):")
        lines.append(tail.to_csv())
    return "\n".join(lines)


def parse(text: str, model: str = "") -> AIVerdict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return AIVerdict("CLOSE", 0, "", model, "no JSON in reply")
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return AIVerdict("CLOSE", 0, "", model, "invalid JSON")
    dec = str(d.get("decision", "")).strip().upper()
    if dec not in ("HOLD", "CLOSE"):
        return AIVerdict("CLOSE", 0, "", model, f"bad decision {dec!r}")
    try:
        conf = max(0, min(100, int(float(d.get("confidence", 0)))))
    except (TypeError, ValueError):
        conf = 0
    return AIVerdict(dec, conf, str(d.get("reason", ""))[:200], model)


class ScoutAI:
    """Thin wrapper over the AI-gate provider stack (primary + fallback models)."""

    def __init__(self, providers: List[Any], timeout: float = 60.0) -> None:
        self.providers = list(providers)
        self.timeout = float(timeout)

    @classmethod
    def from_gate_config(cls, timeout: float = 60.0) -> Optional["ScoutAI"]:
        try:
            from core.ai_gate.config import load_config
            from core.ai_gate.provider import OpenAICompatibleProvider
            cfg = load_config("scout")
            key = cfg.api_key()
            if not key or not cfg.model:
                return None
            models = [cfg.model] + [m for m in cfg.fallback_models if m and m != cfg.model]
            provs = [OpenAICompatibleProvider(cfg.base_url, key, m, 0.0, cfg.max_output_tokens,
                                              cfg.response_format_json, min(int(cfg.max_retries), 2))
                     for m in models]
            return cls(provs, timeout)
        except Exception:
            return None

    def decide(self, user_message: str, clock=None) -> AIVerdict:
        import time as _t
        clock = clock or _t.time
        deadline = clock() + self.timeout
        last_err = "no provider"
        for i, p in enumerate(self.providers):
            remaining = deadline - clock()
            if remaining <= 2:
                break
            sub = deadline if i + 1 == len(self.providers) else clock() + remaining * 0.6
            try:
                r = p.complete(SYSTEM, user_message, sub)
                return parse(r.text, getattr(r, "model", "") or getattr(p, "model", ""))
            except Exception as exc:                  # 503/429/timeout -> next model
                last_err = str(exc)[:160]
        return AIVerdict("CLOSE", 0, "", "", last_err)
