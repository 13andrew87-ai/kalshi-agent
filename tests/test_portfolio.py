"""Portfolio accounting tests — the money math must be exact."""

import tempfile

import pytest

from kalshi_agent.models import Fill
from kalshi_agent.portfolio import Portfolio


def fill(ticker="T-1", side="yes", action="buy", count=10, price=40, fee=0, kind="paper"):
    return Fill(ticker=ticker, side=side, action=action, count=count,
                price_cents=price, fee_cents=fee, ts=0.0, kind=kind)


def test_buy_sell_and_settlement_accounting():
    pf = Portfolio(100_000, state_dir=tempfile.mkdtemp())  # $1000
    pf.apply_fill(fill(count=10, price=40))            # buy 10 yes @40 -> -400
    assert pf.cash_cents == 100_000 - 400
    pf.apply_fill(fill(side="no", count=5, price=55))   # buy 5 no @55 -> -275
    assert pf.cash_cents == 100_000 - 675
    pf.apply_fill(fill(action="sell", count=5, price=50))  # sell 5 yes @50 -> +250
    assert pf.cash_cents == 100_000 - 675 + 250
    pnl = pf.settle("T-1", settlement_value=100)        # yes wins
    # yes: 5*100 - 5*40 = +300 ; no: 0 - 5*55 = -275 -> total +25
    assert pnl == 300 - 275
    assert pf.cash_cents == 100_000 - 675 + 250 + 500
    # conservation: equity == bankroll + closed pnl (fees zero here)
    closed = sum(t.pnl_cents for t in pf.closed)
    assert pf.equity_cents({}) == pytest.approx(100_000 + closed)


def test_fees_deducted_from_cash():
    pf = Portfolio(100_000, state_dir=tempfile.mkdtemp())
    pf.apply_fill(fill(count=10, price=40, fee=18))
    assert pf.cash_cents == 100_000 - 400 - 18


def test_no_shortselling():
    pf = Portfolio(100_000, state_dir=tempfile.mkdtemp())
    with pytest.raises(ValueError):
        pf.apply_fill(fill(action="sell", count=1, price=40))


def test_unrealized_mark_to_market():
    pf = Portfolio(100_000, state_dir=tempfile.mkdtemp())
    pf.apply_fill(fill(count=10, price=40))
    eq = pf.equity_cents({"T-1": 50})
    assert eq == pytest.approx(100_000 - 400 + 500)


def test_settlement_no_wins():
    pf = Portfolio(100_000, state_dir=tempfile.mkdtemp())
    pf.apply_fill(fill(count=10, price=40))
    pnl = pf.settle("T-1", 0)
    assert pnl == -400                     # lost entire basis
    assert pf.cash_cents == 99_600         # payout 0; buy cost was 400
    assert "T-1" not in pf.positions


def test_persistence_roundtrip(tmp_path):
    pf = Portfolio(100_000, state_dir=str(tmp_path), name="rt")
    pf.apply_fill(fill(count=10, price=40, fee=18))
    pf.apply_fill(fill(ticker="T-2", side="no", count=3, price=60))
    pf.settle("T-1", 100)
    pf.save()
    pf2 = Portfolio.load(str(tmp_path / "rt.json"), name="rt")
    assert pf2.cash_cents == pf.cash_cents
    assert "T-2" in pf2.positions and pf2.positions["T-2"].no_count == 3
    assert len(pf2.closed) == len(pf.closed)


def test_summary_text_runs():
    pf = Portfolio(100_000, state_dir=tempfile.mkdtemp())
    pf.apply_fill(fill(count=10, price=40, fee=18))
    txt = pf.summary_text({"T-1": 45})
    assert "equity" in txt and "T-1" in txt
