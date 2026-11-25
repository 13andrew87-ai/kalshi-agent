"""Paper broker tests: fills walk the book, respect limits, charge fees."""

import tempfile

import pytest

from kalshi_agent.brokers import PaperBroker
from kalshi_agent.config import AppConfig
from kalshi_agent.models import BookLevel, Intent, OrderBook, order_fee_cents
from kalshi_agent.portfolio import Portfolio


def make_book(yes_bids, no_bids):
    return OrderBook(ticker="T",
                     yes_bids=[BookLevel(*lv) for lv in yes_bids],
                     no_bids=[BookLevel(*lv) for lv in no_bids])


def buy_yes(count, price, side="yes", action="buy", ticker="T"):
    return Intent(ticker=ticker, side=side, action=action, count=count, price_cents=price)


def test_fill_walks_asks_up_to_limit():
    cfg = AppConfig()
    pf = Portfolio(100_000, state_dir=tempfile.mkdtemp())
    broker = PaperBroker(cfg)
    # no bids 58x50, 55x20 -> yes asks 42x50, 45x20
    book = make_book([(45, 100)], [(58, 50), (55, 20)])
    fills = broker.execute(buy_yes(55, 45), book, pf)
    assert [(f.count, f.price_cents) for f in fills] == [(50, 42), (5, 45)]
    assert fills[0].fee_cents == order_fee_cents(50, 42, cfg.taker_fee_multiplier)
    assert pf.cash_cents == 100_000 - 50 * 42 - fills[0].fee_cents - 5 * 45 - fills[1].fee_cents


def test_ioc_cancels_when_depth_exhausted():
    cfg = AppConfig()
    pf = Portfolio(100_000, state_dir=tempfile.mkdtemp())
    broker = PaperBroker(cfg)
    book = make_book([(45, 100)], [(58, 50)])   # yes ask 42 with depth 50 only
    fills = broker.execute(buy_yes(80, 42), book, pf)
    assert sum(f.count for f in fills) == 50    # only depth at <= limit
    assert pf.positions["T"].yes_count == 50


def test_buy_no_side_uses_no_asks():
    cfg = AppConfig()
    pf = Portfolio(100_000, state_dir=tempfile.mkdtemp())
    broker = PaperBroker(cfg)
    book = make_book([(45, 100)], [(55, 200)])  # no asks = 100-45 = 55 x 100
    fills = broker.execute(buy_yes(10, 55, side="no"), book, pf)
    assert fills and fills[0].price_cents == 55
    assert pf.positions["T"].no_count == 10


def test_sell_walks_bids_down_to_limit():
    cfg = AppConfig()
    pf = Portfolio(100_000, state_dir=tempfile.mkdtemp())
    broker = PaperBroker(cfg)
    book = make_book([(45, 100)], [(58, 50)])
    broker.execute(buy_yes(10, 46), book, pf)   # buy 10 yes at ask 42
    fills = broker.execute(buy_yes(10, 45, action="sell"), book, pf)
    assert fills and fills[0].price_cents == 45  # yes bid 45 x 100
    assert "T" not in pf.positions              # fully exited and cleaned up
    assert pf.closed and pf.closed[-1].pnl_cents == pytest.approx(10 * (45 - 42) - fills[0].fee_cents)


def test_sell_no_side():
    cfg = AppConfig()
    pf = Portfolio(100_000, state_dir=tempfile.mkdtemp())
    broker = PaperBroker(cfg)
    book = make_book([(45, 100)], [(55, 200)])
    broker.execute(buy_yes(10, 55, side="no"), book, pf)
    fills = broker.execute(buy_yes(10, 55, side="no", action="sell"), book, pf)
    assert fills and fills[0].price_cents == 55  # no bid 55
    assert "T" not in pf.positions
