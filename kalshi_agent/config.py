"""Configuration loading: YAML file + environment variable overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Any

import yaml

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"  # Kalshi demo is .co, not .elections.kalshi.com (NXDOMAIN)


@dataclass
class AppConfig:
    """Every knob the agent uses.  All money values are integer cents."""

    # --- identity / connectivity -------------------------------------------------
    api_key_id: str = ""                      # KALSHI_API_KEY_ID
    private_key_path: str = ""                # KALSHI_PRIVATE_KEY_PATH (PEM)
    base_url: str = DEFAULT_BASE_URL
    use_demo_env: bool = False                # demo-api.kalshi.co (fake money)

    # --- trading mode ------------------------------------------------------------
    mode: str = "paper"                       # paper | live
    live_trading_enabled: bool = False        # must ALSO pass --live + KALSHI_LIVE_CONFIRM=YES
    dry_run: bool = True                      # log orders instead of sending (live only)

    # --- bankroll ----------------------------------------------------------------
    bankroll_cents: int = 100_000             # starting cash for paper mode ($1,000)

    # --- strategy thresholds -----------------------------------------------------
    kelly_fraction: float = 0.10              # fraction of full Kelly to risk
    min_edge_cents: int = 6                   # model prob must beat exec price by this (after fees)
    min_ev_cents: float = 1.0                 # min EV per contract after fees
    max_price_cents: int = 92                 # skip "near-certain" trades (tail risk, poor payoff)
    min_price_cents: int = 8                  # skip cheap longshots
    max_contracts_per_order: int = 250
    order_type: str = "ioc"                   # live broker order time-in-force style

    # --- risk limits (the part that makes it hard to blow up) ---------------------
    max_pct_bankroll_per_market: float = 2.0  # cap on TOTAL cost-basis per market, % of equity
    max_total_exposure_pct: float = 15.0      # cap on total open cost basis, % of equity
    max_open_positions: int = 8
    daily_loss_limit_pct: float = 5.0         # halt for the day after -X% realized
    consecutive_loss_kill: int = 6            # halt after N losing settlements in a row
    min_cash_reserve_pct: float = 20.0        # never spend cash below this % of equity
    # Exit rules — backtest-validated winner across 3 seed sets (120 episodes):
    # 15c stop / +5c take-profit beat every percentage-of-stake variant tried.
    stop_loss_pct: float = 0.0               # %-of-stake stop (0 = disabled — use cents)
    take_profit_pct: float = 0.0             # %-of-stake take-profit (0 = disabled)
    stop_loss_cents: int = 15                # exit if mark falls >= this many cents below entry
    take_profit_cents: int = 5               # exit if mark rises >= this many cents above entry
    max_close_hours_ahead: int = 24 * 21      # only trade markets closing within this window
    min_hours_to_close: float = 1.0           # don't trade markets about to close
    cooldown_minutes_after_loss: int = 60     # per-market cooldown after a losing entry
    ticker_whitelist: list[str] = field(default_factory=list)   # empty = all allowed
    ticker_blacklist: list[str] = field(default_factory=list)

    # --- universe filter ----------------------------------------------------------
    categories: list[str] = field(default_factory=list)         # empty = all categories
    series_watchlist: list[str] = field(default_factory=list)   # Kalshi series tickers;
    min_liquidity_cents: int = 10_000          # $100 of resting depth
    min_volume: int = 100
    scan_limit: int = 200                      # max markets fetched per scan

    # --- predictor weights ---------------------------------------------------------
    w_consensus: float = 0.30                  # order-book mid (noisy, instantaneous)
    w_ema: float = 0.30                        # EMA of candle closes (price anchor)
    w_momentum: float = 0.10                   # recent drift continuation
    w_base_rate: float = 0.30                  # category prior, or ema when absent
    momentum_lookback: int = 12                # candles
    reversion_lookback: int = 24               # EMA half-life, candles
    min_confidence: float = 0.35
    base_rates_path: str = ""                  # optional YAML {category: base_rate}

    # --- fees -----------------------------------------------------------------------
    taker_fee_multiplier: float = 0.07         # Kalshi general formula: 0.07 * C * P * (1-P)
    maker_fee_multiplier: float = 0.0

    # --- engine plumbing -------------------------------------------------------------
    poll_seconds: int = 30                     # sleep between live cycles
    web_sim_poll_seconds: float = 2.0          # dashboard: real seconds per sim cycle
    web_paper_poll_seconds: float = 30.0       # dashboard: seconds between paper cycles
    rate_limit_per_sec: float = 8.0
    request_timeout_s: int = 15
    state_dir: str = "state"
    reports_dir: str = "reports"
    candle_interval_minutes: int = 60          # 1 | 60 | 1440
    candle_lookback_hours: int = 168
    log_level: str = "INFO"

    # --- backtest / demo ----------------------------------------------------------------
    demo_seed: int = 42
    demo_markets: int = 18
    demo_cycles: int = 96
    demo_hours_per_cycle: float = 3.0
    backtest_episodes: int = 40

    # ------------------------------------------------------------------ loading -----
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        valid = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - valid)
        if unknown:
            raise ValueError(f"Unknown config keys: {unknown}")
        cfg = cls()
        for f in fields(cls):
            if f.name in data:
                val = data[f.name]
                ftype = f.type if isinstance(f.type, str) else str(f.type)
                if "list" in ftype:
                    if not isinstance(val, list):
                        raise ValueError(f"config key '{f.name}' must be a list")
                    setattr(cfg, f.name, [str(v) for v in val])
                elif isinstance(getattr(cfg, f.name), bool):
                    setattr(cfg, f.name, bool(val))
                elif isinstance(getattr(cfg, f.name), int):
                    setattr(cfg, f.name, int(val))
                elif isinstance(getattr(cfg, f.name), float):
                    setattr(cfg, f.name, float(val))
                else:
                    setattr(cfg, f.name, val)
        return cfg

    @classmethod
    def load(cls, path: str | None = None) -> "AppConfig":
        data: dict[str, Any] = {}
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        cfg = cls.from_dict(data)
        cfg.apply_env()
        cfg.base_url = DEMO_BASE_URL if cfg.use_demo_env else cfg.base_url
        cfg.validate()
        return cfg

    def apply_env(self) -> None:
        self.api_key_id = os.environ.get("KALSHI_API_KEY_ID", self.api_key_id)
        self.private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", self.private_key_path)
        if os.environ.get("KALSHI_DEMO", "").strip() in {"1", "true", "yes"}:
            self.use_demo_env = True
        if os.environ.get("KALSHI_LIVE_CONFIRM", "").strip() == "YES":
            pass  # consumed by the live gate in cli.py

    def validate(self) -> None:
        if not (0.0 < self.kelly_fraction <= 1.0):
            raise ValueError("kelly_fraction must be in (0, 1]")
        if not (0.0 <= self.stop_loss_pct <= 100.0):
            raise ValueError("stop_loss_pct must be in [0, 100] (% of original stake)")
        if self.take_profit_pct < 0.0:
            raise ValueError("take_profit_pct must be >= 0 (% of original stake)")
        if self.stop_loss_cents < 0 or self.take_profit_cents < 0:
            raise ValueError("stop_loss_cents / take_profit_cents must be >= 0 (0 = disabled)")
        if not (0 < self.min_price_cents < self.max_price_cents < 100):
            raise ValueError("price band must satisfy 0 < min < max < 100")
        if self.mode not in {"paper", "live"}:
            raise ValueError("mode must be 'paper' or 'live'")
        if self.order_type not in {"ioc", "gtc", "fok"}:
            raise ValueError("order_type must be 'ioc', 'gtc' or 'fok'")
        if not (0 <= self.taker_fee_multiplier <= 0.20):
            raise ValueError("taker_fee_multiplier out of sane range")
        if self.bankroll_cents <= 0:
            raise ValueError("bankroll_cents must be positive")

    def has_credentials(self) -> bool:
        return bool(self.api_key_id and self.private_key_path and os.path.exists(self.private_key_path))
