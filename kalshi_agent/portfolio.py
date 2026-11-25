"""Portfolio: cash, positions, fills, P&L.  Persistence to JSON.

Used by both the paper broker (authoritative) and live mode (mirror of the
exchange, rebuilt from /portfolio/balance + /portfolio/positions + fills).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import Fill, Position

log = logging.getLogger("kalshi.portfolio")


@dataclass
class TradeRecord:
    """A closed round-trip, created at settlement or exit."""

    ticker: str
    side: str
    count: int
    entry_price_cents: float
    exit_price_cents: float
    fee_cents: float
    pnl_cents: float
    opened_ts: float
    closed_ts: float
    why: str = ""
    kind: str = "paper"


class Portfolio:
    def __init__(self, cash_cents: int, state_dir: str = "state", name: str = "portfolio"):
        self.cash_cents = float(cash_cents)
        self.starting_cash_cents = float(cash_cents)
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self.closed: list[TradeRecord] = []
        self.equity_history: list[tuple[float, float]] = []   # (ts, equity_cents)
        self._state_dir = state_dir
        self._name = name
        os.makedirs(state_dir, exist_ok=True)
        self.path = os.path.join(state_dir, f"{name}.json")

    # ------------------------------------------------------------- fills ---
    def apply_fill(self, fill: Fill) -> None:
        """Apply a fill, updating cash, position, and realized P&L correctly
        under Kalshi's binary semantics (winning contracts pay 100c)."""
        pos = self.positions.setdefault(fill.ticker, Position(ticker=fill.ticker))
        qty, price = fill.count, fill.price_cents
        gross = qty * price
        if fill.action == "buy":
            if fill.side == "yes":
                tot = pos.avg_yes_price_cents * pos.yes_count + price * qty
                pos.yes_count += qty
                pos.avg_yes_price_cents = tot / pos.yes_count
            else:
                tot = pos.avg_no_price_cents * pos.no_count + price * qty
                pos.no_count += qty
                pos.avg_no_price_cents = tot / pos.no_count
            self.cash_cents -= gross + fill.fee_cents
        else:  # sell
            if fill.side == "yes":
                if qty > pos.yes_count:
                    raise ValueError(f"cannot sell {qty} yes of {pos.yes_count} on {fill.ticker}")
                pos.yes_count -= qty
                realized = qty * (price - pos.avg_yes_price_cents) - fill.fee_cents
            else:
                if qty > pos.no_count:
                    raise ValueError(f"cannot sell {qty} no of {pos.no_count} on {fill.ticker}")
                pos.no_count -= qty
                realized = qty * (price - pos.avg_no_price_cents) - fill.fee_cents
            pos.realized_pnl_cents += realized
            self.cash_cents += gross - fill.fee_cents
            self._record_closed(fill, price, realized)
            if pos.yes_count == 0 and pos.no_count == 0:
                del self.positions[fill.ticker]
        self.fills.append(fill)

    def _record_closed(self, fill: Fill, exit_price: int, realized: float) -> None:
        avg = 0.0
        if fill.side == "yes":
            avg = self.positions.get(fill.ticker, Position(fill.ticker)).avg_yes_price_cents
        else:
            avg = self.positions.get(fill.ticker, Position(fill.ticker)).avg_no_price_cents
        self.closed.append(
            TradeRecord(
                ticker=fill.ticker, side=fill.side, count=fill.count,
                entry_price_cents=avg, exit_price_cents=exit_price,
                fee_cents=fill.fee_cents, pnl_cents=realized,
                opened_ts=0.0, closed_ts=fill.ts or time.time(),
                why=fill.kind, kind=fill.kind,
            )
        )

    # -------------------------------------------------------- settlement ---
    def settle(self, ticker: str, settlement_value: int, why: str = "settle") -> float | None:
        """Settle a held market: YES pays 100 if won, NO pays 100 if NO won (0 if yes)."""
        pos = self.positions.get(ticker)
        if pos is None:
            return None
        pnl = 0.0
        if pos.yes_count:
            payout = pos.yes_count * settlement_value
            basis = pos.yes_count * pos.avg_yes_price_cents
            pnl += payout - basis
            self.cash_cents += payout
            self.closed.append(TradeRecord(
                ticker=ticker, side="yes", count=pos.yes_count,
                entry_price_cents=pos.avg_yes_price_cents, exit_price_cents=settlement_value,
                fee_cents=0.0, pnl_cents=pnl, opened_ts=0.0, closed_ts=time.time(),
                why=why, kind="settlement",
            ))
        if pos.no_count:
            payout = pos.no_count * (100 - settlement_value)
            basis = pos.no_count * pos.avg_no_price_cents
            pnl_n = payout - basis
            pnl += pnl_n
            self.cash_cents += payout
            self.closed.append(TradeRecord(
                ticker=ticker, side="no", count=pos.no_count,
                entry_price_cents=pos.avg_no_price_cents, exit_price_cents=100 - settlement_value,
                fee_cents=0.0, pnl_cents=pnl_n, opened_ts=0.0, closed_ts=time.time(),
                why=why, kind="settlement",
            ))
        del self.positions[ticker]
        return pnl

    # ------------------------------------------------------------- marks ---
    def equity_cents(self, marks: dict[str, int] | None = None) -> float:
        total = self.cash_cents
        for t, pos in self.positions.items():
            mark = (marks or {}).get(t)
            if mark is None:
                # fall back to the held side's own cost basis scale, never a
                # blind 50c when we only hold one side
                if pos.yes_count:
                    mark = pos.avg_yes_price_cents
                elif pos.no_count:
                    mark = 100.0 - pos.avg_no_price_cents
                else:
                    mark = 50
            total += pos.value_cents(int(round(mark)))
        return total

    def open_exposure_cents(self) -> float:
        return sum(p.basis_cents() for p in self.positions.values())

    def snapshot(self, marks: dict[str, int] | None = None) -> dict[str, float]:
        eq = self.equity_cents(marks)
        self.equity_history.append((time.time(), eq))
        return {
            "cash_cents": self.cash_cents,
            "equity_cents": eq,
            "unrealized_cents": eq - self.cash_cents,
            "realized_cents": sum(t.pnl_cents for t in self.closed),
            "return_pct": (eq / self.starting_cash_cents - 1) * 100.0,
            "open_positions": len(self.positions),
        }

    # ------------------------------------------------------------ reports ---
    def win_rate(self) -> tuple[int, int]:
        wins = sum(1 for t in self.closed if t.pnl_cents > 0)
        return wins, len(self.closed)

    def summary_text(self, marks: dict[str, int] | None = None) -> str:
        snap = self.snapshot(marks)
        wins, total = self.win_rate()
        lines = [
            f"cash     {snap['cash_cents'] / 100:>10,.2f} $",
            f"equity   {snap['equity_cents'] / 100:>10,.2f} $   ({snap['return_pct']:+.2f}%)",
            f"open pos {snap['open_positions']:>10d}",
            f"closed   {total:>10d}   win rate {wins}/{total or 1}",
        ]
        for t, pos in sorted(self.positions.items()):
            mark = (marks or {}).get(t, 50)
            val = pos.value_cents(mark)
            lines.append(f"  {t:<28} Y{pos.yes_count:>4}/N{pos.no_count:>4} "
                         f"avgY {pos.avg_yes_price_cents:>5.1f}c mark {mark:>3}c "
                         f"value {val / 100:>7.2f}$")
        return "\n".join(lines)

    # --------------------------------------------------------- persistence ---
    def save(self) -> None:
        data = {
            "cash_cents": self.cash_cents,
            "starting_cash_cents": self.starting_cash_cents,
            "positions": {t: asdict(p) for t, p in self.positions.items()},
            "closed": [asdict(t) for t in self.closed[-500:]],
            "equity_history": self.equity_history[-2000:],
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        os.replace(tmp, self.path)   # atomic: a crash never leaves a torn file

    @classmethod
    def load(cls, path: str, name: str = "portfolio") -> "Portfolio":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        pf = cls(cash_cents=int(data.get("cash_cents", 0)), state_dir=os.path.dirname(path) or "state", name=name)
        pf.starting_cash_cents = float(data.get("starting_cash_cents", pf.starting_cash_cents))
        for t, p in data.get("positions", {}).items():
            pos = Position(**p)
            pos.ticker = t
            pf.positions[t] = pos
        pf.closed = [TradeRecord(**t) for t in data.get("closed", [])]
        pf.equity_history = [tuple(e) for e in data.get("equity_history", [])]
        return pf
