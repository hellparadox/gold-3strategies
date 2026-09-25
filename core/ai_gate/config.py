"""Configuration of the AI gate: ``config/ai_gate.yaml`` + runtime override.

The YAML has ``defaults`` and per-strategy overrides under ``bots.<name>``
(``<name>`` = ``strategy.name``).  A Telegram ``/aigate <mode>`` command
stores a runtime override in ``data/ai_gate_state_<name>.json``.  Precedence:

* override present and the YAML mode is unchanged since it was written
  → override wins (source ``runtime``);
* YAML mode edited after the override was written → YAML wins and the
  override file is rewritten to match (source ``yaml``).  Editing the YAML is
  therefore always a reliable way to switch modes.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

import yaml
from loguru import logger

MODES = ("off", "shadow", "live")
FAIL_POLICIES = ("open", "closed")
DEFAULT_CONFIG_PATH = os.path.join("config", "ai_gate.yaml")


class ConfigError(ValueError):
    """Invalid ai_gate configuration."""


@dataclass(frozen=True)
class ContextConfig:
    trigger_bars: int = 48
    h1_bars: int = 48
    include_recent_trades: bool = True
    recent_trades: int = 20


@dataclass(frozen=True)
class AIGateConfig:
    mode: str = "off"
    provider: str = "openai_compatible"
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = ""
    api_key_env: str = "AI_GATE_API_KEY"
    response_format_json: bool = True
    temperature: float = 0.0
    max_output_tokens: int = 600
    timeout_seconds: float = 20.0
    max_retries: int = 2
    fail_policy: str = "open"
    block_min_confidence: int = 60
    max_entry_drift_atr: float = 0.30
    max_calls_per_day: int = 60
    max_cost_usd_per_day: float = 2.0
    price_input_per_mtok: float = 0.0
    price_output_per_mtok: float = 0.0
    notify_telegram: bool = True
    prompt_file: str = os.path.join("config", "ai_gate_prompt.md")
    journal_dir: str = "data"
    context: ContextConfig = field(default_factory=ContextConfig)
    base_rates: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ validation
    def validate(self) -> "AIGateConfig":
        problems = []
        if self.mode not in MODES:
            problems.append(f"mode must be one of {MODES}, got {self.mode!r}")
        if self.provider != "openai_compatible":
            problems.append(f"provider must be 'openai_compatible', got {self.provider!r}")
        if not str(self.base_url).startswith(("https://", "http://")):
            problems.append("base_url must start with https:// or http://")
        if not self.api_key_env or not str(self.api_key_env).replace("_", "").isalnum():
            problems.append("api_key_env must be an environment variable name")
        if not 0.0 <= float(self.temperature) <= 2.0:
            problems.append("temperature must be within 0..2")
        if not 50 <= int(self.max_output_tokens) <= 8000:
            problems.append("max_output_tokens must be within 50..8000")
        if not 1.0 <= float(self.timeout_seconds) <= 120.0:
            problems.append("timeout_seconds must be within 1..120")
        if not 0 <= int(self.max_retries) <= 5:
            problems.append("max_retries must be within 0..5")
        if self.fail_policy not in FAIL_POLICIES:
            problems.append(f"fail_policy must be one of {FAIL_POLICIES}")
        if not 0 <= int(self.block_min_confidence) <= 100:
            problems.append("block_min_confidence must be within 0..100")
        if not 0.0 < float(self.max_entry_drift_atr) <= 5.0:
            problems.append("max_entry_drift_atr must be within (0, 5]")
        if int(self.max_calls_per_day) < 0:
            problems.append("max_calls_per_day must be >= 0")
        if float(self.max_cost_usd_per_day) < 0:
            problems.append("max_cost_usd_per_day must be >= 0")
        if float(self.price_input_per_mtok) < 0 or float(self.price_output_per_mtok) < 0:
            problems.append("token prices must be >= 0")
        c = self.context
        if not 10 <= int(c.trigger_bars) <= 200:
            problems.append("context.trigger_bars must be within 10..200")
        if not 0 <= int(c.h1_bars) <= 200:
            problems.append("context.h1_bars must be within 0..200")
        if not 0 <= int(c.recent_trades) <= 200:
            problems.append("context.recent_trades must be within 0..200")
        if not isinstance(self.base_rates, dict):
            problems.append("base_rates must be a mapping")
        if problems:
            raise ConfigError("; ".join(problems))
        return self

    def with_mode(self, mode: str) -> "AIGateConfig":
        values = {f.name: getattr(self, f.name) for f in fields(self)}
        values["mode"] = mode
        return AIGateConfig(**values).validate()

    def api_key(self) -> str:
        return str(os.environ.get(self.api_key_env, "") or "").strip()


def _deep_merge(base: Dict[str, Any], extra: Mapping[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _from_mapping(raw: Mapping[str, Any]) -> AIGateConfig:
    known = {f.name for f in fields(AIGateConfig)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(f"unknown ai_gate keys: {unknown}")
    values = dict(raw)
    ctx = values.pop("context", None) or {}
    ctx_known = {f.name for f in fields(ContextConfig)}
    ctx_unknown = sorted(set(ctx) - ctx_known)
    if ctx_unknown:
        raise ConfigError(f"unknown ai_gate.context keys: {ctx_unknown}")
    mode = values.get("mode", "off")
    if isinstance(mode, bool):          # YAML 1.1 reads a bare `off` as False
        mode = "off" if mode is False else "invalid-true"
    values["mode"] = str(mode).strip().lower()
    values["fail_policy"] = str(values.get("fail_policy", "open")).strip().lower()
    values["model"] = str(values.get("model", "") or "").strip()
    values["base_rates"] = dict(values.get("base_rates") or {})
    return AIGateConfig(context=ContextConfig(**ctx), **values).validate()


def load_config(strategy_name: str, path: str = DEFAULT_CONFIG_PATH) -> AIGateConfig:
    """``defaults`` deep-merged with ``bots[strategy_name]``.  Missing file → off."""
    if not os.path.exists(path):
        return AIGateConfig()
    with open(path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    if not isinstance(doc, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    merged = _deep_merge(doc.get("defaults") or {}, (doc.get("bots") or {}).get(strategy_name) or {})
    return _from_mapping(merged)


# --------------------------------------------------------------- runtime override
def state_path(strategy_name: str, journal_dir: str = "data") -> str:
    return os.path.join(journal_dir, f"ai_gate_state_{strategy_name}.json")


def write_state(path: str, mode: str, yaml_mode: str) -> None:
    """Atomic write (temp file + os.replace) of the runtime override."""
    if mode not in MODES:
        raise ConfigError(f"mode must be one of {MODES}")
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    payload = {
        "mode": mode,
        "set_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "yaml_mode_at_set": yaml_mode,
    }
    fd, tmp = tempfile.mkstemp(prefix=".ai_gate_state_", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_state(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def resolve_mode(yaml_mode: str, path: str) -> Tuple[str, str]:
    """Effective mode and its source (``yaml`` | ``runtime``)."""
    state = read_state(path)
    if not state or state.get("mode") not in MODES:
        return yaml_mode, "yaml"
    if state.get("yaml_mode_at_set") == yaml_mode:
        return str(state["mode"]), "runtime"
    logger.warning(
        "AI gate: yaml mode changed since runtime override ({} -> {}); yaml wins",
        state.get("yaml_mode_at_set"), yaml_mode,
    )
    try:
        write_state(path, yaml_mode, yaml_mode)
    except OSError as exc:
        logger.warning("AI gate: cannot rewrite runtime state: {}", exc)
    return yaml_mode, "yaml"
