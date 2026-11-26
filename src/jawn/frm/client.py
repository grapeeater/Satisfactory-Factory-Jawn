"""Typed-ish async client for Ficsit Remote Monitoring.

Safety properties:
  * endpoint allowlists for reads and writes (anything else raises before any I/O)
  * loopback-only by default (FRM reads are unauthenticated; never reach across the LAN)
  * GETs retry on transient failures; POSTs never retry (no duplicate writes)
  * the auth token is never included in exceptions, reprs or logs
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class FrmError(Exception):
    """kind: timeout | connection | http | malformed | auth | not_allowed | not_found"""

    def __init__(self, kind: str, endpoint: str, detail: str = "", status: int | None = None) -> None:
        super().__init__(f"FRM {kind} on {endpoint}: {detail}" + (f" (HTTP {status})" if status else ""))
        self.kind = kind
        self.endpoint = endpoint
        self.status = status
        self.detail = detail


@dataclass
class CallStats:
    gets: int = 0
    posts: int = 0
    get_failures: int = 0
    post_failures: int = 0
    retries: int = 0


class FrmClient:
    def __init__(
        self,
        base_url: str,
        token: str | None,
        read_allowlist: list[str],
        write_allowlist: list[str],
        timeout_s: float = 3.0,
        get_retries: int = 1,
        allow_remote: bool = False,
    ) -> None:
        host = urlparse(base_url).hostname or ""
        if host not in LOOPBACK and not allow_remote:
            raise ValueError(f"refusing non-loopback FRM host {host!r} (set frm.allow_remote to override)")
        self.base_url = base_url.rstrip("/")
        self._token = token or ""
        self.read_allowlist = set(read_allowlist)
        self.write_allowlist = set(write_allowlist)
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self.get_retries = max(0, get_retries)
        self.stats = CallStats()
        self._session: aiohttp.ClientSession | None = None

    def __repr__(self) -> str:  # never show the token
        return f"FrmClient({self.base_url!r}, token={'set' if self._token else 'unset'})"

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # force_close: FRM answers with Connection: close; avoids stale keep-alive sockets
            self._session = aiohttp.ClientSession(timeout=self.timeout, connector=aiohttp.TCPConnector(force_close=True, limit=8))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _once(self, method: str, endpoint: str, body: Any = None) -> Any:
        sess = await self._sess()
        url = f"{self.base_url}/{endpoint}"
        headers = {"Content-Type": "application/json"}
        if method == "POST":
            headers["X-FRM-Authorization"] = self._token
        try:
            async with sess.request(method, url, data=None if body is None else json.dumps(body), headers=headers) as resp:
                text = await resp.text()
                if resp.status == 401:
                    raise FrmError("auth", endpoint, "unauthorized (check FRM token)", resp.status)
                if resp.status == 404:
                    raise FrmError("not_found", endpoint, text[:200], resp.status)
                if resp.status >= 400:
                    raise FrmError("http", endpoint, text[:200], resp.status)
                try:
                    return json.loads(text) if text else None
                except json.JSONDecodeError as e:
                    raise FrmError("malformed", endpoint, f"invalid JSON: {e.msg}") from None
        except asyncio.TimeoutError:
            raise FrmError("timeout", endpoint, f"no response within {self.timeout.total}s") from None
        except aiohttp.ClientError as e:
            raise FrmError("connection", endpoint, type(e).__name__) from None
        except ConnectionError as e:
            raise FrmError("connection", endpoint, type(e).__name__) from None

    async def get(self, endpoint: str) -> Any:
        if endpoint not in self.read_allowlist:
            raise FrmError("not_allowed", endpoint, "endpoint not in read allowlist")
        last: FrmError | None = None
        for attempt in range(self.get_retries + 1):
            self.stats.gets += 1
            try:
                return await self._once("GET", endpoint)
            except FrmError as e:
                last = e
                self.stats.get_failures += 1
                transient = e.kind in ("timeout", "connection", "malformed") or (e.kind == "http" and (e.status or 0) >= 500)
                if not transient or attempt == self.get_retries:
                    raise
                self.stats.retries += 1
                await asyncio.sleep(0.05 * (attempt + 1))
        assert last is not None
        raise last

    async def post(self, endpoint: str, body: Any) -> Any:
        if endpoint not in self.write_allowlist:
            raise FrmError("not_allowed", endpoint, "endpoint not in write allowlist")
        if not self._token:
            raise FrmError("auth", endpoint, "no FRM token configured; writes disabled")
        self.stats.posts += 1
        try:
            return await self._once("POST", endpoint, body)
        except FrmError:
            self.stats.post_failures += 1
            raise
