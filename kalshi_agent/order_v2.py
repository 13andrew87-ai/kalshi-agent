"""Kalshi v2 order helpers.

V2 (``POST /trade-api/v2/portfolio/events/orders``) quotes everything from the
YES side of the market as a single book: ``bid`` = buy YES, ``ask`` = sell YES,
price = fixed-point dollars, quantities are decimal strings. Buying NO is
economically equivalent to selling YES at ``1 - price`` (likewise selling NO is
buying YES), so we convert at this boundary.
"""
from __future__ import annotations

from typing import Any

TIF_MAP = {"ioc": "immediate_or_cancel", "gtc": "good_till_canceled",
           "fok": "fill_or_kill"}


def build_v2_order_body(*, ticker: str, action: str, side: str, count: int,
                        price_cents: int, order_type: str,
                        client_order_id: str) -> dict[str, Any]:
    action = (action or "buy").lower()
    side = (side or "yes").lower()
    leg_cents = int(price_cents)          # YES-leg price in cents
    if side == "no":
        leg_cents = 100 - int(price_cents)
    if side == "yes":
        leg_side = "bid" if action == "buy" else "ask"
    else:  # no leg: buying NO == selling YES, selling NO == buying YES
        leg_side = "ask" if action == "buy" else "bid"
    return {
        "ticker": ticker,
        "side": leg_side,
        "count": f"{int(count):.2f}",
        "price": f"{leg_cents / 100:.4f}",
        "time_in_force": TIF_MAP.get(order_type, "immediate_or_cancel"),
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": client_order_id,
        "post_only": False,
        "cancel_order_on_pause": False,
        "reduce_only": False,
        "subaccount": 0,
    }


def parse_v2_response(resp: dict[str, Any]) -> tuple[str, int, int]:
    """Normalize v2 (flat) and legacy v1 (nested under "order") create responses."""
    order = resp.get("order", resp) if isinstance(resp, dict) else {}
    if not isinstance(order, dict):
        order = {}
    oid = str(order.get("order_id") or resp.get("order_id") or "")
    raw = order.get("fill_count", None)
    if raw is None:
        raw = order.get("taker_fill_count", order.get("filled_count", 0)) or 0
    try:
        filled = int(float(raw))
    except (TypeError, ValueError):
        filled = 0
    try:
        remaining = int(float(order.get("remaining_count", 0) or 0))
    except (TypeError, ValueError):
        remaining = 0
    return oid, filled, remaining
