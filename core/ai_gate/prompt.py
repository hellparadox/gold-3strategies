"""System prompt (versioned) and user message for the AI gate."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Tuple

from core.ai_gate.decision import OUTPUT_SCHEMA_VERSION


def load_system_prompt(path: str) -> Tuple[str, str]:
    """Return ``(prompt_text, prompt_version)``; version = sha256(text + schema)[:12]."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        raise ValueError(f"empty AI gate prompt file: {path}")
    version = hashlib.sha256((text + "\n" + OUTPUT_SCHEMA_VERSION).encode("utf-8")).hexdigest()[:12]
    return text, version


def build_user_message(context: Dict[str, Any]) -> str:
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
