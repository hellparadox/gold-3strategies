"""AI pre-trade gate: ask an LLM whether a rule-based entry should be taken.

Modes (config/ai_gate.yaml, runtime override via Telegram ``/aigate``):

* ``off``    – nothing runs; the bot behaves exactly as without this package.
* ``shadow`` – the verdict is requested in the background and journaled; it
  never delays or changes an order.  Used to collect evidence.
* ``live``   – the entry waits (without blocking the main loop) for the
  verdict; a confident SKIP cancels the entry.

Everything the model sees is built from CLOSED candles up to the signal bar;
nothing that identifies the trading account is sent.  See ``gate.AIGate``.
"""
from core.ai_gate.config import MODES, AIGateConfig, load_config, resolve_mode
from core.ai_gate.decision import Decision, InvalidDecision, parse_decision
from core.ai_gate.gate import AIGate, GateResult

__all__ = [
    "MODES", "AIGateConfig", "load_config", "resolve_mode",
    "Decision", "InvalidDecision", "parse_decision",
    "AIGate", "GateResult",
]
