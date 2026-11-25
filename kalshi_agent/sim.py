"""A deterministic simulated Kalshi-compatible exchange.

Generates binary markets whose hidden "true" probabilities evolve as
mean-reverting random walks with occasional news shocks.  Quotes synthetic
order books around the true probability plus noise, and produces synthetic
candlestick history — all shaped exactly like the real API responses, so the
same predictor / strategy / broker code paths run against both.

Purpose: safe end-to-end demos, tests, and parameter sweeps.  Ground truth is
known, so settlement and P&L are verifiable.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .models import Market, OrderBook, _parse_levels

CATEGORIES = ["Sports", "Politics", "Economics", "Weather", "Crypto", "Pop Culture"]
TEAM_A = ["Lakers", "Chiefs", "Yankees", "Rangers", "Wildcats", "Tigers", "Hawks", "Bulls"]
TEAM_B = ["Celtics", "49ers", "Mets", "Devils", "Bears", "Lions", "Falcons", "Jets"]
TOPICS = [
    "Will the Fed cut rates at the next meeting?",
    "Will the candidate win the Senate race?",
    "Will CPI print above 3%?",
    "Will NYC exceed 90F in July?",
    "Will BTC close above $100k this month?",
    "Will the album top the Billboard 200?",
]


@dataclass
class _SimState:
    ticker: str
    category: str
    title: str
    subtitle: str
    true_p: float
    base_p: float
    sigma: float                       # per-step vol of the hidden true probability
    spread_cents: int
    depth_per_level: int
    close_ts: float                    # unix seconds
    open_ts: float
    volume: int
    history: list[tuple[int, float]] = field(default_factory=list)  # (ts, quoted_mid)
    settled: bool = False
    settlement_value: int | None = None
    price_floor: float = 0.02
    price_ceiling: float = 0.98


class SimExchange:
    """Drop-in data source for the agent; also settles markets over time."""

    def __init__(
        self,
        seed: int = 42,
        n_markets: int = 18,
        now: datetime | None = None,
    ):
        self.rng = random.Random(seed)
        self.now = now or datetime.now(timezone.utc)
        self.now_ts = self.now.timestamp()
        self.states: dict[str, _SimState] = {}
        self.settlement_log: list[dict] = []
        self._book_cache: dict[str, tuple[list, list]] = {}   # quotes persist within a step
        for i in range(n_markets):
            cat = CATEGORIES[i % len(CATEGORIES)]
            base_p = round(self.rng.uniform(0.15, 0.85), 3)
            team_mode = cat == "Sports"
            title = f"{TEAM_A[i % len(TEAM_A)]} vs {TEAM_B[(i * 3) % len(TEAM_B)]}" if team_mode else TOPICS[i % len(TOPICS)]
            subtitle = f"Will {TEAM_A[i % len(TEAM_A)]} win?" if team_mode else title
            days_to_close = self.rng.uniform(0.5, 12)
            spread = self.rng.choice([1, 1, 2, 2, 3, 4])
            st = _SimState(
                ticker=f"SIM{i:03d}-{chr(65 + i % 26)}",
                category=cat,
                title=title,
                subtitle=subtitle,
                true_p=base_p,
                base_p=base_p,
                sigma=self.rng.uniform(0.004, 0.02),
                spread_cents=spread,
                depth_per_level=self.rng.choice([80, 150, 300, 500, 900]),
                close_ts=self.now_ts + days_to_close * 86400,
                open_ts=self.now_ts - self.rng.uniform(2, 30) * 86400,
                volume=self.rng.randint(500, 80_000),
            )
            # pre-seed 120 hours of history so momentum/reversion have data
            t0 = self.now_ts - 120 * 3600
            p = base_p
            for h in range(120):
                p = self._evolve(st, p, dt_hours=1.0)
                st.history.append((int(t0 + h * 3600), p * 100 + self.rng.gauss(0, 1.5)))
            st.true_p = p
            self.states[st.ticker] = st

    # ------------------------------------------------------------ evolution ---
    def _evolve(self, st: _SimState, p: float, dt_hours: float) -> float:
        reversion = 0.02 * dt_hours * (st.base_p - p)
        noise = self.rng.gauss(0.0, st.sigma * math.sqrt(max(dt_hours, 0.01)))
        shock = 0.0
        if self.rng.random() < 0.01 * dt_hours:            # rare news shock
            shock = self.rng.uniform(-0.12, 0.12)
        hours_left = max((st.close_ts - self.now_ts) / 3600.0, 0.0)
        converge = max(0.0, 1.0 - hours_left / 336.0)      # drift toward 0/1 near close
        drift = converge * (0.5 * (0.0 if p < 0.5 else 1.0) - p) * 0.02 * dt_hours
        q = p + reversion + noise + shock + drift
        return min(st.price_ceiling, max(st.price_floor, q))

    def step(self, hours: float) -> list[dict]:
        """Advance the world, return newly settled markets."""
        self.now_ts += hours * 3600
        self._book_cache.clear()          # world moved -> requote
        settled_now = []
        for st in self.states.values():
            if st.settled:
                continue
            st.true_p = self._evolve(st, st.true_p, hours)
            # history records a noisy traded-price mid, like real candlesticks
            st.history.append((int(self.now_ts), self.quoted_mid(st) + self.rng.gauss(0, 1.5)))
            st.volume += self.rng.randint(0, 300)
            if self.now_ts >= st.close_ts:
                st.settled = True
                outcome_yes = self.rng.random() < st.true_p
                st.settlement_value = 100 if outcome_yes else 0
                rec = {"ticker": st.ticker, "settlement_value": st.settlement_value,
                       "true_p": round(st.true_p, 4), "ts": int(self.now_ts)}
                self.settlement_log.append(rec)
                settled_now.append(rec)
        return settled_now

    def quoted_mid(self, st: _SimState) -> float:
        return st.true_p * 100

    # ------------------------------------------------- MarketDataSource API ---
    def get_markets(self, status: str = "open", limit: int = 200, **_kw) -> list[dict]:
        out = []
        for st in self.states.values():
            settled = st.settled
            if status == "open" and settled:
                continue
            out.append(self._market_dict(st))
        return out[:limit]

    def get_orderbook(self, ticker: str, depth: int | None = None, **_kw) -> dict:
        st = self.states[ticker]
        yes_bids, no_bids = self._quotes(st)
        # build compressed rows of up to 3 [price, qty] pairs like the real API
        def rows(levels: list[tuple[int, int]]) -> list[list[int]]:
            out, cur = [], []
            for p, q in levels:
                cur.extend([p, q])
                if len(cur) >= 6:
                    out.append(cur)
                    cur = []
            if cur:
                out.append(cur)
            return out

        return {"orderbook": {"yes": rows(yes_bids), "no": rows(no_bids)}}

    def _quotes(self, st: _SimState) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
        if st.ticker in self._book_cache:
            return self._book_cache[st.ticker]
        # Books drift around fair value with realistic inefficiency noise —
        # quotes routinely sit 2-6c off fair value on less liquid markets.
        # Quotes persist until the world steps, like a real resting book.
        mid = st.true_p * 100
        noise = self.rng.uniform(-6.0, 6.0)
        half = st.spread_cents / 2.0 + noise
        best_yes_bid = int(round(mid - half))
        best_yes_bid = min(98, max(1, best_yes_bid))
        levels_yes, levels_no = [], []
        for k in range(4):
            q = max(20, int(st.depth_per_level / (k + 1)))
            yb = best_yes_bid - k
            nb = 100 - best_yes_bid - k
            if 1 <= yb <= 99:
                levels_yes.append((yb, q))
            if 1 <= nb <= 99:
                levels_no.append((nb, q))
        self._book_cache[st.ticker] = (levels_yes, levels_no)
        return levels_yes, levels_no

    def _market_dict(self, st: _SimState) -> dict:
        yes_bids, no_bids = self._quotes(st)
        yb, ya = yes_bids[0][0], 100 - no_bids[0][0]
        nb, na = no_bids[0][0], 100 - yes_bids[0][0]
        status = "settled" if st.settled else "open"
        return {
            "ticker": st.ticker,
            "event_ticker": st.ticker.rsplit("-", 1)[0],
            "title": st.title,
            "subtitle": st.subtitle,
            "category": st.category,
            "status": status,
            "close_time": datetime.fromtimestamp(st.close_ts, tz=timezone.utc).isoformat(),
            "open_time": datetime.fromtimestamp(st.open_ts, tz=timezone.utc).isoformat(),
            "yes_bid": yb, "yes_ask": ya, "no_bid": nb, "no_ask": na,
            "last_price": int(round(st.true_p * 100)),
            "volume": st.volume,
            "open_interest": st.depth_per_level * 12,
            "liquidity": int(st.depth_per_level * 100 * 2),
            "tick_size": 1,
            "rules_primary": f"Resolves YES if the outcome of '{st.subtitle}' occurs.",
            "settlement_value": st.settlement_value,
        }

    def get_market(self, ticker: str) -> dict:
        return self._market_dict(self.states[ticker])

    def get_candlesticks(self, ticker: str, start_ts: int, end_ts: int,
                         period_interval: int = 60, **_kw) -> list[dict]:
        st = self.states[ticker]
        pts = [(ts, p) for ts, p in st.history if start_ts <= ts <= end_ts]
        out = []
        for ts, p in pts:
            mid = min(99, max(1, int(round(p))))  # history is already in cents
            out.append({
                "ts": ts, "period_interval": period_interval,
                "price_open": mid, "price_close": mid,
                "price_high": min(99, mid + 1), "price_low": max(1, mid - 1),
                "volume": 50, "open_interest": 100,
            })
        return out

    def get_balance(self) -> dict:
        return {"balance": 0}

    def market_obj(self, ticker: str) -> Market:
        return Market.from_api(self.get_market(ticker))

    def book_obj(self, ticker: str) -> OrderBook:
        return OrderBook.from_api(self.get_orderbook(ticker), ticker)

    def true_probabilities(self) -> dict[str, float]:
        return {t: st.true_p for t, st in self.states.items()}

    def settlements_for(self, tickers: list[str]) -> dict[str, int]:
        """Agent hook: settlement values for any held markets that have settled."""
        out: dict[str, int] = {}
        for t in tickers:
            st = self.states.get(t)
            if st and st.settled and st.settlement_value is not None:
                out[t] = st.settlement_value
        return out
