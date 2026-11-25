"""Order execution brokers.

PaperBroker — fills intents against the (real or simulated) order book with
Kalshi-style taker fees, IOC semantics, no slippage beyond the book.  This is
the default and the way to prove an idea before risking money.

LiveBroker — sends real orders to Kalshi.  Heavily gated:
  - config.live_trading_enabled must be true
  - CLI requires --live and KALSHI_LIVE_CONFIRM=YES
  - dry_run mode logs the exact order it *would* send
  - IOC limit orders only (fill-now-or-cancel): no resting-order management
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Protocol

from .config import AppConfig
from .models import Fill, Intent, Market, OrderBook, order_fee_cents

log = logging.getLogger("kalshi.broker")


def _fp_count(*vals) -> int:
    """Parse a fixed-point contract count ("2.00" -> 2).  Never rescales."""
    for v in vals:
        if v in (None, ""):
            continue
        try:
            return int(float(v))
        except (TypeError, ValueError):
            continue
    return 0


def _fp_price_cents(fr: dict, default: int) -> int:
    """Parse a fill price across schema generations.

    Legacy: integer cents in "price".  Live: dollar strings in
    "price_dollars" (and "price" may itself be a dollar string)."""
    p = fr.get("price")
    if p not in (None, ""):
        try:
            f = float(p)
            return int(round(f * 100)) if 0 < f <= 1.0 else int(round(f))
        except (TypeError, ValueError):
            pass
    pd_ = fr.get("price_dollars")
    if pd_ not in (None, ""):
        try:
            return int(round(float(pd_) * 100))
        except (TypeError, ValueError):
            pass
    return default


def _fp_fee_cents(fr: dict) -> int:
    """Parse a fill fee across schema generations.

    *_dollars / bare float < $1 -> convert to cents; *_cents keys -> cents."""
    for k in ("fee_cents", "total_fee_cents"):
        v = fr.get(k)
        if v not in (None, ""):
            try:
                return int(round(float(v)))
            except (TypeError, ValueError):
                pass
    for k in ("total_fee_dollars", "fee_dollars", "total_fee", "fee"):
        v = fr.get(k)
        if v not in (None, ""):
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if k.endswith("dollars") or f < 1.0:
                return int(round(f * 100))
            return int(round(f))
    return 0


class MarketDataSource(Protocol):
    def get_markets(self, **kw) -> list[dict]: ...
    def get_orderbook(self, ticker: str, **kw) -> dict: ...
    def get_candlesticks(self, ticker: str, start_ts: int, end_ts: int,
                         period_interval: int = 60) -> list[dict]: ...


class PaperBroker:
    """Fill intents against the book at the touch.  IOC semantics."""

    kind = "paper"

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def execute(self, intent: Intent, book: OrderBook, portfolio: Any) -> list[Fill]:
        if intent.count <= 0:
            return []
        side = intent.side
        # executable levels: buying = walk asks; selling = walk bids
        levels = (book.yes_asks if side == "yes" else book.no_asks) if intent.action == "buy" \
            else (book.yes_bids if side == "yes" else book.no_bids)
        remaining = intent.count
        fills: list[Fill] = []
        for lv in levels:
            if remaining <= 0:
                break
            if intent.action == "buy" and lv.price_cents > intent.price_cents:
                break
            if intent.action == "sell" and lv.price_cents < intent.price_cents:
                break
            take = min(remaining, lv.quantity)
            if take <= 0:
                continue
            fee = order_fee_cents(take, lv.price_cents, self.cfg.taker_fee_multiplier)
            f = Fill(
                ticker=intent.ticker, side=side, action=intent.action, count=take,
                price_cents=lv.price_cents, fee_cents=fee, ts=time.time(),
                order_id=f"paper-{int(time.time() * 1000)}", kind=self.kind,
            )
            portfolio.apply_fill(f)
            fills.append(f)
            remaining -= take
            log.info("PAPER FILL %s %s %d x %dc fee=%dc %s",
                     intent.ticker, f"{intent.action} {side}", take, lv.price_cents, fee, intent.rationale)
        if remaining > 0:
            log.info("paper IOC unfilled remainder %d %s %s (insufficient depth @ <=%dc)",
                     remaining, intent.action, intent.ticker, intent.price_cents)
        return fills


class LiveBroker:
    """Real order placement against Kalshi.  IOC limit orders only."""

    kind = "live"

    def __init__(self, cfg: AppConfig, client: Any):
        self.cfg = cfg
        self.client = client

    def execute(self, intent: Intent, book: OrderBook, portfolio: Any) -> list[Fill]:
        if intent.count <= 0:
            return []
        if self.cfg.dry_run:
            log.warning("DRY-RUN (no order sent): %s %s %d %s @ %dc | %s",
                        intent.action, intent.side, intent.count, intent.ticker,
                        intent.price_cents, intent.rationale)
            return []
        tif_map = {"ioc": "immediate_or_cancel", "gtc": "good_till_canceled",
                   "fok": "fill_or_kill"}
        body: dict[str, Any] = {
            "ticker": intent.ticker,
            "type": "limit",
            "action": intent.action,
            "side": intent.side,
            "count": intent.count,
            "time_in_force": tif_map.get(self.cfg.order_type, "immediate_or_cancel"),
            "client_order_id": intent.client_order_id(),
        }
        if intent.side == "yes":
            body["yes_price"] = intent.price_cents
        else:
            body["no_price"] = intent.price_cents
        log.warning("LIVE ORDER: %s", body)
        resp = self.client.create_order(body)
        order = resp.get("order", {})
        filled = int(order.get("taker_fill_count") or order.get("filled_count") or 0)
        fills: list[Fill] = []
        if filled > 0:
            # fetch authoritative fills from the exchange
            try:
                matched = 0
                min_ts = int(time.time()) - 120
                for fr in self.client.get_fills(min_ts=min_ts):
                    if fr.get("ticker") != intent.ticker:
                        continue
                    oid_fr, oid_order = fr.get("order_id"), order.get("order_id")
                    if oid_fr and oid_order and str(oid_fr) != str(oid_order):
                        continue
                    count = _fp_count(fr.get("count_fp"), fr.get("count"), fr.get("quantity"))
                    if count <= 0:
                        continue
                    f = Fill(
                        ticker=intent.ticker,
                        side=fr.get("side", intent.side),
                        action=fr.get("action", intent.action),
                        count=count,
                        price_cents=_fp_price_cents(fr, intent.price_cents),
                        fee_cents=_fp_fee_cents(fr), ts=time.time(),
                        order_id=str(oid_order or oid_fr or ""), kind=self.kind,
                    )
                    portfolio.apply_fill(f)
                    fills.append(f)
                    matched += count
                if not fills:
                    log.error("LIVE FILL RECONCILIATION MISS: order %s reported filled=%s but "
                              "no matching fill records returned — portfolio state may drift; "
                              "check the Kalshi dashboard", order.get("order_id"), filled)
                elif matched != int(filled):
                    log.warning("LIVE FILL PARTIAL RECONCILE: exchange reported %s filled, "
                                "reconciled %s contract(s) for %s", filled, matched, intent.ticker)
            except Exception as exc:  # noqa: BLE001
                log.error("could not reconcile fills for %s: %s", intent.ticker, exc)
        else:
            log.info("IOC order %s did not execute (status=%s)", order.get("order_id"), order.get("status"))
        return fills


def make_broker(cfg: AppConfig, client: Any | None):
    if cfg.mode == "live":
        if not cfg.live_trading_enabled:
            raise ValueError("live mode requires live_trading_enabled: true in config")
        if client is None:
            raise ValueError("live mode requires an authenticated KalshiClient")
        return LiveBroker(cfg, client)
    return PaperBroker(cfg)


def clamp_book_price(p: float) -> int:
    return int(min(99, max(1, math.floor(p))))
