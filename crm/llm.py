"""The one place OpenAI is called. Models are pinned in config; every call logs tokens, cost, and latency.

Reasoning tokens are billed as output tokens, so `output_tokens` already includes them (the API's
completion_tokens). They are logged separately so effort settings can be compared on cost.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .config import Settings

_LOG_LOCK = threading.Lock()


@dataclass
class LLMResult:
    text: str
    model: str
    effort: Optional[str]
    input_tokens: int
    output_tokens: int  # includes reasoning tokens
    reasoning_tokens: int
    cost_usd: float
    latency_ms: int


def cost_usd(settings: Settings, model: str, in_tokens: int, out_tokens: int) -> float:
    p_in, p_out = settings.price(model)
    return (in_tokens * p_in + out_tokens * p_out) / 1_000_000


def complete(settings: Settings, conn: sqlite3.Connection, prompt: str, *, model: str, run_id: str, purpose: str,
             effort: Optional[str] = None, system: str = "", client=None) -> LLMResult:
    """One chat completion with a pinned model. `client` is injectable for tests; the real one is lazy."""
    if client is None:
        if not settings.openai_api_key:
            raise RuntimeError("missing OPENAI_API_KEY (see .env.example)")
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key)
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    kwargs = {"model": model, "messages": messages}
    if effort:
        kwargs["reasoning_effort"] = effort
    t0 = time.monotonic()
    resp = client.chat.completions.create(**kwargs)
    ms = int((time.monotonic() - t0) * 1000)
    usage = resp.usage
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = int(getattr(details, "reasoning_tokens", 0) or 0)
    res = LLMResult(
        text=resp.choices[0].message.content or "", model=model, effort=effort,
        input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens, reasoning_tokens=reasoning,
        cost_usd=cost_usd(settings, model, usage.prompt_tokens, usage.completion_tokens), latency_ms=ms,
    )
    with _LOG_LOCK:
        conn.execute(
            "INSERT INTO llm_calls (run_id, model, purpose, input_tokens, output_tokens, reasoning_tokens, "
            "effort, cost_usd, latency_ms) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, model, purpose, res.input_tokens, res.output_tokens, reasoning, effort, res.cost_usd, ms),
        )
        conn.commit()
    return res


def model_available(settings: Settings, model: str, client=None) -> None:
    """Raise if the pinned model id cannot be retrieved (used by preflight; no tokens are billed)."""
    if client is None:
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key)
    client.models.retrieve(model)


def run_spend(conn: sqlite3.Connection, run_id: str) -> float:
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE run_id = ?", (run_id,)).fetchone()
    return float(row[0])
