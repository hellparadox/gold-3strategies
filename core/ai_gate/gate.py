"""The gate: journal, budget, background worker and the live/shadow policy.

Thread model: one worker thread (``ai-gate``) performs provider calls; the bot's
main loop only submits work and later polls a ``Future``.  Nothing here ever
blocks the main loop.  All public methods are safe to call from any thread and
never raise into the trading path (failures are logged and journaled).
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from loguru import logger

from core.ai_gate.config import (
    MODES, AIGateConfig, ConfigError, load_config, resolve_mode, state_path, write_state,
)
from core.ai_gate.decision import InvalidDecision, parse_decision
from core.ai_gate.evaluate import (
    FINAL_EXITS, HORIZON_HOURS, ExitRules, correctness, evaluate, simulate,
)
from core.ai_gate.journal import DecisionJournal
from core.ai_gate.prompt import build_user_message, load_system_prompt
from core.ai_gate.provider import LLMProvider, OpenAICompatibleProvider, ProviderError, redact

MAX_INFLIGHT = 4
_STATUS_LABEL = {
    "timeout": "TIMEOUT", "http_error": "ERROR", "error": "ERROR", "invalid": "INVALID",
    "budget": "BUDGET", "no_key": "NOKEY", "queue_full": "ERROR", "stale": "ERROR",
    "late": "TIMEOUT",
}
ACTIONS_FA = {
    "shadow_logged": "فقط ثبت شد",
    "taken": "وارد شد",
    "blocked": "جلوی ورود گرفته شد",
    "taken_fail_open": "به‌خاطر خطا بدون نظر وارد شد",
    "blocked_fail_closed": "به‌خاطر خطا وارد نشد",
    "cancelled_drift": "لغو: قیمت در زمان انتظار دور شد",
    "cancelled_guard": "لغو: شرایط ورود دیگر برقرار نبود",
    "order_failed": "سفارش در بروکر انجام نشد",
}
MODES_FA = {"off": "خاموش", "shadow": "سایه (فقط ثبت)", "live": "فعال (می‌تواند جلوی ورود را بگیرد)"}


def hash_key(raw_key: str) -> str:
    """Journal key: sha256 of the execution key (keeps login/server out of the journal)."""
    return hashlib.sha256(str(raw_key).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GateResult:
    signal_key: str
    mode: str
    status: str
    decision: Optional[str] = None
    confidence: int = 0
    p_protect: Optional[float] = None
    reasons: Tuple[str, ...] = ()
    risk_flags: Tuple[str, ...] = ()
    latency_ms: int = 0
    cost_usd: float = 0.0
    model: str = ""
    error: str = ""
    cached: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def label(self) -> str:
        return str(self.decision) if self.ok else _STATUS_LABEL.get(self.status, "ERROR")

    @property
    def headline(self) -> str:
        if self.ok:
            return self.reasons[0] if self.reasons else "-"
        return self.error or self.status


def _done(result: GateResult) -> "Future[GateResult]":
    fut: "Future[GateResult]" = Future()
    fut.set_result(result)
    return fut


class AIGate:
    """See module docstring.  Build with :meth:`create`."""

    def __init__(self, cfg: AIGateConfig, strategy_name: str, *,
                 notify: Optional[Callable[[str], None]] = None,
                 provider: Optional[LLMProvider] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._yaml_cfg = cfg
        self.strategy_name = strategy_name
        self._notify = notify
        self._provider = provider
        self._clock = clock
        self._lock = threading.RLock()
        self._state_path = state_path(strategy_name, cfg.journal_dir)
        self._journal_path = os.path.join(cfg.journal_dir, f"ai_gate_{strategy_name}.db")
        if cfg.mode == "off" and not os.path.exists(self._state_path):
            self._mode, self._source = "off", "yaml"
        else:
            self._mode, self._source = resolve_mode(cfg.mode, self._state_path)
        self._journal: Optional[DecisionJournal] = None
        self._closed = False
        self._executor: Optional[ThreadPoolExecutor] = None
        self._system_prompt = ""
        self.prompt_version = ""
        self._ready = False
        self._disabled_reason = ""
        self._inflight = 0
        self._expired: set = set()          # live keys whose deadline passed in the main loop
        self._last: Optional[GateResult] = None
        if self._mode != "off":
            self._ensure_ready()

    # ---------------------------------------------------------------- factory
    @classmethod
    def create(cls, strategy_name: str, *, config_path: str = os.path.join("config", "ai_gate.yaml"),
               notify: Optional[Callable[[str], None]] = None,
               provider: Optional[LLMProvider] = None) -> "AIGate":
        """Never raises: an invalid config logs an ERROR and yields an ``off`` gate."""
        try:
            cfg = load_config(strategy_name, config_path)
        except (ConfigError, OSError, ValueError) as exc:
            logger.error("AI gate config invalid, gate stays off: {}", exc)
            cfg = AIGateConfig()
        return cls(cfg, strategy_name, notify=notify, provider=provider)

    # ------------------------------------------------------------------ state
    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def active_mode(self) -> str:
        """Mode actually applied to trading (``off`` when disabled for lack of key/model)."""
        with self._lock:
            if self._mode == "off" or not self._ready:
                return "off"
            return self._mode

    def _cfg(self) -> AIGateConfig:
        return self._yaml_cfg

    @property
    def config(self) -> AIGateConfig:
        return self._yaml_cfg

    def _ensure_ready(self) -> bool:
        with self._lock:
            if self._ready:
                return True
            cfg = self._cfg()
            if self._provider is None:
                key = cfg.api_key()
                if not key or not cfg.model:
                    if self._disabled_reason != "no_key":
                        logger.warning(
                            "AI gate disabled: missing API key (env {}) or model", cfg.api_key_env)
                    self._disabled_reason = "no_key"
                    return False
                self._provider = OpenAICompatibleProvider(
                    cfg.base_url, key, cfg.model, cfg.temperature, cfg.max_output_tokens,
                    cfg.response_format_json, cfg.max_retries)
            try:
                self._system_prompt, self.prompt_version = load_system_prompt(cfg.prompt_file)
            except (OSError, ValueError) as exc:
                logger.error("AI gate disabled: prompt file unavailable: {}", exc)
                self._disabled_reason = "prompt"
                return False
            self._open_journal()
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-gate")
            self._ready, self._disabled_reason = True, ""
            logger.info("AI gate ready | mode={} ({}) | model={} | prompt={}",
                        self._mode, self._source, getattr(self._provider, "model", cfg.model),
                        self.prompt_version)
            return True

    def _open_journal(self) -> DecisionJournal:
        with self._lock:
            if self._closed:
                raise RuntimeError("AI gate is shut down")
            if self._journal is None:
                self._journal = DecisionJournal(self._journal_path)
            return self._journal

    def set_mode(self, mode: str) -> Dict[str, Any]:
        mode = str(mode).strip().lower()
        if mode not in MODES:
            raise ConfigError(f"mode must be one of {MODES}")
        with self._lock:
            write_state(self._state_path, mode, self._cfg().mode)
            previous, self._mode, self._source = self._mode, mode, "runtime"
        logger.warning("AI gate mode {} -> {} (runtime override)", previous, mode)
        if mode != "off":
            self._ensure_ready()
        return self.status()

    # ----------------------------------------------------------------- submit
    def submit(self, context: Dict[str, Any], signal_key: str,
               meta: Dict[str, Any]) -> Optional["Future[GateResult]"]:
        """Queue a verdict request.  ``None`` when the gate is not active."""
        mode = self.active_mode()
        if mode == "off":
            return None
        cfg = self._cfg()
        journal = self._open_journal()
        try:
            existing = journal.get(signal_key)
            if existing is not None:
                return _done(self._from_row(existing))
            calls, cost = journal.today_usage()
            over_calls = cfg.max_calls_per_day > 0 and calls >= cfg.max_calls_per_day
            over_cost = cfg.max_cost_usd_per_day > 0 and cost >= cfg.max_cost_usd_per_day
            model = str(getattr(self._provider, "model", cfg.model))
            journal.insert_pending(signal_key, mode, meta, context, model, self.prompt_version)
            if over_calls or over_cost:
                return _done(self._finish(signal_key, mode, "budget",
                                          error=f"daily budget reached ({calls} calls, ${cost:.2f})"))
            with self._lock:
                if self._inflight >= MAX_INFLIGHT or self._executor is None:
                    full = True
                else:
                    self._inflight += 1
                    full = False
            if full:
                return _done(self._finish(signal_key, mode, "queue_full", error="worker queue full"))
            deadline = self._clock() + float(cfg.timeout_seconds)
            return self._executor.submit(self._run, signal_key, mode, context, deadline)
        except Exception as exc:  # never break the trading path
            logger.warning("AI gate submit failed: {}", exc)
            return _done(GateResult(signal_key, mode, "error", error=str(exc)[:200]))

    def _run(self, signal_key: str, mode: str, context: Dict[str, Any], deadline: float) -> GateResult:
        cfg = self._cfg()
        text, tokens_in, tokens_out, latency, model = "", 0, 0, 0, ""
        try:
            response = self._provider.complete(self._system_prompt, build_user_message(context), deadline)
            text, model = response.text, response.model
            tokens_in, tokens_out, latency = response.tokens_in, response.tokens_out, response.latency_ms
            verdict = parse_decision(text)
            cost = (tokens_in * cfg.price_input_per_mtok + tokens_out * cfg.price_output_per_mtok) / 1e6
            result = self._finish(signal_key, mode, "ok", response_text=text, model=model,
                                  decision=verdict.decision, confidence=verdict.confidence,
                                  p_protect=verdict.p_protect, reasons=verdict.reasons,
                                  risk_flags=verdict.risk_flags, latency_ms=latency,
                                  tokens_in=tokens_in, tokens_out=tokens_out, cost=cost)
        except ProviderError as exc:
            result = self._finish(signal_key, mode, exc.kind, error=str(exc))
        except InvalidDecision as exc:
            cost = (tokens_in * cfg.price_input_per_mtok + tokens_out * cfg.price_output_per_mtok) / 1e6
            result = self._finish(signal_key, mode, "invalid", response_text=text, model=model,
                                  error=str(exc), latency_ms=latency, tokens_in=tokens_in,
                                  tokens_out=tokens_out, cost=cost)
        except Exception as exc:
            secret = cfg.api_key()
            result = self._finish(signal_key, mode, "error", error=redact(repr(exc), secret))
        finally:
            with self._lock:
                self._inflight = max(0, self._inflight - 1)
        if mode == "shadow":
            self.finalize(result, "shadow_logged")
        return result

    def _finish(self, signal_key: str, mode: str, status: str, *, response_text: str = "",
                model: str = "", error: str = "", decision: Optional[str] = None,
                confidence: int = 0, p_protect: Optional[float] = None,
                reasons: Optional[List[str]] = None, risk_flags: Optional[List[str]] = None,
                latency_ms: int = 0, tokens_in: int = 0, tokens_out: int = 0,
                cost: float = 0.0) -> GateResult:
        with self._lock:
            if signal_key in self._expired and status != "timeout":
                # the main loop already gave up on this signal: keep the verdict for
                # research but mark it so it is never mistaken for a decision in time
                status = "late"
        try:
            self._open_journal().complete(
                signal_key, status=status, response_text=response_text, error=error,
                decision=decision, confidence=confidence if status == "ok" else None,
                p_protect=p_protect, reasons=reasons, risk_flags=risk_flags,
                latency_ms=latency_ms, tokens_in=tokens_in, tokens_out=tokens_out,
                cost_usd=cost, model=model or None)
        except Exception as exc:
            logger.warning("AI gate journal write failed: {}", exc)
        result = GateResult(signal_key, mode, status, decision, int(confidence or 0), p_protect,
                            tuple(reasons or ()), tuple(risk_flags or ()), int(latency_ms),
                            round(float(cost), 6), model, error[:200])
        with self._lock:
            self._last = result
        return result

    @staticmethod
    def _from_row(row: Dict[str, Any]) -> GateResult:
        status = row.get("status") or "error"
        if status == "pending":
            status = "stale"
        return GateResult(
            row["signal_key"], row.get("mode") or "", status, row.get("decision"),
            int(row.get("confidence") or 0), row.get("p_protect"),
            tuple(json.loads(row.get("reasons_json") or "[]")),
            tuple(json.loads(row.get("risk_flags_json") or "[]")),
            int(row.get("latency_ms") or 0), float(row.get("cost_usd") or 0.0),
            row.get("model") or "", row.get("error") or "", cached=True)

    # ----------------------------------------------------------------- policy
    def decide_live(self, result: GateResult) -> Tuple[bool, str]:
        """(allow_entry, action) for a live-mode result."""
        cfg = self._cfg()
        if result.ok:
            if result.decision == "SKIP" and result.confidence >= int(cfg.block_min_confidence):
                return False, "blocked"
            return True, "taken"
        if cfg.fail_policy == "closed":
            return False, "blocked_fail_closed"
        return True, "taken_fail_open"

    def timeout_result(self, signal_key: str, mode: str) -> GateResult:
        """Result used by the main loop when its own safety deadline passes first."""
        with self._lock:
            self._expired.add(signal_key)
        return self._finish(signal_key, mode, "timeout", error="decision deadline passed")

    def finalize(self, result: GateResult, action: str) -> None:
        """Journal the action, write the fixed-format log line, notify admins."""
        try:
            journal = self._open_journal()
            journal.set_action(result.signal_key, action)
        except Exception as exc:
            logger.warning("AI gate journal action failed: {}", exc)
        reason = result.headline.replace("\n", " ")[:160]
        logger.info("🤖 AI gate [{}] {} {}% → {} | {}", result.mode, result.label,
                    result.confidence if result.ok else 0, action, reason)
        if self._notify is not None and self._cfg().notify_telegram:
            try:
                self._notify(self._telegram_text(result, action))
            except Exception as exc:
                logger.warning("AI gate telegram notify failed: {}", exc)

    @staticmethod
    def _telegram_text(result: GateResult, action: str) -> str:
        mode_fa = "سایه" if result.mode == "shadow" else "فعال"
        if result.ok:
            verdict = "ورود ✅" if result.decision == "TAKE" else "رد ⛔"
            head = f"{verdict} با اطمینان {result.confidence}٪"
            if result.p_protect is not None:
                head += f" · احتمال رسیدن به سربه‌سر {round(result.p_protect * 100)}٪"
            reasons = "\n".join(f"• {html.escape(r)}" for r in result.reasons) or "—"
        else:
            head = f"بدون نظر ({html.escape(result.label)})"
            reasons = html.escape(result.error or result.status)
        return (f"🤖 <b>نظر هوش مصنوعی</b> (حالت {mode_fa})\n{head}\n"
                f"اقدام: <b>{ACTIONS_FA.get(action, action)}</b>\n{reasons}")

    # --------------------------------------------------------------- outcomes
    def attach_ticket(self, signal_key: str, ticket: int, fill: float) -> None:
        try:
            if self._journal is not None or os.path.exists(self._journal_path):
                self._open_journal().attach_ticket(signal_key, int(ticket), float(fill))
        except Exception as exc:
            logger.warning("AI gate attach_ticket failed: {}", exc)

    def record_outcome(self, ticket: int, profit: float) -> None:
        try:
            if self._journal is not None or os.path.exists(self._journal_path):
                self._open_journal().record_outcome(int(ticket), float(profit))
        except Exception as exc:
            logger.warning("AI gate record_outcome failed: {}", exc)

    # ------------------------------------------------------------ evaluation
    def has_journal(self) -> bool:
        with self._lock:
            return not self._closed and (self._journal is not None or os.path.exists(self._journal_path))

    def simulate_pending(self, fetch_bars: Callable[[pd.Timestamp, pd.Timestamp], pd.DataFrame],
                         rules: ExitRules, server_now: datetime, limit: int = 30) -> int:
        """Simulate the outcome of verdicts on broker M1 bars; returns rows updated.

        Called from the bot's main loop (MT5 thread).  A simulation is final when the
        simulated trade exited (tp/sl/be/trail) or the 48 h horizon has passed; until then
        the partial state is stored so the dashboard can show it as in progress.
        """
        if not self.has_journal():
            return 0
        journal = self._open_journal()
        now = pd.Timestamp(server_now)
        horizon = timedelta(hours=HORIZON_HOURS)
        updated = 0
        for row in journal.pending_sims(limit):
            try:
                ctx = json.loads(row.get("context_json") or "{}")
                tf = str(ctx.get("trigger_tf") or "M15")
                minutes = int(tf[1:]) if tf[1:].isdigit() else 15
                start = pd.Timestamp(row["ref_time_server"]) + timedelta(minutes=minutes)
                if now < start + timedelta(minutes=1):
                    continue
                end = min(now, start + horizon)
                bars = fetch_bars(start, end)
                if bars is None or len(bars) == 0:
                    if now > start + horizon + timedelta(days=5):   # history will not come back
                        journal.store_sim(row["signal_key"], 0.0, "no_data", False, True)
                        updated += 1
                    continue
                bars = bars.loc[(bars.index >= start) & (bars.index <= end)]
                res = simulate(row["side"], float(row["entry_plan"]), float(row["sl_plan"]),
                               float(row["tp_plan"]), float(row["atr"]), bars, rules,
                               float(row.get("spread_points") or 0.0))
                if res.exit == "no_data":
                    continue
                done = res.exit in FINAL_EXITS or end >= start + horizon
                journal.store_sim(row["signal_key"], res.profit_usd, res.exit, res.protected, done)
                updated += 1
            except Exception as exc:
                logger.warning("AI gate simulation failed for row {}: {}", row.get("id"), exc)
        return updated

    def dashboard_report(self, days: int = 90, tz_minutes: int = 210, max_items: int = 80) -> Dict[str, Any]:
        """Everything the dashboard's AI tab shows (journal only; never touches MT5)."""
        out: Dict[str, Any] = {"status": self.status(), "report": None, "items": [], "days": days}
        if not self.has_journal():
            return out
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = self._open_journal().rows(since)
        rep = evaluate(rows)
        rep.pop("items", None)
        out["report"] = rep
        shift = timedelta(minutes=tz_minutes)
        items = []
        for r in reversed(rows[-max_items:]):
            try:
                created = datetime.strptime(str(r["created_utc"])[:19], "%Y-%m-%d %H:%M:%S") + shift
                when = created.strftime("%m-%d %H:%M")
            except ValueError:
                when = str(r.get("created_utc"))
            sim = r.get("sim_profit") if r.get("sim_exit") != "no_data" else None
            items.append({
                "time": when, "side": r.get("side"), "layer": r.get("layer"), "mode": r.get("mode"),
                "status": r.get("status"), "decision": r.get("decision"),
                "confidence": r.get("confidence"), "p_protect": r.get("p_protect"),
                "reasons": json.loads(r.get("reasons_json") or "[]"),
                "action": r.get("action"), "action_fa": ACTIONS_FA.get(r.get("action") or "", r.get("action")),
                "error": r.get("error") if r.get("status") != "ok" else "",
                "actual_profit": r.get("profit"),
                "sim_profit": sim, "sim_exit": r.get("sim_exit"), "sim_done": bool(r.get("sim_done")),
                "correct": (correctness(r["decision"], sim)
                            if r.get("status") == "ok" and r.get("sim_done") and sim is not None else None),
            })
        out["items"] = items
        return out

    # ----------------------------------------------------------------- status
    def status(self) -> Dict[str, Any]:
        cfg = self._cfg()
        with self._lock:
            out: Dict[str, Any] = {
                "mode": self._mode, "mode_fa": MODES_FA.get(self._mode, self._mode),
                "source": self._source, "active_mode": self.active_mode(),
                "disabled_reason": self._disabled_reason,
                "model": str(getattr(self._provider, "model", "") or cfg.model),
                "prompt_version": self.prompt_version, "fail_policy": cfg.fail_policy,
                "block_min_confidence": cfg.block_min_confidence,
                "timeout_seconds": cfg.timeout_seconds,
                "max_calls_per_day": cfg.max_calls_per_day,
                "max_cost_usd_per_day": cfg.max_cost_usd_per_day,
            }
            journal_exists = not self._closed and (
                self._journal is not None or os.path.exists(self._journal_path))
        out["journal"] = None
        if journal_exists:
            try:
                out["journal"] = self._open_journal().summary(30)
            except Exception as exc:
                logger.warning("AI gate summary failed: {}", exc)
        return out

    def shutdown(self) -> None:
        with self._lock:
            executor, self._executor = self._executor, None
            journal, self._journal = self._journal, None
            self._ready = False
            self._closed = True
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        if journal is not None:
            journal.close()
