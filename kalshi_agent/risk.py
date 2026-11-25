"""Hard risk controls with persistent state.

The risk engine is the last line of defense *before* an order is sent:
  - per-market cost-basis cap (% of equity)
  - total open exposure cap (% of equity)
  - max concurrent open positions
  - minimum cash reserve
  - daily realized-loss halt (resets at UTC midnight)
  - consecutive-settlement-loss kill switch
  - ticker whitelist/blacklist and per-market cooldowns after losses

Every check returns (allowed, reason).  All limits live in AppConfig; state
is persisted so halts survive restarts.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from .config import AppConfig
from .models import Intent

log = logging.getLogger("kalshi.risk")

STATE_VERSION = 1


class RiskManager:
    def __init__(self, cfg: AppConfig, state_dir: str | None = None):
        self.cfg = cfg
        self.state_dir = state_dir or cfg.state_dir
        os.makedirs(self.state_dir, exist_ok=True)
        self.path = os.path.join(self.state_dir, "risk_state.json")
        self.state: dict[str, Any] = self._load()

    # ------------------------------------------------------------- state ---
    def _load(self) -> dict[str, Any]:
        defaults = {
            "version": STATE_VERSION,
            "halted": False,
            "halt_reason": "",
            "halt_day": "",
            "realized_pnl_by_day": {},
            "consecutive_losses": 0,
            "cooldowns": {},          # ticker -> unix ts until which entrys are blocked
            "recent_days": [],
        }
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if loaded.get("version") == STATE_VERSION:
                defaults.update(loaded)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return defaults

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2, default=str)

    # ------------------------------------------------------------ checks ---
    def pre_trade_check(self, intent: Intent, equity_cents: float, cash_cents: float,
                        positions: dict[str, dict[str, Any]], kind: str = "entry") -> tuple[bool, str]:
        """Gate an order.  `kind` = 'entry' enforces every exposure/reserve cap;
        'exit' (selling to reduce risk) only requires the engine not be halted
        and the ticker allowed — you must always be able to get out."""
        cfg = self.cfg
        self._roll_daily_state()

        if self.state["halted"]:
            return False, f"TRADING HALTED: {self.state['halt_reason']}"

        if intent.ticker in cfg.ticker_blacklist:
            return False, "ticker is blacklisted"
        if cfg.ticker_whitelist and intent.ticker not in cfg.ticker_whitelist:
            return False, "ticker not in whitelist"

        if kind == "exit":
            return True, "ok (exit — risk-reducing)"

        cd_until = self.state["cooldowns"].get(intent.ticker, 0)
        if time.time() < cd_until:
            mins = (cd_until - time.time()) / 60
            return False, f"cooldown active for {mins:.0f} more min"

        if intent.count <= 0:
            return False, "zero-size order"
        cost = intent.count * intent.price_cents

        open_exposure = sum(p.get("basis_cents", 0) for p in positions.values())
        same_market = positions.get(intent.ticker, {}).get("basis_cents", 0)
        # per-market cap applies to the TOTAL basis in that market, including
        # what we already hold — otherwise repeated adds stack past the cap
        if (same_market + cost) > equity_cents * cfg.max_pct_bankroll_per_market / 100.0:
            return False, f"per-market cap {cfg.max_pct_bankroll_per_market}% would be exceeded"
        if (open_exposure + cost) > equity_cents * cfg.max_total_exposure_pct / 100.0:
            return False, f"total exposure cap {cfg.max_total_exposure_pct}% would be exceeded"
        if len(positions) >= cfg.max_open_positions and intent.ticker not in positions:
            return False, f"max open positions ({cfg.max_open_positions}) reached"
        if cash_cents - cost < equity_cents * cfg.min_cash_reserve_pct / 100.0:
            return False, f"cash reserve {cfg.min_cash_reserve_pct}% would be violated"
        return True, "ok"

    def _roll_daily_state(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state.get("halted") and self.state.get("halt_day") and self.state["halt_day"] != today:
            # daily loss halts reset at UTC midnight; kill-switch halts do not
            if self.state.get("halt_reason", "").startswith("DAILY_LOSS"):
                log.info("daily loss halt lifted for %s", today)
                self.state["halted"] = False
                self.state["halt_reason"] = ""
        self.state.setdefault("realized_pnl_by_day", {}).setdefault(today, 0.0)

    # ----------------------------------------------------------- feedback ---
    def record_settlement(self, ticker: str, pnl_cents: float) -> None:
        """Called when a market we held settles.  Updates loss streak + daily P&L."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._roll_daily_state()
        self.state["realized_pnl_by_day"][today] = round(
            self.state["realized_pnl_by_day"].get(today, 0.0) + pnl_cents, 2
        )
        if pnl_cents < 0:
            self.state["consecutive_losses"] = self.state.get("consecutive_losses", 0) + 1
            if self.state["consecutive_losses"] >= self.cfg.consecutive_loss_kill:
                self.halt(f"LOSS_STREAK: {self.state['consecutive_losses']} consecutive losing settlements")
        else:
            self.state["consecutive_losses"] = 0
        self.save()

    def record_entry_loss(self, ticker: str) -> None:
        """Short cooldown on the market after we exit at a loss."""
        cd = time.time() + self.cfg.cooldown_minutes_after_loss * 60
        self.state["cooldowns"][ticker] = cd
        self.save()

    def check_daily_loss(self, equity_cents: float, start_of_day_equity: float) -> bool:
        """Halt if today's realized loss breaches the daily limit."""
        self._roll_daily_state()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pnl = self.state["realized_pnl_by_day"].get(today, 0.0)
        limit = start_of_day_equity * self.cfg.daily_loss_limit_pct / 100.0
        if limit > 0 and pnl <= -limit:
            self.halt(f"DAILY_LOSS: {pnl / 100:.2f}$ <= -{limit / 100:.2f}$ limit")
            return True
        return False

    def halt(self, reason: str) -> None:
        self.state["halted"] = True
        self.state["halt_reason"] = reason
        self.state["halt_day"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.save()
        log.error("RISK HALT: %s", reason)

    def resume(self) -> None:
        self.state["halted"] = False
        self.state["halt_reason"] = ""
        self.state["consecutive_losses"] = 0
        self.save()
        log.warning("risk halt manually resumed")

    def summary(self) -> dict[str, Any]:
        return {
            "halted": self.state["halted"],
            "halt_reason": self.state["halt_reason"],
            "consecutive_losses": self.state["consecutive_losses"],
            "today_realized_pnl_cents": self.state.get("realized_pnl_by_day", {}).get(
                datetime.now(timezone.utc).strftime("%Y-%m-%d"), 0.0
            ),
            "active_cooldowns": sorted(
                t for t, ts in self.state.get("cooldowns", {}).items() if ts > time.time()
            ),
        }
