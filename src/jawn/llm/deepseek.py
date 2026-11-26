"""DeepSeek provider (OpenAI-compatible Chat Completions).

Model identifier verified 2026-11-26 two ways: GET https://api.deepseek.com/models (free,
lists ``deepseek-flash`` and ``deepseek-v4-pro``) and the pricing docs (``deepseek-flash`` ->
DeepSeek-V4.1-Flash-1017). ``deepseek-chat``/``deepseek-reasoner`` were scheduled for
retirement on 2026-07-24 and are not used.

Spend safety: a request is only sent when BOTH ``llm.deepseek.allow_paid = true`` in the
experiment config AND the environment variable ``JAWN_ALLOW_PAID_CALLS=1`` are set, and the
budget meter has reserved the worst-case cost. Otherwise ``complete`` raises
``paid_calls_disabled`` before any network I/O.

Requests: thinking disabled (``{"thinking": {"type": "disabled"}}``), temperature 0, small
max_tokens, and either strict tool calling (``/beta`` base URL, ``strict: true``) or JSON mode.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import aiohttp

from ..config import DeepSeekConfig
from . import pricing
from .base import DecisionRequest, ProviderError, ProviderResult, Usage, approx_tokens

PAID_ENV = "JAWN_ALLOW_PAID_CALLS"


class DeepSeekProvider:
    name = "deepseek"
    paid = True

    def __init__(self, cfg: DeepSeekConfig, api_key: str | None = None, now: Callable[[], datetime] | None = None, sleep: Callable[[float], Any] | None = None) -> None:
        self.cfg = cfg
        self._key = api_key if api_key is not None else os.environ.get(cfg.api_key_env, "")
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or asyncio.sleep
        u = urlparse(cfg.base_url)
        if u.scheme != "https" and (u.hostname or "") not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("DeepSeek base_url must be https (http allowed only for a local fake server)")
        base = cfg.base_url.rstrip("/")
        if cfg.mode == "tools_strict" and not base.endswith("/beta"):
            base += "/beta"
        self.endpoint = base + "/chat/completions"
        self._session: aiohttp.ClientSession | None = None
        self.sent = 0  # number of HTTP requests actually sent

    def __repr__(self) -> str:
        return f"DeepSeekProvider(model={self.cfg.model!r}, key={'set' if self._key else 'unset'})"

    def paid_calls_enabled(self) -> bool:
        return bool(self.cfg.allow_paid and os.environ.get(PAID_ENV) == "1")

    def build_body(self, req: DecisionRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": req.messages,
            "max_tokens": min(req.max_tokens, self.cfg.max_tokens),
            "temperature": self.cfg.temperature,
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        if self.cfg.mode == "tools_strict":
            body["tools"] = [req.tool]
            body["tool_choice"] = {"type": "function", "function": {"name": req.tool["function"]["name"]}}
        else:
            body["response_format"] = {"type": "json_object"}
        return body

    def estimate_max_cost(self, req: DecisionRequest) -> float:
        text = json.dumps(self.build_body(req))
        return pricing.worst_case(approx_tokens(text), min(req.max_tokens, self.cfg.max_tokens), self.cfg.pricing)

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.cfg.timeout_s))
        return self._session

    async def complete(self, req: DecisionRequest) -> ProviderResult:
        if not self.paid_calls_enabled():
            raise ProviderError("paid_calls_disabled", f"set llm.deepseek.allow_paid=true and {PAID_ENV}=1 to allow paid calls")
        if not self._key:
            raise ProviderError("auth", f"no API key in ${self.cfg.api_key_env}")
        body = self.build_body(req)
        last: ProviderError | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                return await self._once(body)
            except ProviderError as e:
                last = e
                if not e.retryable or attempt == self.cfg.max_retries:
                    raise
                await self._sleep(min(8.0, 1.0 * 2**attempt))
        assert last is not None
        raise last

    async def _once(self, body: dict[str, Any]) -> ProviderResult:
        sess = await self._sess()
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        t0 = self._now()
        try:
            self.sent += 1
            async with sess.post(self.endpoint, data=json.dumps(body), headers=headers) as resp:
                text = (await resp.text()).strip()  # server may send keep-alive blank lines first
                status = resp.status
        except asyncio.TimeoutError:
            raise ProviderError("timeout", f"no response within {self.cfg.timeout_s}s", retryable=True) from None
        except aiohttp.ClientError as e:
            raise ProviderError("server", f"connection error {type(e).__name__}", retryable=True) from None
        if status != 200:
            detail = text[:300]
            if status == 401:
                raise ProviderError("auth", "authentication failed", status=status)
            if status == 402:
                raise ProviderError("budget", "insufficient balance (HTTP 402)", status=status)
            if status == 429:
                raise ProviderError("rate_limit", detail, retryable=True, status=status)
            if status in (500, 502, 503, 504):
                raise ProviderError("server", f"HTTP {status}", retryable=True, status=status)
            raise ProviderError("bad_request", f"HTTP {status}: {detail}", status=status)
        try:
            data = json.loads(text)
            choice = data["choices"][0]
            msg = choice.get("message") or {}
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
            raise ProviderError("server", f"unparseable response: {type(e).__name__}", retryable=True) from None
        raw = ""
        calls = msg.get("tool_calls") or []
        if calls:
            raw = (calls[0].get("function") or {}).get("arguments") or ""
        elif isinstance(msg.get("content"), str):
            raw = msg["content"]  # JSON mode; may be blank/whitespace (documented caveat)
        u = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(u.get("prompt_tokens", 0)),
            cache_hit_tokens=int(u.get("prompt_cache_hit_tokens", 0)),
            cache_miss_tokens=int(u.get("prompt_cache_miss_tokens", 0)),
            completion_tokens=int(u.get("completion_tokens", 0)),
            reasoning_tokens=int((u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0),
            simulated=False,
        )
        usage.cost_usd = pricing.cost(usage, self.cfg.pricing, t0)
        return ProviderResult(raw=raw, usage=usage, model=str(data.get("model", self.cfg.model)), finish_reason=str(choice.get("finish_reason", "")), extra={"peak": pricing.is_peak(t0, self.cfg.pricing)})

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
