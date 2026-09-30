"""List the models the AI-gate API key can use, and ping candidates for a fallback.

    py -3.11 tools/ai_gate_models.py                      # list model ids (filtered by --grep)
    py -3.11 tools/ai_gate_models.py --ping m1 m2 ...     # one tiny request per model

Reads base_url / api_key_env from config/ai_gate.yaml (defaults + --bot override)
and the key from .env.  The key itself is never printed.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

import core  # noqa: E402,F401  (loads .env)
from core.ai_gate.config import load_config  # noqa: E402
from core.ai_gate.provider import redact  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default="ichimoku_m15")
    ap.add_argument("--grep", default="gemini", help="show only ids containing this text ('' = all)")
    ap.add_argument("--ping", nargs="*", default=None, metavar="MODEL")
    a = ap.parse_args()
    cfg = load_config(a.bot)
    key = cfg.api_key()
    if not key:
        print(f"no API key in env {cfg.api_key_env}")
        return 2
    base = cfg.base_url.rstrip("/")
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    print(f"endpoint: {base} | configured model: {cfg.model} | fallback: {list(cfg.fallback_models) or '-'}")
    if a.ping is None:
        try:
            r = requests.get(base + "/models", headers=hdr, timeout=30)
            ids = sorted(str(m.get("id", "")) for m in (r.json().get("data") or []))
        except Exception as exc:
            print("list failed:", redact(str(exc), key))
            return 1
        ids = [i.split("/")[-1] for i in ids if a.grep.lower() in i.lower()]
        print(f"{len(ids)} model(s):")
        for i in ids:
            print("  ", i)
        return 0
    for model in a.ping:
        body = {"model": model, "max_tokens": 300, "temperature": 0,
                "messages": [{"role": "user", "content": "Reply with the single word OK."}]}
        t0 = time.time()
        try:
            r = requests.post(base + "/chat/completions", json=body, headers=hdr, timeout=60)
            ms = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                text = (r.json()["choices"][0]["message"].get("content") or "").strip()[:40]
                print(f"  {model:<32} OK   {ms:>6} ms  reply={text!r}")
            else:
                print(f"  {model:<32} HTTP {r.status_code} {ms:>6} ms  {redact(r.text[:120], key)}")
        except Exception as exc:
            print(f"  {model:<32} FAIL {redact(str(exc)[:120], key)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
