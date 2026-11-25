"""Ensemble probability predictor.

Components:
  consensus  — order-book mid (the market's instantaneous estimate; noisy)
  ema        — EMA of candle closes: the "fundamental" price anchor built
               from history.  Averaging many prints is far more precise than
               one book snapshot, so disagreements between ema and the book
               are where genuine mispricings (and entries) come from.
  momentum   — consensus nudged by recent drift (short-term continuation)
  base_rate  — optional category prior blended into the ema anchor, weighted
               by time-to-close (prior fades out as settlement nears)

The blend is clamped to [0.01, 0.99]; confidence discounts for wide spreads,
component disagreement, and missing history.  The predictor is deliberately
simple and auditable: it is a framework for YOUR edge, not a magic oracle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from .models import Candle, Market, OrderBook, Prediction
from .config import AppConfig

log = logging.getLogger("kalshi.predictor")


class BaseRateTable:
    def __init__(self, rates: dict[str, float] | None = None):
        self.rates = rates or {}

    @classmethod
    def from_yaml(cls, path: str | None) -> "BaseRateTable":
        if not path:
            return cls()
        import yaml

        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            return cls({str(k): float(v) for k, v in data.items()})
        except FileNotFoundError:
            log.warning("base_rates file %s not found; using no priors", path)
            return cls()


class Predictor:
    def __init__(self, cfg: AppConfig, base_rates: BaseRateTable | None = None):
        self.cfg = cfg
        self.base_rates = base_rates or BaseRateTable()

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _clamp(p: float) -> float:
        return min(0.99, max(0.01, p))

    def _consensus(self, market: Market, book: OrderBook | None) -> tuple[float, list[str]]:
        notes: list[str] = []
        if book is not None:
            mid = book.mid_cents()
            if mid is not None:
                notes.append(f"book mid {mid:.1f}c")
                return self._clamp(mid / 100.0), notes
        mid = market.mid_cents
        notes.append(f"market mid {mid}c (no book)")
        return self._clamp(mid / 100.0), notes

    def _trend(self, closes: list[int]) -> float:
        """Signed recent change in price points (cents)."""
        if len(closes) < 2:
            return 0.0
        look = self.cfg.momentum_lookback
        window = closes[-look:] if len(closes) >= look else closes
        return window[-1] - window[0]

    def _ema(self, closes: list[int]) -> float | None:
        """EMA over candle closes with half-life = reversion_lookback candles."""
        if len(closes) < 6:
            return None
        half_life = max(self.cfg.reversion_lookback, 2)
        alpha = 1.0 - 0.5 ** (1.0 / half_life)
        ema = float(closes[0])
        for c in closes[1:]:
            ema += alpha * (c - ema)
        return ema

    # -------------------------------------------------------------- predict
    def predict(self, market: Market, book: OrderBook | None,
                candles: Iterable[Candle] | None = None,
                now: datetime | None = None) -> Prediction:
        cfg = self.cfg
        now = now or datetime.now(timezone.utc)
        notes: list[str] = []
        closes = [c.close_c for c in candles] if candles else []
        closes = [c for c in closes if 0 < c < 100]

        p_consensus, c_notes = self._consensus(market, book)
        notes.extend(c_notes)
        components: dict[str, float] = {"consensus": p_consensus}

        ema = self._ema(closes)
        p_ema = self._clamp(ema / 100.0) if ema is not None else p_consensus
        components["ema"] = p_ema
        if ema is not None:
            notes.append(f"ema anchor {ema:.1f}c")

        trend = self._trend(closes) if closes else 0.0
        nudge = max(-8.0, min(8.0, float(trend)))
        p_momentum = self._clamp(p_consensus + nudge / 100.0 * 0.5)
        components["momentum"] = p_momentum
        if closes:
            notes.append(f"trend {trend:+d}c over lookback")

        prior = self.base_rates.rates.get(market.category)
        if prior is not None:
            hours_left = market.hours_to_close(now)
            # far from close a static prior carries weight; near close the
            # market's own price is the best estimate and the prior fades out
            w_prior = 0.5 * hours_left / (hours_left + 24.0)
            p_base = self._clamp(p_ema * (1 - w_prior) + prior * w_prior)
            notes.append(f"{market.category} prior {prior:.2f} @w={w_prior:.2f}")
        else:
            p_base = p_ema
        components["base_rate"] = p_base

        weights = {
            "consensus": cfg.w_consensus,
            "ema": cfg.w_ema,
            "momentum": cfg.w_momentum,
            "base_rate": cfg.w_base_rate,
        }
        total_w = sum(weights.values())
        p_blend = sum(components[k] * weights[k] for k in weights) / total_w
        p_blend = self._clamp(p_blend)

        # confidence: 1 = high.  penalize wide spread + disagreement + no history
        spread_penalty = 0.0
        if book is not None:
            sp = book.spread_cents()
            if sp is not None:
                spread_penalty = min(0.4, max(0.0, (sp - 1) * 0.10))
        vals = list(components.values())
        disagreement = (max(vals) - min(vals)) if vals else 1.0
        disagree_penalty = min(0.35, disagreement * 0.5)
        history_penalty = 0.15 if not closes else 0.0
        confidence = max(0.0, 1.0 - spread_penalty - disagree_penalty - history_penalty)
        if closes:
            notes.append(f"confidence {confidence:.2f}")

        return Prediction(
            ticker=market.ticker,
            p_blend=p_blend,
            confidence=confidence,
            components=components,
            notes=notes,
        )
