"""Kalshi Trade API v2 REST client.

Public market-data endpoints work unauthenticated; portfolio endpoints are
signed with RSA-PSS.  Includes token-bucket rate limiting and retry with
backoff for 429/5xx/network errors.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse
from typing import Any, Iterator

import requests

from .auth import KalshiSigner
from .config import AppConfig

log = logging.getLogger("kalshi.client")


class KalshiApiError(RuntimeError):
    def __init__(self, status: int, message: str, path: str = ""):
        super().__init__(f"[{status}] {message} ({path})")
        self.status = status
        self.path = path


class KalshiAuthError(KalshiApiError):
    pass


class KalshiRateLimitError(KalshiApiError):
    pass


class _TokenBucket:
    def __init__(self, rate_per_sec: float):
        self.rate = max(rate_per_sec, 0.5)
        self.capacity = max(int(self.rate), 1)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def take(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
            time.sleep(0.05)


class KalshiClient:
    def __init__(
        self,
        cfg: AppConfig,
        signer: KalshiSigner | None = None,
        session: requests.Session | None = None,
    ):
        self.cfg = cfg
        self.base = cfg.base_url.rstrip("/")
        self.signer = signer
        self.session = session or requests.Session()
        self.bucket = _TokenBucket(cfg.rate_limit_per_sec)
        self.timeout = cfg.request_timeout_s

    # ------------------------------------------------------------- plumbing ---
    def _path(self, endpoint: str) -> str:
        return f"{self.base}{endpoint}"

    def request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        max_retries: int = 4,
    ) -> dict[str, Any]:
        path_only = urllib.parse.urlparse(self._path(endpoint)).path
        headers = self.signer.headers(method, path_only) if self.signer else {"Accept": "application/json"}
        url = self._path(endpoint)
        backoff = 1.0
        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            self.bucket.take()
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body,
                    headers=headers, timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_err = exc
                log.warning("network error on %s %s (attempt %d): %s", method, endpoint, attempt + 1, exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("retry-after")
                delay = backoff
                if retry_after:
                    try:
                        parsed = float(retry_after)
                        if parsed > 0:
                            delay = parsed
                    except ValueError:
                        pass  # non-numeric retry-after -> keep exponential backoff
                log.warning("retryable %d on %s, sleeping %.1fs", resp.status_code, endpoint, delay)
                time.sleep(delay)
                backoff = min(backoff * 2, 30)
                last_err = KalshiApiError(resp.status_code, resp.text[:300], endpoint)
                continue
            if resp.status_code in (401, 403):
                raise KalshiAuthError(resp.status_code, resp.text[:300], endpoint)
            if resp.status_code >= 400:
                raise KalshiApiError(resp.status_code, resp.text[:300], endpoint)
            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()
        raise last_err or KalshiApiError(0, "exhausted retries", endpoint)

    # ----------------------------------------------------------- market data ---
    def get_markets(
        self,
        status: str = "open",
        limit: int = 200,
        max_pages: int = 5,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        mve_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"status": status, "limit": min(limit, 1000)}
            if cursor:
                params["cursor"] = cursor
            if event_ticker:
                params["event_ticker"] = event_ticker
            if series_ticker:
                params["series_ticker"] = series_ticker
            if mve_filter:
                params["mve_filter"] = mve_filter
            data = self.request("GET", "/markets", params=params)
            out.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor or not data.get("markets"):
                break
        return out

    def get_market(self, ticker: str) -> dict[str, Any]:
        return self.request("GET", f"/markets/{ticker}").get("market", {})

    def get_orderbook(self, ticker: str, depth: int | None = None) -> dict[str, Any]:
        params = {"depth": depth} if depth else None
        return self.request("GET", f"/markets/{ticker}/orderbook", params=params)

    def get_candlesticks(self, ticker: str, start_ts: int, end_ts: int,
                         period_interval: int = 60) -> list[dict[str, Any]]:
        data = self.request(
            "GET", f"/markets/{ticker}/candlesticks",
            params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )
        return data.get("market_candlesticks", [])

    def get_events(self, status: str = "open", limit: int = 200) -> list[dict[str, Any]]:
        return self.request("GET", "/events", params={"status": status, "limit": limit}).get("events", [])

    def exchange_status(self) -> dict[str, Any]:
        return self.request("GET", "/exchange/status")

    # ------------------------------------------------------------- portfolio ---
    def get_balance(self) -> dict[str, Any]:
        return self.request("GET", "/portfolio/balance")

    def get_positions(self, status: str | None = "open") -> list[dict[str, Any]]:
        params = {"status": status} if status else None
        return self.request("GET", "/portfolio/positions", params=params).get("market_positions", [])

    def get_fills(self, min_ts: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if min_ts:
            params["min_ts"] = min_ts
        return self.request("GET", "/portfolio/fills", params=params).get("fills", [])

    def get_orders(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return self.request("GET", "/portfolio/orders", params=params).get("orders", [])

    def create_order(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/portfolio/orders", json_body=body)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self.request("DELETE", f"/portfolio/orders/{order_id}/cancel")

    def cancel_all_orders(self) -> int:
        canceled = 0
        for od in self.get_orders(status="resting"):
            try:
                self.cancel_order(od["order_id"])
                canceled += 1
            except KalshiApiError as exc:
                log.warning("cancel failed for %s: %s", od.get("order_id"), exc)
        return canceled
