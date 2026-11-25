"""Kalshi prediction-market trading agent.

A modular, safety-first trading agent for Kalshi (CFTC-regulated event
contracts exchange).  Includes:

- signed REST client for the Kalshi Trade API v2 (RSA-PSS auth)
- a deterministic simulated exchange for offline development / demos
- an ensemble probability predictor (consensus, momentum, mean-reversion,
  optional category base rates)
- edge + EV-after-fees evaluation and fractional-Kelly position sizing
- a hard risk engine (exposure caps, daily-loss halt, loss-streak kill switch)
- paper broker (fills against the real or simulated book) and live broker
- backtest harness, HTML/Markdown reporting, and a CLI

Nothing here is financial advice.  Live trading requires explicit opt-in.
"""

__version__ = "1.0.0"
