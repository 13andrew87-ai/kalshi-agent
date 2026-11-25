"""Core dataclasses shared across the agent."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


def parse_ts(value: Any) -> datetime | None:
    """Parse Kalshi timestamps (ISO-8601, possibly with 'Z')."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    txt = str(value).strip()
    try:
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


@dataclass
class Market:
    ticker: str
    event_ticker: str = ""
    title: str = ""
    subtitle: str = ""
    category: str = ""
    status: str = "open"                       # normalized: open | closed | settled
    close_time: datetime | None = None
    open_time: datetime | None = None
    yes_bid: int = 0
    yes_ask: int = 0
    no_bid: int = 0
    no_ask: int = 0
    last_price: int = 0
    volume: int = 0
    open_interest: int = 0
    liquidity_cents: int = 0
    rules_primary: str = ""
    tick_size: int = 1
    settlement_value: int | None = None        # 100 = YES won, 0 = NO won
    is_provisional: bool = False               # unquoted combo/parlay markets

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Market":
        # Kalshi's live schema prices as dollar strings ("0.4250") and volumes
        # as fixed-point strings ("123.00"); older/demo responses use int cents.
        def cents(dollar_str: Any, legacy: Any) -> int:
            if dollar_str not in (None, ""):
                try:
                    return int(round(float(dollar_str) * 100))
                except (TypeError, ValueError):
                    pass
            return int(legacy or 0)

        def num(fp_str: Any, legacy: Any) -> int:
            if fp_str not in (None, ""):
                try:
                    return int(float(fp_str))
                except (TypeError, ValueError):
                    pass
            return int(legacy or 0)

        status = (d.get("status") or "open").lower()
        if status == "active":
            status = "open"
        settlement_value = None
        if d.get("settlement_value") is not None:
            settlement_value = int(d["settlement_value"])
        elif d.get("settlement_value_dollars") not in (None, ""):
            try:
                settlement_value = int(round(float(d["settlement_value_dollars"]) * 100))
            except (TypeError, ValueError):
                settlement_value = None
        elif (d.get("result") or "").lower() in ("yes", "no"):
            settlement_value = 100 if d["result"].lower() == "yes" else 0
        return cls(
            ticker=d.get("ticker", ""),
            event_ticker=d.get("event_ticker", ""),
            title=d.get("title", ""),
            subtitle=d.get("subtitle", "") or d.get("yes_sub_title", ""),
            category=d.get("category", "") or "General",
            status=status,
            close_time=parse_ts(d.get("close_time") or d.get("expected_expiration_time")),
            open_time=parse_ts(d.get("open_time")),
            yes_bid=cents(d.get("yes_bid_dollars"), d.get("yes_bid")),
            yes_ask=cents(d.get("yes_ask_dollars"), d.get("yes_ask")),
            no_bid=cents(d.get("no_bid_dollars"), d.get("no_bid")),
            no_ask=cents(d.get("no_ask_dollars"), d.get("no_ask")),
            last_price=cents(d.get("last_price_dollars"), d.get("last_price")),
            volume=num(d.get("volume_fp"), d.get("volume")),
            open_interest=num(d.get("open_interest_fp"), d.get("open_interest")),
            liquidity_cents=int(d.get("liquidity") or 0),
            rules_primary=d.get("rules_primary", ""),
            tick_size=int(d.get("tick_size") or 1),
            settlement_value=settlement_value,
            is_provisional=bool(d.get("is_provisional")),
        )

    @property
    def mid_cents(self) -> int:
        if self.yes_bid and self.yes_ask:
            return round((self.yes_bid + self.yes_ask) / 2)
        return self.last_price or round((self.yes_bid + self.yes_ask) / 2) or 50

    def hours_to_close(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        if self.close_time is None:
            return 1e9
        return max(0.0, (self.close_time - now).total_seconds() / 3600.0)


@dataclass
class BookLevel:
    price_cents: int
    quantity: int


@dataclass
class OrderBook:
    """A normalized view of the Kalshi order book.

    Kalshi's raw orderbook gives resting *bids* for YES ("yes") and resting
    *bids* for NO ("no").  We derive asks: buying YES at ask == selling at the
    best NO bid (price = 100 - no_bid), and vice versa.
    """

    ticker: str
    yes_bids: list[BookLevel] = field(default_factory=list)  # sorted best-first (desc price)
    no_bids: list[BookLevel] = field(default_factory=list)   # sorted best-first (desc price)

    @classmethod
    def from_api(cls, d: dict[str, Any], ticker: str = "") -> "OrderBook":
        raw = d.get("orderbook") or d.get("orderbook_fp") or d
        yes_raw = raw.get("yes") or raw.get("yes_dollars")
        no_raw = raw.get("no") or raw.get("no_dollars")
        return cls(
            ticker=ticker,
            yes_bids=_parse_levels(yes_raw),
            no_bids=_parse_levels(no_raw),
        )

    @property
    def yes_asks(self) -> list[BookLevel]:
        """Derived: best NO bid at 100-p is an executable YES ask."""
        out = [BookLevel(100 - lv.price_cents, lv.quantity) for lv in self.no_bids]
        return sorted(out, key=lambda lv: lv.price_cents)

    @property
    def no_asks(self) -> list[BookLevel]:
        out = [BookLevel(100 - lv.price_cents, lv.quantity) for lv in self.yes_bids]
        return sorted(out, key=lambda lv: lv.price_cents)

    def best_bid(self, side: str) -> BookLevel | None:
        bids = self.yes_bids if side == "yes" else self.no_bids
        return bids[0] if bids else None

    def best_ask(self, side: str) -> BookLevel | None:
        asks = self.yes_asks if side == "yes" else self.no_asks
        return asks[0] if asks else None

    def depth_at(self, side: str, price_cents: int) -> int:
        """Total quantity resting at <= price (asks) or >= price (bids)."""
        total = 0
        levels = (self.yes_asks if side == "yes" else self.no_asks) if price_cents else []
        for lv in levels:
            if lv.price_cents <= price_cents:
                total += lv.quantity
        return total

    def spread_cents(self) -> int | None:
        ba, aa = self.best_bid("yes"), self.best_ask("yes")
        if ba and aa:
            return aa.price_cents - ba.price_cents
        return None

    def mid_cents(self) -> float | None:
        ba, aa = self.best_bid("yes"), self.best_ask("yes")
        if ba and aa:
            return (ba.price_cents + aa.price_cents) / 2
        return None


def _parse_levels(raw: Any) -> list[BookLevel]:
    """Defensively parse Kalshi order-book levels across schema generations.

    Live schema: nested [price_dollar_string, size_string] pairs, e.g.
        [["0.3800", "19031.00"], ["0.3700", "502.00"]]  (prices in DOLLARS,
        sizes possibly fractional).  Legacy/demo schema: integer cents with
        compressed flattened rows.  We accept any nesting and both scales.
    """
    scalars: list[Any] = []

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float, str)):
            scalars.append(node)
        elif isinstance(node, (list, tuple)):
            for el in node:
                walk(el)

    walk(raw)
    levels: list[BookLevel] = []
    for i in range(0, len(scalars) - 1, 2):
        try:
            p_f = float(scalars[i])
            q_f = float(scalars[i + 1])
        except (TypeError, ValueError):
            continue
        # "0.3800" -> 38c ; legacy 38 -> 38c ; a pinned $1.00 side -> 100c (reject)
        price = int(p_f * 100 + 0.5) if p_f <= 1.0 else int(p_f + 0.5)
        qty = int(q_f)
        if 1 <= price <= 99 and qty > 0:
            levels.append(BookLevel(price_cents=price, quantity=qty))
    levels.sort(key=lambda lv: lv.price_cents, reverse=True)
    return levels


@dataclass
class Candle:
    ts: int                 # unix seconds
    open_c: int
    close_c: int
    high_c: int
    low_c: int
    volume: int = 0


@dataclass
class Prediction:
    """Ensemble output for one market."""

    ticker: str
    p_blend: float
    confidence: float
    components: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class Signal:
    ticker: str
    side: str = ""                                  # "yes" | "no" | ""
    action: str = ""                                # "buy" | "sell" (exit) | ""
    exec_price_cents: int = 0
    edge_cents: float = 0.0                         # after fees
    ev_cents: float = 0.0                           # per contract, after fees
    kelly_contracts: int = 0
    depth_contracts: int = 0
    tradeable: bool = False
    skip_reason: str = ""
    reasons: list[str] = field(default_factory=list)
    prediction: Prediction | None = None
    fee_cents_per_contract: float = 0.0


@dataclass
class Intent:
    """A concrete order request produced by strategy+risk, ready for a broker."""

    ticker: str
    side: str                  # "yes" | "no"
    action: str                # "buy" | "sell"
    count: int
    price_cents: int           # limit price
    rationale: str = ""
    signal: Signal | None = None

    def client_order_id(self) -> str:
        import uuid

        stem = f"ka-{self.action}-{self.side[:1]}-"
        return (stem + uuid.uuid4().hex[:16])[:32]


@dataclass
class Fill:
    ticker: str
    side: str
    action: str
    count: int
    price_cents: int
    fee_cents: int
    ts: float = 0.0
    order_id: str = ""
    kind: str = "paper"         # paper | live | exit


@dataclass
class Position:
    ticker: str
    yes_count: int = 0
    no_count: int = 0
    avg_yes_price_cents: float = 0.0
    avg_no_price_cents: float = 0.0
    realized_pnl_cents: float = 0.0

    def value_cents(self, mark_yes_cents: int) -> float:
        mark_yes = clamp_cents(mark_yes_cents)
        return self.yes_count * mark_yes + self.no_count * (100 - mark_yes)

    def basis_cents(self) -> float:
        return self.yes_count * self.avg_yes_price_cents + self.no_count * self.avg_no_price_cents


def clamp_cents(p: float, lo: int = 0, hi: int = 100) -> int:
    return int(min(hi, max(lo, round(p))))


def kelly_contracts(bankroll_cents: float, p: float, price_cents: int,
                    fee_mult: float, kelly_fraction: float) -> tuple[int, float]:
    """Fractional-Kelly sizing for a binary contract.

    Cost per contract c (dollars) = price + taker fee.  Win pays $1.
    Exact Kelly optimum: n* = W * (p - c) / (c * (1-c))  [contracts],
    where W = bankroll in dollars.  We return floor(n* * kelly_fraction).

    Returns (contracts, fee_per_contract_cents).
    """
    p = min(max(p, 1e-9), 1 - 1e-9)
    price_d = price_cents / 100.0
    fee_c = fee_mult * price_d * (1.0 - price_d)          # dollars per contract
    c = price_d + fee_c
    if c <= 0 or c >= 1 or p <= c:
        return 0, fee_c * 100
    W = bankroll_cents / 100.0
    n_star = W * (p - c) / (c * (1.0 - c))
    n = int(math.floor(n_star * kelly_fraction))
    return max(n, 0), fee_c * 100


def order_fee_cents(count: int, price_cents: int, fee_mult: float) -> int:
    """Kalshi-style taker fee, rounded up to the cent.

    fees = ceil(fee_mult * C * P * (1-P))  with P in dollars, converted to cents.
    """
    if count <= 0 or not (0 < price_cents < 100):
        return 0
    p = price_cents / 100.0
    dollars = fee_mult * count * p * (1.0 - p)
    return int(math.ceil(dollars * 100)) if dollars > 0 else 0


def _num(v: Any) -> int:
    """Parse ints from int/float/dollar-string PRICES ("0.4200" -> 42).

    NOTE: this scales sub-$1 values by 100, which is right for prices but
    wrong for fixed-point quantities — use _fp_int for volumes/counts."""
    if v in (None, ""):
        return 0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0
    return int(f * 100 + 0.5) if 0 < f <= 1.0 else int(f + 0.5)


def _fp_int(v: Any) -> int:
    """Parse fixed-point quantities ("123.00" -> 123).  Never rescales."""
    if v in (None, ""):
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def merge_candles(rows: Iterable[dict[str, Any]]) -> list[Candle]:
    out: list[Candle] = []
    for r in rows:
        out.append(
            Candle(
                ts=int(r.get("ts") or 0),
                open_c=_num(r.get("price_open")),
                close_c=_num(r.get("price_close")),
                high_c=_num(r.get("price_high")),
                low_c=_num(r.get("price_low")),
                volume=_fp_int(r.get("volume") or r.get("volume_fp")),
            )
        )
    out.sort(key=lambda c: c.ts)
    return out
