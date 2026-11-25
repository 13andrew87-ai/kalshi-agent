"""AI-managed exit overlay for the live agent (opt-in, local real-money only).

The strategy still owns the catastrophic machinery — this overlay only governs
the "how much more profit is left / when to cut a loser" judgment call.

Hard guarantees (not negotiable, enforced here before Claude is ever called):
  1. The cents STOP from config stays absolute. If pnl <= -stop_loss_cents the
     position is sold immediately — regardless of the model, the network, or
     this file's own state.
  2. If Claude is unreachable, out of credits, or answers garbage, the
     strategy's deterministic exits (+5c take-profit / reversal / time-exit)
     run unchanged. Failure degrades to exactly the old behavior.
  3. One advisor call per position per ai_exit_poll_minutes. Between checks,
     the fixed rules govern — an outage between polls can't strand a position
     outside its fixed rules.
  4. Every AI decision is logged with its rationale.

What the AI may change:
  * Strategy says TAKE-PROFIT but the model judges the move has more
    room and momentum -> HOLD (suppress the sale), within the hard floor.
  * Strategy says nothing, but the model judges profits are maxed or the
    edge is fading -> SELL early.
  * The model can also cut a losing trade BEFORE the hard floor
    ("reasonable loss" behavior).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import requests

from .models import Intent

log = logging.getLogger("kalshi.ai_exits")

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_HARD_PREFIXES = ("STOP",)          # rationales produced by the strategy's hard stop
_KEEP_ONLY = ("AI-EXIT",)         # our own rationale prefix; pass through untouched


class AIExitAdvisor:
    def __init__(self, cfg, api_key: str | None = None, session: Any = None,
                 model: str | None = None, min_interval_minutes: float | None = None):
        self.cfg = cfg
        self.key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
        self.session = session   # injectable for tests
        mins = min_interval_minutes
        if mins is None:
            mins = float(getattr(cfg, "ai_exit_poll_minutes", 5) or 5)
        self.min_interval_s = max(30.0, mins * 60.0)
        self._last_call: dict[str, float] = {}

    # ------------------------------------------------------------- public ---
    def decide(self, *, market, book, prediction, intent,
               position_yes: int, position_no: int,
               avg_price_cents: float) -> Intent | None:
        ticker = market.ticker
        held_side = "yes" if position_yes > 0 else "no"
        qty = position_yes if held_side == "yes" else position_no
        bid = book.best_bid(held_side)
        mark = float(bid.price_cents if bid else market.mid_cents)
        entry = max(float(avg_price_cents), 0.01)
        pnl = mark - entry

        # 1) absolute floor — never routed through an LLM
        stop_c = getattr(self.cfg, "stop_loss_cents", 0)
        if stop_c > 0 and mark <= entry - stop_c:
            rationale = f"STOP (ai-exits floor): mark {mark:.0f}c <= entry {entry:.0f}c - {stop_c}c"
            log.warning("%s", rationale)
            return Intent(ticker=ticker, side=held_side, action="sell",
                          count=qty, price_cents=max(int(mark), 1),
                          rationale=rationale)

        # 2) strategy hard stops stay authoritative
        if intent is not None and str(intent.rationale or "").startswith(_HARD_PREFIXES):
            return intent

        # 3) rate limit: between AI checks the fixed rules govern
        now = time.time()
        last = self._last_call.get(ticker, 0.0)
        if now - last < self.min_interval_s:
            return intent

        # 4) no key -> deterministic old behavior, loudly
        if not self.key:
            log.warning("AI exits enabled but ANTHROPIC_API_KEY is unset — fixed exits for %s", ticker)
            self._last_call[ticker] = now
            return intent

        verdict = self._ask(market=market, held_side=held_side, qty=qty,
                            entry=entry, mark=mark, pnl=pnl,
                            prediction=prediction, intent=intent)
        self._last_call[ticker] = now
        if verdict is None:                      # outage / credits / garbage -> fixed rules
            return intent
        action = str(verdict.get("action") or "").lower()
        conf = str(verdict.get("confidence") or "low").lower()
        reason = str(verdict.get("reason") or "")[:180]
        if conf == "low":
            log.info("AI verdict LOW confidence on %s (%s) — deferring to fixed rules", ticker, reason)
            return intent
        if action == "sell":
            log.warning("AI EXIT %s (conf %s, pnl %+.0fc): %s", ticker, conf, pnl, reason)
            return Intent(ticker=ticker, side=held_side, action="sell",
                          count=qty, price_cents=max(int(mark), 1),
                          rationale=f"AI-EXIT ({conf}): {reason}")
        # hold: suppress a discretionary fixed exit, never a hard stop
        if intent is not None:
            log.info("AI HOLD overrides fixed %r on %s (pnl %+.0fc, conf %s): %s",
                     str(intent.rationale).split(":")[0], ticker, pnl, conf, reason)
            return None
        return None

    # -------------------------------------------------------------- inner ---
    def _ask(self, *, market, held_side, qty, entry, mark, pnl,
             prediction, intent) -> dict[str, Any] | None:
        p_yes = getattr(prediction, "p_blend", None)
        if p_yes is None:
            return None
        side_prob = p_yes if held_side == "yes" else (1.0 - p_yes)
        fair_ours = side_prob * 100
        try:
            hours_left = market.hours_to_close()
        except Exception:  # noqa: BLE001
            hours_left = float("nan")
        title = str(getattr(market, "title", "") or "")[:90]
        plan = str(intent.rationale) if intent else "none (no fixed exit currently armed)"
        stop_c = getattr(self.cfg, "stop_loss_cents", 0)
        user_msg = (
            f"Market: {title} ({market.ticker})\n"
            f"Holding: {qty} {held_side.upper()} contracts, avg entry {entry:.1f}c\n"
            f"Best bid now: {mark:.0f}c | unrealized P&L: {pnl:+.1f}c per contract (x{qty})\n"
            f"Model P(YES)={p_yes:.3f} → your side fair value ≈ {fair_ours:.0f}c\n"
            f"Hours until close: {hours_left:.1f}\n"
            f"Standing fixed-exit plan: {plan}\n\n"
            f"A hard stop at -{stop_c}c is enforced by code no matter what you say —\n"
            f"do not count on holding through it.\n\n"
            "Decide: SELL now, or HOLD.\n"
            "Maximize expected profit without riding a winner back into a loser. If the\n"
            "profitable run looks done (momentum stalling, edge gone, side overpriced vs\n"
            "model, or close is near and price won't converge), sell — taking a solid gain\n"
            "beats donating it back. If the edge is gone and the position is losing, cut it\n"
            "before the hard stop. When genuinely torn and the position is in profit, prefer\n"
            "SELL. When torn and flat/losing, prefer SELL if the model edge has vanished.\n\n"
            'Reply ONLY compact JSON: {"action":"sell"|"hold","confidence":"high"|"medium"|"low","reason":"<=140 chars"}'
        )
        system = ("You are a disciplined trading desk exiting one position at a time. "
                  "You never recommend adding risk, only holding or selling. "
                  "No advice, no hedging prose — just the JSON.")
        http = self.session or requests
        try:
            resp = http.post(_ANTHROPIC_URL, timeout=45, headers={
                "x-api-key": self.key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }, json={
                "model": self.model,
                "max_tokens": 200,
                "system": system,
                "messages": [{"role": "user", "content": user_msg}],
            })
        except Exception as exc:  # noqa: BLE001
            log.error("AI exit call failed for %s (%s) — fixed rules apply", market.ticker, exc)
            return None
        if resp.status_code != 200:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001
                detail = (resp.text or "")[:200]
            log.error("AI exit HTTP %s for %s: %s — fixed rules apply",
                      resp.status_code, market.ticker, detail)
            return None
        try:
            text = resp.json()["content"][0]["text"]
            m = re.search(r"\{.*\}", text, re.DOTALL)
            obj = json.loads(m.group(0)) if m else None
        except Exception:  # noqa: BLE001
            obj = None
        if not isinstance(obj, dict) or str(obj.get("action", "")).lower() not in ("sell", "hold"):
            log.error("AI exit returned unparseable verdict for %s — fixed rules apply", market.ticker)
            return None
        return obj
