"""Tests for the strategy engine: Kelly sizing, fees, entry/exit logic."""

import math

import pytest

from kalshi_agent.config import AppConfig
from kalshi_agent.models import BookLevel, Candle, Market, OrderBook, kelly_contracts, order_fee_cents
from kalshi_agent.strategy import Strategy


def make_cfg(**kw) -> AppConfig:
    cfg = AppConfig()
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def make_book(yes_bid=45, yes_bid_qty=100, no_bid=55, no_bid_qty=200):
    return OrderBook(
        ticker="T-1",
        yes_bids=[BookLevel(yes_bid, yes_bid_qty)],
        no_bids=[BookLevel(no_bid, no_bid_qty)],
    )


def make_market(**kw) -> Market:
    from datetime import datetime, timedelta, timezone

    d = dict(
        ticker="T-1", status="open", yes_bid=45, yes_ask=46, no_bid=54, no_ask=45,
        last_price=45, volume=5000, liquidity_cents=500_000,
        close_time=datetime.now(timezone.utc) + timedelta(days=3),
    )
    d.update(kw)
    m = Market(ticker=d["ticker"])
    for k, v in d.items():
        setattr(m, k, v)
    return m


# ------------------------------------------------------------------ fees ---
def test_fee_formula_matches_kalshi_style():
    # fee = ceil(mult * C * P * (1-P)) in cents
    assert order_fee_cents(10, 50, 0.07) == math.ceil(0.07 * 10 * 0.25 * 100)
    assert order_fee_cents(0, 50, 0.07) == 0
    assert order_fee_cents(100, 1, 0.07) == math.ceil(0.07 * 100 * 0.0099 * 100)
    assert order_fee_cents(5, 100, 0.07) == 0  # degenerate price


# ----------------------------------------------------------------- kelly ---
def test_kelly_positive_edge_sizes_correctly():
    # n* = W * (p - c) / (c * (1-c))
    bank = 100_000.0
    p, price = 0.60, 50
    fee_c = 0.07 * 0.5 * 0.5
    c = 0.5 + fee_c
    expected = math.floor(bank / 100 * (p - c) / (c * (1 - c)))
    n, fee = kelly_contracts(bank, p, price, 0.07, 1.0)
    assert n == expected
    assert fee == pytest.approx(fee_c * 100)


def test_kelly_zero_when_no_edge():
    n, _ = kelly_contracts(100_000.0, 0.50, 55, 0.07, 1.0)
    assert n == 0


def test_kelly_fraction_scales_down():
    full, _ = kelly_contracts(100_000.0, 0.62, 50, 0.07, 1.0)
    frac, _ = kelly_contracts(100_000.0, 0.62, 50, 0.07, 0.25)
    assert frac == math.floor(full * 0.25)


# ---------------------------------------------------------------- entry ---
def test_entry_no_edge_skips():
    cfg = make_cfg(min_edge_cents=2)
    strat = Strategy(cfg)
    book = make_book(no_bid=55)              # yes ask = 45c
    pred = type("P", (), {"p_blend": 0.46, "confidence": 0.9, "components": {}})()
    from kalshi_agent.models import Prediction

    pred = Prediction(ticker="T-1", p_blend=0.46, confidence=0.9)
    sig = strat.evaluate_entry(make_market(), book, pred, 100_000.0, [])
    assert not sig.tradeable
    assert "edge" in sig.skip_reason


def test_entry_positive_edge_trades():
    cfg = make_cfg(min_edge_cents=2, min_confidence=0.3)
    strat = Strategy(cfg)
    # model says 60c, yes ask is 45c -> big edge
    book = make_book(no_bid=55, no_bid_qty=500)
    from kalshi_agent.models import Prediction

    pred = Prediction(ticker="T-1", p_blend=0.60, confidence=0.9)
    m = make_market()
    sig = strat.evaluate_entry(m, book, pred, 100_000.0, [])
    assert sig.tradeable
    assert sig.side == "yes"
    assert sig.exec_price_cents == 45
    assert sig.kelly_contracts > 0
    assert sig.kelly_contracts <= 500  # capped by depth
    # per-market cap: 5% of $1000 = $50 at 45c -> 111 contracts
    assert sig.kelly_contracts <= 111


def test_entry_prefers_better_side():
    cfg = make_cfg(min_edge_cents=1, min_confidence=0.3)
    strat = Strategy(cfg)
    # market prices YES ~90c, model says p(yes)=0.10 -> buying NO at 10c has huge edge
    book = make_book(yes_bid=90, yes_bid_qty=100, no_bid=10, no_bid_qty=300)
    from kalshi_agent.models import Prediction

    pred = Prediction(ticker="T-1", p_blend=0.10, confidence=0.9)
    sig = strat.evaluate_entry(make_market(yes_bid=90, yes_ask=91), book, pred, 100_000.0, [])
    assert sig.tradeable and sig.side == "no" and sig.exec_price_cents == 10


def test_entry_price_band_skip():
    cfg = make_cfg(min_price_cents=8, max_price_cents=92, min_edge_cents=1, min_confidence=0.3)
    strat = Strategy(cfg)
    book = make_book(yes_bid=2, no_bid=95)
    from kalshi_agent.models import Prediction

    pred = Prediction(ticker="T-1", p_blend=0.10, confidence=0.9)
    sig = strat.evaluate_entry(make_market(), book, pred, 100_000.0, [])
    assert not sig.tradeable
    assert "band" in sig.skip_reason


def test_entry_liquidity_filter():
    cfg = make_cfg(min_liquidity_cents=10_000)
    strat = Strategy(cfg)
    from kalshi_agent.models import Prediction

    pred = Prediction(ticker="T-1", p_blend=0.7, confidence=0.9)
    sig = strat.evaluate_entry(make_market(liquidity_cents=100), make_book(), pred, 100_000.0, [])
    assert not sig.tradeable and "liquidity" in sig.skip_reason


def test_entry_close_time_window():
    from datetime import datetime, timedelta, timezone

    cfg = make_cfg(min_hours_to_close=1.0, min_confidence=0.3)
    strat = Strategy(cfg)
    from kalshi_agent.models import Prediction

    pred = Prediction(ticker="T-1", p_blend=0.7, confidence=0.9)
    m = make_market(close_time=datetime.now(timezone.utc) + timedelta(minutes=30))
    sig = strat.evaluate_entry(m, make_book(), pred, 100_000.0, [])
    assert not sig.tradeable and "soon" in sig.skip_reason


# ---------------------------------------------------------------- exits ---
def test_exit_stop_loss():
    from kalshi_agent.models import Prediction

    strat = Strategy(make_cfg(stop_loss_cents=15))
    book = make_book(yes_bid=20, no_bid=80)   # yes bid = 20c (bought at 40c)
    pred = Prediction(ticker="T-1", p_blend=0.5, confidence=0.9)
    intent = strat.evaluate_exit(make_market(), book, pred, position_yes=10, position_no=0,
                                 avg_price_cents=40)
    assert intent is not None and intent.action == "sell" and "STOP" in intent.rationale
    assert intent.count == 10


def test_exit_take_profit():
    from kalshi_agent.models import Prediction

    strat = Strategy(make_cfg(take_profit_cents=10))
    book = make_book(yes_bid=90, no_bid=10)   # yes bid = 90c (bought at 60c)
    pred = Prediction(ticker="T-1", p_blend=0.6, confidence=0.9)
    intent = strat.evaluate_exit(make_market(), book, pred, position_yes=10, position_no=0,
                                 avg_price_cents=60)
    assert intent is not None and "TAKE-PROFIT" in intent.rationale


def test_exit_hold_when_still_agreeing():
    from kalshi_agent.models import Prediction

    strat = Strategy(make_cfg(stop_loss_cents=15, take_profit_cents=10))
    book = make_book(no_bid=57)             # yes bid 43c, bought at 45c, model 50%
    pred = Prediction(ticker="T-1", p_blend=0.50, confidence=0.9)
    intent = strat.evaluate_exit(make_market(), book, pred, position_yes=10, position_no=0,
                                 avg_price_cents=45)
    assert intent is None


# ------------------------------------------- percentage exits (new) ---
def test_exit_stop_loss_pct_50():
    from kalshi_agent.models import Prediction

    strat = Strategy(make_cfg(stop_loss_pct=50.0, take_profit_pct=80.0, stop_loss_cents=0, take_profit_cents=0))
    book = make_book(yes_bid=20, no_bid=80)   # bought at 40c -> -50% of stake
    pred = Prediction(ticker="T-1", p_blend=0.5, confidence=0.9)
    intent = strat.evaluate_exit(make_market(), book, pred, position_yes=10,
                                 position_no=0, avg_price_cents=40)
    assert intent is not None and intent.action == "sell"
    assert "STOP-LOSS" in intent.rationale and "50%" in intent.rationale
    assert intent.count == 10


def test_exit_take_profit_pct_80():
    from kalshi_agent.models import Prediction

    strat = Strategy(make_cfg(stop_loss_pct=50.0, take_profit_pct=80.0, stop_loss_cents=0, take_profit_cents=0))
    book = make_book(yes_bid=45, no_bid=55)   # bought at 25c -> +80% gain (45c)
    pred = Prediction(ticker="T-1", p_blend=0.5, confidence=0.9)
    intent = strat.evaluate_exit(make_market(), book, pred, position_yes=10,
                                 position_no=0, avg_price_cents=25)
    assert intent is not None and "TAKE-PROFIT" in intent.rationale
    assert "80%" in intent.rationale


def test_exit_holds_below_pct_thresholds():
    from kalshi_agent.models import Prediction

    strat = Strategy(make_cfg(stop_loss_pct=50.0, take_profit_pct=80.0, stop_loss_cents=0, take_profit_cents=0))
    book = make_book(yes_bid=40, no_bid=60)   # bought at 25c -> +60% (< 80%)
    pred = Prediction(ticker="T-1", p_blend=0.5, confidence=0.9)
    intent = strat.evaluate_exit(make_market(), book, pred, position_yes=10,
                                 position_no=0, avg_price_cents=25)
    assert intent is None


def test_exit_pct_applies_to_no_side():
    from kalshi_agent.models import Prediction

    strat = Strategy(make_cfg(stop_loss_pct=50.0, take_profit_pct=80.0, stop_loss_cents=0, take_profit_cents=0))
    book = make_book(no_bid=22, yes_bid=78)   # bought NO at 40c -> -45% (< 50%): hold
    pred = Prediction(ticker="T-1", p_blend=0.15, confidence=0.9)  # model backs NO
    assert strat.evaluate_exit(make_market(), book, pred, 0, 10, 40) is None
    book2 = make_book(no_bid=20, yes_bid=80)  # now exactly -50%: sell
    intent = strat.evaluate_exit(make_market(), book2, pred, 0, 10, 40)
    assert intent is not None and intent.side == "no" and "STOP-LOSS" in intent.rationale


def test_exit_defaults_are_backtest_winner():
    """Defaults must stay pinned to the validated 15c stop / +5c take-profit."""
    from kalshi_agent.models import Prediction

    cfg = AppConfig()
    assert cfg.stop_loss_pct == 0.0 and cfg.take_profit_pct == 0.0
    assert (cfg.stop_loss_cents, cfg.take_profit_cents) == (15, 5)
    strat = Strategy(cfg)
    pred = Prediction(ticker="T-1", p_blend=0.5, confidence=0.9)
    # winner take-profit: entry 25c, bid 30c -> +5c -> sell
    book = make_book(yes_bid=30, no_bid=70)
    intent = strat.evaluate_exit(make_market(), book, pred, 10, 0, 25)
    assert intent is not None and "TAKE-PROFIT" in intent.rationale
    # winner stop: entry 40c, bid 24c -> -16c -> sell
    book = make_book(yes_bid=24, no_bid=76)
    intent = strat.evaluate_exit(make_market(), book, pred, 10, 0, 40)
    assert intent is not None and "STOP" in intent.rationale
    # hold inside the band: entry 40c, bid 35c -> no exit
    book = make_book(yes_bid=35, no_bid=65)
    assert strat.evaluate_exit(make_market(), book, pred, 10, 0, 40) is None
