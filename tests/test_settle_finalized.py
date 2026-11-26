"""Phantom-position reconciliation: Kalshi's terminal market statuses must settle.

Regression (2026-11-26): a live position on KXNHL2PTOTAL-26NOV25MTLCOL-5 stayed
"open" in the local mirror for 11+ hours while the exchange reported 0 positions —
the market was `status: "finalized"`, which settle_markets never matched.
"""

import os
import tempfile

from kalshi_agent.agent import Agent
from kalshi_agent.brokers import PaperBroker
from kalshi_agent.config import AppConfig
from kalshi_agent.models import Fill, Market
from kalshi_agent.portfolio import Portfolio
from kalshi_agent.risk import RiskManager


def fill(ticker="T-1", side="yes", action="buy", count=10, price=40, fee=0):
    return Fill(ticker=ticker, side=side, action=action, count=count,
                price_cents=price, fee_cents=fee, ts=0.0, kind="paper")


# ------------------------------------------------------------- status mapping ---
def test_from_api_finalized_is_settled_with_value():
    m = Market.from_api({"ticker": "T-1", "status": "finalized",
                         "settlement_value_dollars": "0.0000", "result": "no"})
    assert m.status == "settled" and m.settlement_value == 0


def test_from_api_determined_is_settled_with_value():
    m = Market.from_api({"ticker": "T-1", "status": "determined",
                         "settlement_value_dollars": "1.0000", "result": "yes"})
    assert m.status == "settled" and m.settlement_value == 100


def test_from_api_finalized_result_only():
    m = Market.from_api({"ticker": "T-1", "status": "finalized", "result": "yes"})
    assert m.status == "settled" and m.settlement_value == 100


def test_from_api_finalized_without_result_stays_valueless():
    m = Market.from_api({"ticker": "T-1", "status": "finalized"})
    assert m.status == "settled" and m.settlement_value is None


def test_from_api_active_still_open():
    assert Market.from_api({"ticker": "T-1", "status": "active"}).status == "open"


# ---------------------------------------------------- phantom position cleared ---
class _FinalizedSource:
    """Live-source stand-in: exposes get_market, NOT settlements_for (sim-only)."""

    def __init__(self, ticker, payload):
        self.ticker, self.payload = ticker, payload

    def get_market(self, ticker):
        assert ticker == self.ticker
        return dict(self.payload)


def test_finalized_phantom_position_is_settled_and_removed(tmp_path):
    cfg = AppConfig()
    cfg.state_dir = str(tmp_path / "state")
    cfg.reports_dir = str(tmp_path / "reports")
    pf = Portfolio(100_000, state_dir=cfg.state_dir, name="t")
    pf.apply_fill(fill(ticker="KXNHL2PTOTAL-26NOV25MTLCOL-5", count=2, price=12, fee=2))
    src = _FinalizedSource("KXNHL2PTOTAL-26NOV25MTLCOL-5",
                           {"ticker": "KXNHL2PTOTAL-26NOV25MTLCOL-5", "status": "finalized",
                            "settlement_value_dollars": "0.0000", "result": "no"})
    broker = PaperBroker(cfg)
    risk = RiskManager(cfg, state_dir=cfg.state_dir)
    agent = Agent(cfg, src, pf, broker, risk, start_of_day_equity=100_000)
    n = agent.settle_markets([])          # empty candidates — must still find it via get_market
    assert n == 1
    assert pf.positions == {}
    assert pf.cash_cents == 100_000 - 2 * 12 - 2   # entire basis lost, $0 payout
