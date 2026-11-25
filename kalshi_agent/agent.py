"""The Agent: the orchestrating loop that ties everything together.

One cycle:
  1. refresh the market universe (filters: status, liquidity, volume, category,
     close-time window, price band)
  2. pull book + candle history per candidate; run the predictor ensemble
  3. run strategy: entry signals ranked by edge; exit signals for held positions
  4. run the risk engine gate on every intent
  5. execute through the broker (paper fills / live IOC orders)
  6. settle matured markets, update risk state, snapshot equity, report

Designed so every step is individually testable and every order path passes
through RiskManager.pre_trade_check exactly once.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .brokers import MarketDataSource, PaperBroker
from .config import AppConfig
from .models import Candle, Intent, Market, OrderBook, Signal, merge_candles
from .portfolio import Portfolio
from .predictor import BaseRateTable, Predictor
from .risk import RiskManager
from .strategy import Strategy

log = logging.getLogger("kalshi.agent")


@dataclass
class CycleResult:
    ts: float = field(default_factory=time.time)
    scanned: int = 0
    evaluated: int = 0
    entries: int = 0
    exits: int = 0
    settled: int = 0
    blocked_by_risk: int = 0
    fills: int = 0
    equity_cents: float = 0.0
    cash_cents: float = 0.0
    top_signals: list[Signal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    signals_digest: list[dict] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        cfg: AppConfig,
        source: MarketDataSource,
        portfolio: Portfolio,
        broker: Any,
        risk: RiskManager | None = None,
        strategy: Strategy | None = None,
        predictor: Predictor | None = None,
        start_of_day_equity: float | None = None,
    ):
        self.cfg = cfg
        self.source = source
        self.portfolio = portfolio
        self.broker = broker
        self.risk = risk or RiskManager(cfg)
        self.strategy = strategy or Strategy(cfg)
        self.predictor = predictor or Predictor(cfg, BaseRateTable.from_yaml(cfg.base_rates_path))
        self.start_of_day_equity = start_of_day_equity or portfolio.starting_cash_cents
        self.last_marks: dict[str, int] = {}

    # ------------------------------------------------------------ helpers ---
    def _candidate_markets(self) -> list[Market]:
        """Collect candidate markets.

        With `series_watchlist` set (the live-mode default), fetch markets per
        Kalshi series — the generic listing is flooded with provisional
        combo/parlay markets and won't reliably surface regular markets.
        Otherwise fall back to the generic open-markets listing.
        """
        watchlist = [s for s in getattr(self.cfg, "series_watchlist", []) if s]
        raw: list[dict] = []
        if watchlist:
            seen: set[str] = set()
            for series in watchlist:
                try:
                    rows = self.source.get_markets(status="open", limit=1000,
                                                   max_pages=2, series_ticker=series)
                except Exception as exc:  # noqa: BLE001
                    log.warning("series fetch failed for %s: %s", series, exc)
                    continue
                for d in rows:
                    t = d.get("ticker")
                    if t and t not in seen:
                        seen.add(t)
                        raw.append(d)
        else:
            raw = self.source.get_markets(status="open", limit=self.cfg.scan_limit)

        out = []
        now = datetime.now(timezone.utc)
        for d in raw:
            m = Market.from_api(d)
            if m.status != "open" or m.is_provisional:
                continue
            # require a real two-sided book: nobody quoting = nothing to trade
            if (m.yes_bid <= 0 and m.no_bid <= 0) or m.yes_ask >= 100:
                continue
            hours_left = m.hours_to_close(now)
            if not (self.cfg.min_hours_to_close <= hours_left <= self.cfg.max_close_hours_ahead):
                continue
            if self.cfg.categories and m.category not in self.cfg.categories:
                continue
            if m.liquidity_cents and m.liquidity_cents < self.cfg.min_liquidity_cents:
                continue
            if m.volume and m.volume < self.cfg.min_volume:
                continue
            if m.ticker in self.cfg.ticker_blacklist:
                continue
            if self.cfg.ticker_whitelist and m.ticker not in self.cfg.ticker_whitelist:
                continue
            mid = m.mid_cents
            if not (self.cfg.min_price_cents - 5 <= mid <= self.cfg.max_price_cents + 5):
                continue
            out.append(m)
        # most liquid first when the universe is bigger than the scan budget
        out.sort(key=lambda m: -(m.volume or 0))
        return out[: self.cfg.scan_limit]

    def _get_book(self, ticker: str) -> OrderBook | None:
        try:
            return OrderBook.from_api(self.source.get_orderbook(ticker), ticker)
        except Exception as exc:  # noqa: BLE001
            log.warning("orderbook fetch failed for %s: %s", ticker, exc)
            return None

    def _get_candles(self, ticker: str) -> list[Candle]:
        try:
            end = int(time.time())
            start = end - self.cfg.candle_lookback_hours * 3600
            rows = self.source.get_candlesticks(
                ticker, start, end, period_interval=self.cfg.candle_interval_minutes
            )
            return merge_candles(rows)
        except Exception as exc:  # noqa: BLE001
            log.debug("candle fetch failed for %s: %s", ticker, exc)
            return []

    # -------------------------------------------------------------- cycle ---
    def cycle(self) -> CycleResult:
        res = CycleResult()
        eq = self.portfolio.equity_cents(self.last_marks)
        self.risk.check_daily_loss(eq, self.start_of_day_equity)

        candidates = self._candidate_markets()
        res.scanned = len(candidates)
        signals: list[Signal] = []

        # mark held positions to the freshest mid so equity/P&L are live
        marks = {m.ticker: m.mid_cents for m in candidates if m.mid_cents}
        for t in list(self.portfolio.positions):
            if marks.get(t):
                self.last_marks[t] = marks[t]
            elif t not in self.last_marks:
                m = self._market_for(t, candidates)
                if m:
                    self.last_marks[t] = m.mid_cents

        for m in candidates:
            book = self._get_book(m.ticker)
            if book is None or (not book.yes_bids and not book.no_bids):
                continue
            candles = self._get_candles(m.ticker)
            pred = self.predictor.predict(m, book, candles)
            sig = self.strategy.evaluate_entry(m, book, pred, eq, candles)
            sig.market = m  # type: ignore[attr-defined]
            sig.book = book  # type: ignore[attr-defined]  (execute against the evaluated book)
            signals.append(sig)
        res.evaluated = len(signals)
        res.signals_digest = [
            {"ticker": s.ticker, "side": s.side, "price": s.exec_price_cents,
             "edge": round(s.edge_cents, 1), "size": s.kelly_contracts,
             "tradeable": s.tradeable, "skip": s.skip_reason}
            for s in signals
        ][:80]

        # --- entries, ranked by edge ----------------------------------------
        tradeable = [s for s in signals if s.tradeable]
        tradeable.sort(key=lambda s: -s.edge_cents)
        for sig in tradeable:
            if sig.kelly_contracts <= 0:
                continue
            intent = self.strategy.build_intent(sig, sig.market)  # type: ignore[attr-defined]
            ok, why = self.risk.pre_trade_check(
                intent, eq, self.portfolio.cash_cents,
                {t: {"basis_cents": p.basis_cents()} for t, p in self.portfolio.positions.items()},
            )
            if not ok:
                res.blocked_by_risk += 1
                log.info("RISK BLOCK %s: %s", intent.ticker, why)
                continue
            book = getattr(sig, "book", None) or self._get_book(intent.ticker)
            try:
                fills = self.broker.execute(intent, book, self.portfolio) if book else []
            except Exception as exc:  # noqa: BLE001
                log.exception("order execution failed for %s: %s", intent.ticker, exc)
                fills = []
            res.fills += len(fills)
            res.entries += 1 if fills else 0

        # --- exits for open positions ---------------------------------------
        for t, pos in list(self.portfolio.positions.items()):
            if pos.yes_count == 0 and pos.no_count == 0:
                continue
            m = self._market_for(t, candidates)
            if m is None:
                continue
            book = self._get_book(t)
            if book is None:
                continue
            candles = self._get_candles(t)
            pred = self.predictor.predict(m, book, candles)
            held_side = "yes" if pos.yes_count > 0 else "no"
            avg_price = pos.avg_yes_price_cents if held_side == "yes" else pos.avg_no_price_cents
            qty_before = pos.yes_count if held_side == "yes" else pos.no_count
            exit_intent = self.strategy.evaluate_exit(
                m, book, pred, pos.yes_count, pos.no_count, avg_price
            )
            if exit_intent is None:
                continue
            ok, why = self.risk.pre_trade_check(
                exit_intent, eq, self.portfolio.cash_cents,
                {t2: {"basis_cents": p2.basis_cents()} for t2, p2 in self.portfolio.positions.items()},
                kind="exit",
            )
            if not ok:
                res.blocked_by_risk += 1
                log.info("RISK BLOCK exit %s: %s", t, why)
                continue
            fills = []
            try:
                fills = self.broker.execute(exit_intent, book, self.portfolio)
            except Exception as exc:  # noqa: BLE001
                log.exception("exit execution failed for %s: %s", exit_intent.ticker, exc)
            if fills:
                res.exits += 1
                res.fills += len(fills)
                avg_exit = sum(f.price_cents * f.count for f in fills) / max(
                    sum(f.count for f in fills), 1)
                if avg_exit < avg_price:
                    self.risk.record_entry_loss(t)

        # --- settlements -----------------------------------------------------
        res.settled += self._apply_source_settlements()
        res.settled += self.settle_markets(candidates)

        snap = self.portfolio.snapshot(self.last_marks)
        res.equity_cents = snap["equity_cents"]
        res.cash_cents = snap["cash_cents"]
        res.top_signals = tradeable[:8]
        self.portfolio.save()
        self.risk.save()
        return res

    def _market_for(self, ticker: str, candidates: list[Market]) -> Market | None:
        for m in candidates:
            if m.ticker == ticker:
                return m
        try:
            return Market.from_api(self.source.get_market(ticker))
        except Exception:  # noqa: BLE001
            return None

    def _apply_source_settlements(self) -> int:
        """If the data source exposes settlements_for() (the sim does), settle
        held markets from it directly — works even when the market no longer
        appears in the open-markets scan."""
        hook = getattr(self.source, "settlements_for", None)
        if hook is None:
            return 0
        sv_map = hook(list(self.portfolio.positions.keys()))
        n = 0
        for t, sv in sv_map.items():
            pnl = self.portfolio.settle(t, int(sv), why="settlement")
            if pnl is not None:
                n += 1
                self.risk.record_settlement(t, pnl)
                log.info("SETTLED %s -> %dc  pnl %+.2f$", t, sv, pnl / 100)
        return n

    # ---------------------------------------------------------- settlement ---
    def settle_markets(self, candidates: list[Market]) -> int:
        """Settle any held market that closed.  Uses source settlement data."""
        settled = 0
        for t, pos in list(self.portfolio.positions.items()):
            m = self._market_for(t, candidates)
            if m is None or m.status not in ("closed", "settled"):
                continue
            sv = m.settlement_value
            if sv is None:
                continue
            pnl = self.portfolio.settle(t, int(sv), why="settlement")
            if pnl is not None:
                settled += 1
                self.risk.record_settlement(t, pnl)
                log.info("SETTLED %s -> %dc  pnl %+.2f$", t, sv, pnl / 100)
        return settled

    # ---------------------------------------------------------------- run ---
    def run(self, cycles: int | None = None, poll_seconds: int | None = None) -> list[CycleResult]:
        results: list[CycleResult] = []
        poll = poll_seconds if poll_seconds is not None else self.cfg.poll_seconds
        i = 0
        try:
            while cycles is None or i < cycles:
                if self.risk.state.get("halted"):
                    log.error("agent halted by risk engine: %s — stopping.",
                              self.risk.state.get("halt_reason"))
                    break
                try:
                    res = self.cycle()
                except Exception:  # noqa: BLE001
                    log.exception("cycle %d failed — continuing", i + 1)
                    i += 1
                    if cycles is None or i < cycles:
                        time.sleep(poll)
                    continue
                results.append(res)
                log.info(
                    "cycle %d: scanned %d, entries %d, exits %d, settled %d, blocked %d, equity %.2f$",
                    i + 1, res.scanned, res.entries, res.exits, res.settled,
                    res.blocked_by_risk, res.equity_cents / 100,
                )
                i += 1
                if cycles is None or i < cycles:
                    time.sleep(poll)
        except KeyboardInterrupt:
            log.warning("interrupted by user — shutting down cleanly (%d cycles run)", i)
        return results
