"""Signal generation: edge vs. market, EV after fees, Kelly sizing, exits."""

from __future__ import annotations

import logging
from typing import Any

from .config import AppConfig
from .models import (
    Candle,
    Intent,
    Market,
    OrderBook,
    Prediction,
    Signal,
    kelly_contracts,
    order_fee_cents,
)

log = logging.getLogger("kalshi.strategy")


class Strategy:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    # ------------------------------------------------------------- entry ----
    def evaluate_entry(
        self,
        market: Market,
        book: OrderBook,
        prediction: Prediction,
        equity_cents: float,
        candles: list[Candle] | None = None,
    ) -> Signal:
        """Decide whether to enter a market, and at what size."""
        cfg = self.cfg
        sig = Signal(
            ticker=market.ticker,
            exec_price_cents=0,
            prediction=prediction,
        )

        # --- hard filters -----------------------------------------------------
        if market.status != "open":
            return self._skip(sig, f"status={market.status}")
        hours_left = market.hours_to_close()
        if hours_left > cfg.max_close_hours_ahead:
            return self._skip(sig, f"closes in {hours_left:.0f}h (too far out)")
        if hours_left < cfg.min_hours_to_close:
            return self._skip(sig, f"closing in {hours_left:.2f}h (too soon)")
        if market.liquidity_cents and market.liquidity_cents < cfg.min_liquidity_cents:
            return self._skip(sig, f"liquidity {market.liquidity_cents / 100:.0f}$ < min")
        if market.volume and market.volume < cfg.min_volume:
            return self._skip(sig, f"volume {market.volume} < min")
        if cfg.categories and market.category not in cfg.categories:
            return self._skip(sig, f"category {market.category} not allowlisted")
        if prediction.confidence < cfg.min_confidence:
            return self._skip(sig, f"confidence {prediction.confidence:.2f} < {cfg.min_confidence}")

        # --- pick the better side vs. the book --------------------------------
        p = prediction.p_blend
        best: tuple[str, int, int] | None = None     # (side, ask_cents, depth)
        for side in ("yes", "no"):
            ask = book.best_ask(side)
            if ask is None:
                continue
            p_side = p if side == "yes" else 1.0 - p
            depth = book.depth_at(side, ask.price_cents)
            if best is None:
                best = (side, ask.price_cents, depth)
            else:
                # compare edges in cents after fees
                e_new = (p_side * 100) - ask.price_cents - self._fee_per_contract(ask.price_cents)
                e_cur = ((p if best[0] == "yes" else 1 - p) * 100) - best[1] - self._fee_per_contract(best[1])
                if e_new > e_cur:
                    best = (side, ask.price_cents, depth)
        if best is None:
            return self._skip(sig, "no executable ask on either side")

        side, ask_c, depth = best
        p_side = p if side == "yes" else 1.0 - p
        sig.side = side
        sig.exec_price_cents = ask_c
        sig.depth_contracts = depth

        # --- price band guard --------------------------------------------------
        if not (cfg.min_price_cents <= ask_c <= cfg.max_price_cents):
            return self._skip(sig, f"ask {ask_c}c outside band [{cfg.min_price_cents}, {cfg.max_price_cents}]")

        # --- edge & EV after fees ---------------------------------------------
        fee_per_c = self._fee_per_contract(ask_c)
        sig.fee_cents_per_contract = fee_per_c
        sig.edge_cents = p_side * 100 - ask_c - fee_per_c
        sig.ev_cents = sig.edge_cents  # EV per contract == expected pnl per contract
        if sig.edge_cents < cfg.min_edge_cents:
            return self._skip(sig, f"edge {sig.edge_cents:.1f}c < min {cfg.min_edge_cents}c")

        # --- Kelly sizing, capped by depth, order cap, and portfolio cap -------
        n_kelly, _ = kelly_contracts(equity_cents, p_side, ask_c, cfg.taker_fee_multiplier, cfg.kelly_fraction)
        n = min(n_kelly, depth, cfg.max_contracts_per_order)
        cap_by_market = int((equity_cents * cfg.max_pct_bankroll_per_market / 100.0) / max(ask_c, 1))
        n = min(n, cap_by_market)
        sig.kelly_contracts = n          # final, capped size — what the broker sends
        if n <= 0:
            return self._skip(sig, "sized to 0 contracts (kelly/caps)")
        sig.tradeable = True
        sig.reasons = [
            f"model p({side})={p_side:.3f} vs ask {ask_c}c",
            f"edge={sig.edge_cents:.1f}c fee={fee_per_c:.2f}c depth={depth}",
            f"kelly={n_kelly} -> sized {n}",
        ]
        return sig

    def build_intent(self, sig: Signal, market: Market) -> Intent:
        order_price = self._limit_for_entry(sig)
        return Intent(
            ticker=sig.ticker,
            side=sig.side,
            action="buy",
            count=sig.kelly_contracts if sig.tradeable else 0,
            price_cents=order_price,
            rationale="; ".join(sig.reasons),
            signal=sig,
        )

    def _limit_for_entry(self, sig: Signal) -> int:
        """Place the limit at the executable ask (IOC semantics) — never pay more."""
        return sig.exec_price_cents

    # -------------------------------------------------------------- exits ----
    def evaluate_exit(
        self,
        market: Market,
        book: OrderBook,
        prediction: Prediction,
        position_yes: int,
        position_no: int,
        avg_price_cents: float,
        entry_ts: float | None = None,
    ) -> Intent | None:
        """Return an exit Intent, or None if the position should be held."""
        cfg = self.cfg
        if market.status != "open" or (position_yes == 0 and position_no == 0):
            return None

        held_side = "yes" if position_yes > 0 else "no"
        held_qty = position_yes if held_side == "yes" else position_no
        bid = book.best_bid(held_side)
        mark = bid.price_cents if bid else market.mid_cents
        model_mark = prediction.p_blend * 100 if held_side == "yes" else (1 - prediction.p_blend) * 100
        entry = max(avg_price_cents, 0.01)  # stake per contract; guard div-by-zero

        # 1) stop loss: mark fell to (100 - stop_loss_pct)% of the original bet.
        #    Default 50% -> sell once the position has lost half its cost basis.
        #    An absolute cents backstop fires too when set (tighter of the two wins).
        stop_pct_hit = cfg.stop_loss_pct > 0 and mark <= entry * (1.0 - cfg.stop_loss_pct / 100.0)
        stop_cents_hit = cfg.stop_loss_cents > 0 and mark <= avg_price_cents - cfg.stop_loss_cents
        if stop_pct_hit or stop_cents_hit:
            if stop_pct_hit:
                stop_price = entry * (1.0 - cfg.stop_loss_pct / 100.0)
                rationale = (f"STOP-LOSS: mark {mark:.0f}c <= {stop_price:.1f}c "
                             f"(-{cfg.stop_loss_pct:.0f}% of original bet {entry:.1f}c)")
            else:
                rationale = f"STOP: mark {mark:.0f}c <= entry {avg_price_cents:.0f}c - {cfg.stop_loss_cents}c"
            return Intent(
                ticker=market.ticker, side=held_side, action="sell", count=held_qty,
                price_cents=max(mark, 1), rationale=rationale,
            )
        # 2) take profit: unrealized gain >= take_profit_pct% of the original bet.
        #    Default 80% -> sell once the position is up 80% on its cost basis.
        #    An absolute cents backstop fires too when set.
        tp_pct_hit = cfg.take_profit_pct > 0 and mark >= entry * (1.0 + cfg.take_profit_pct / 100.0)
        tp_cents_hit = cfg.take_profit_cents > 0 and mark >= avg_price_cents + cfg.take_profit_cents
        if tp_pct_hit or tp_cents_hit:
            if tp_pct_hit:
                tp_price = entry * (1.0 + cfg.take_profit_pct / 100.0)
                rationale = (f"TAKE-PROFIT: mark {mark:.0f}c >= {tp_price:.1f}c "
                             f"(+{cfg.take_profit_pct:.0f}% of original bet {entry:.1f}c)")
            else:
                rationale = f"TAKE-PROFIT: mark {mark:.0f}c >= entry+{cfg.take_profit_cents}c"
            return Intent(
                ticker=market.ticker, side=held_side, action="sell", count=held_qty,
                price_cents=max(mark, 1), rationale=rationale,
            )
        # 3) thesis reversal: model now disagrees with our side by > min edge
        edge_other = (1 - prediction.p_blend) * 100 - (100 - mark) if held_side == "yes" else prediction.p_blend * 100 - mark
        if edge_other > cfg.min_edge_cents:
            return Intent(
                ticker=market.ticker, side=held_side, action="sell", count=held_qty,
                price_cents=max(mark, 1),
                rationale=f"REVERSAL: model now favors other side by {edge_other:.1f}c",
            )
        # 4) nearing close with no convergence — collect what's left
        if market.hours_to_close() < cfg.min_hours_to_close * 2 and mark < model_mark:
            return Intent(
                ticker=market.ticker, side=held_side, action="sell", count=held_qty,
                price_cents=max(mark, 1),
                rationale=f"TIME-EXIT: closing soon, mark {mark:.0f}c vs model {model_mark:.0f}c",
            )
        return None

    # ---------------------------------------------------------------- utils ---
    def _fee_per_contract(self, price_cents: int) -> float:
        p = price_cents / 100.0
        return self.cfg.taker_fee_multiplier * p * (1 - p) * 100

    @staticmethod
    def _skip(sig: Signal, reason: str) -> Signal:
        sig.skip_reason = reason
        sig.tradeable = False
        return sig


def summarize_signals(signals: list[Signal]) -> str:
    tradeable = [s for s in signals if s.tradeable]
    lines = [f"{len(tradeable)} tradeable / {len(signals)} evaluated"]
    for s in sorted(tradeable, key=lambda x: -x.edge_cents)[:10]:
        lines.append(f"  {s.ticker:<28} BUY {s.side.upper():<3} @{s.exec_price_cents:>2}c "
                     f"edge {s.edge_cents:+.1f}c size {s.kelly_contracts}")
    return "\n".join(lines)
