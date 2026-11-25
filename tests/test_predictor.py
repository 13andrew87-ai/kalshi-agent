"""Predictor tests: blend sanity, confidence penalties, base rates."""

import math

from kalshi_agent.config import AppConfig
from kalshi_agent.models import BookLevel, Candle, Market, OrderBook
from kalshi_agent.predictor import BaseRateTable, Predictor
from datetime import datetime, timedelta, timezone


def make_book(yes_bid=45, no_bid=55):
    return OrderBook(ticker="T", yes_bids=[BookLevel(yes_bid, 100)],
                     no_bids=[BookLevel(no_bid, 100)])


def make_market(**kw):
    d = dict(ticker="T", status="open", yes_bid=45, yes_ask=46, volume=5000,
             liquidity_cents=500_000, category="Sports")
    d.update(kw)
    m = Market(ticker=d["ticker"])
    for k, v in d.items():
        setattr(m, k, v)
    return m


def candles(closes):
    return [Candle(ts=i, open_c=c, close_c=c, high_c=c, low_c=c) for i, c in enumerate(closes)]


def test_prediction_bounded_and_has_components():
    p = Predictor(AppConfig()).predict(make_market(), make_book(), [])
    assert 0.01 <= p.p_blend <= 0.99
    assert set(p.components) == {"consensus", "ema", "momentum", "base_rate"}
    assert 0.0 <= p.confidence <= 1.0


def test_consensus_tracks_book_mid():
    book = make_book(yes_bid=40, no_bid=58)      # mid = (40 + 42)/2 = 41c
    p = Predictor(AppConfig()).predict(make_market(), book, [])
    assert p.components["consensus"] == 0.41


def test_trend_nudges_momentum_component():
    cfg = AppConfig()
    up = Predictor(cfg).predict(make_market(), make_book(), candles([40] * 10 + [50] * 2))
    flat = Predictor(cfg).predict(make_market(), make_book(), candles([45] * 12))
    assert up.components["momentum"] > flat.components["momentum"]


def test_wide_spread_lowers_confidence():
    cfg = AppConfig()
    tight = Predictor(cfg).predict(make_market(), make_book(45, 55), [])
    wide = Predictor(cfg).predict(make_market(), make_book(30, 40), [])  # yes ask = 60 -> spread 30
    assert wide.confidence < tight.confidence


def test_missing_history_lowers_confidence():
    cfg = AppConfig()
    with_hist = Predictor(cfg).predict(make_market(), make_book(), candles([45] * 20))
    no_hist = Predictor(cfg).predict(make_market(), make_book(), [])
    assert no_hist.confidence < with_hist.confidence


def test_base_rate_pulls_far_from_close_only():
    cfg = AppConfig()
    now = datetime.now(timezone.utc)
    far = make_market(close_time=now + timedelta(days=30))
    near = make_market(close_time=now + timedelta(minutes=30))
    br = BaseRateTable({"Sports": 0.80})
    pred_far = Predictor(cfg, br).predict(far, make_book(), [])
    pred_near = Predictor(cfg, br).predict(near, make_book(), [])
    mid = pred_far.components["consensus"]
    assert abs(pred_far.components["base_rate"] - mid) > abs(pred_near.components["base_rate"] - mid)
    assert pred_far.components["base_rate"] > mid  # pulled toward 0.80 prior


def test_without_base_rate_table_base_equals_consensus():
    p = Predictor(AppConfig(), BaseRateTable()).predict(make_market(), make_book(), [])
    assert p.components["base_rate"] == p.components["consensus"]


# --------------------------------- candle parsing regression (was silently broken) ---
def test_merge_candles_parses_live_and_sim_shapes():
    from kalshi_agent.models import merge_candles
    rows = [
        {"ts": 1000, "price_open": "0.4100", "price_close": "0.4300",
         "price_high": "0.4400", "price_low": "0.4000", "volume_fp": "123.00"},
        {"ts": 2000, "price_open": 51, "price_close": 52,
         "price_high": 53, "price_low": 50, "volume": 80},
    ]
    candles = merge_candles(rows)
    assert len(candles) == 2
    assert candles[0].close_c == 43 and candles[0].volume == 123
    assert candles[1].close_c == 52 and candles[1].volume == 80


def test_merge_candles_sub_dollar_volume_not_rescaled():
    from kalshi_agent.models import merge_candles
    rows = [{"ts": 1000, "price_open": 50, "price_close": 51,
             "price_high": 52, "price_low": 49, "volume": "0.75"}]
    assert merge_candles(rows)[0].volume == 0        # int(), not 75


def test_predict_with_real_candles_uses_ema(tmp_path=None):
    from datetime import datetime, timedelta, timezone
    from kalshi_agent.config import AppConfig
    from kalshi_agent.models import Candle, Market, merge_candles
    from kalshi_agent.predictor import Predictor
    cfg = AppConfig()
    closes = [50, 52, 48, 55, 47, 56, 46, 58, 44, 60, 43, 62] * 2
    candles = [Candle(ts=i, open_c=c, close_c=c, high_c=c, low_c=c) for i, c in enumerate(closes)]
    m = Market(ticker="T-1", status="open", yes_bid=50, yes_ask=52, no_bid=48, no_ask=50,
               last_price=51, volume=1000, liquidity_cents=100000,
               close_time=datetime.now(timezone.utc) + timedelta(days=3))
    pred = Predictor(cfg).predict(m, None, candles)
    assert pred.components["ema"] != pred.components["consensus"]   # EMA actually used
    assert pred.components["momentum"] != pred.components["consensus"]
