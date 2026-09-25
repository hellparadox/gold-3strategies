"""Strict parsing of the model's JSON verdict."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, List

OUTPUT_SCHEMA_VERSION = "ai_gate_out/1"
DECISIONS = ("TAKE", "SKIP")
_MAX_REASONS = 5
_MAX_REASON_CHARS = 160
_MAX_FLAGS = 8
_RE_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_RE_FLAG = re.compile(r"[^a-z0-9_]+")


class InvalidDecision(ValueError):
    """The model's reply is not a valid verdict."""


@dataclass(frozen=True)
class Decision:
    decision: str
    confidence: int
    p_protect: float
    reasons: List[str] = field(default_factory=list)
    risk_flags: List[str] = field(default_factory=list)


def _extract_object(text: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        raise InvalidDecision("empty reply")
    fenced = _RE_FENCE.search(raw)
    if fenced:
        raw = fenced.group(1)
    try:
        return json.loads(raw)
    except ValueError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise InvalidDecision("reply is not JSON")
        try:
            return json.loads(raw[start:end + 1])
        except ValueError as exc:
            raise InvalidDecision(f"reply is not JSON: {exc}") from None


def parse_decision(text: str) -> Decision:
    obj = _extract_object(text)
    if not isinstance(obj, dict):
        raise InvalidDecision("reply JSON is not an object")

    decision = obj.get("decision")
    if not isinstance(decision, str) or decision.strip().upper() not in DECISIONS:
        raise InvalidDecision(f"decision must be TAKE or SKIP, got {decision!r}")

    confidence = obj.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise InvalidDecision("confidence must be a number")
    if float(confidence) != int(confidence) or not 0 <= int(confidence) <= 100:
        raise InvalidDecision("confidence must be an integer 0..100")

    p_protect = obj.get("p_protect")
    if isinstance(p_protect, bool) or not isinstance(p_protect, (int, float)):
        raise InvalidDecision("p_protect must be a number")
    if not 0.0 <= float(p_protect) <= 1.0:
        raise InvalidDecision("p_protect must be within 0..1")

    reasons = obj.get("reasons", [])
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise InvalidDecision("reasons must be a list of strings")
    reasons = [r.strip()[:_MAX_REASON_CHARS] for r in reasons if r.strip()][:_MAX_REASONS]

    flags = obj.get("risk_flags", [])
    if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
        raise InvalidDecision("risk_flags must be a list of strings")
    flags = [_RE_FLAG.sub("_", f.strip().lower()).strip("_")[:40] for f in flags]
    flags = [f for f in flags if f][:_MAX_FLAGS]

    return Decision(
        decision=decision.strip().upper(),
        confidence=int(confidence),
        p_protect=round(float(p_protect), 4),
        reasons=reasons,
        risk_flags=flags,
    )
