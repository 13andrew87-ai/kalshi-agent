"""Risk engine tests: every limit must actually block."""

import os

import pytest

from kalshi_agent.config import AppConfig
from kalshi_agent.models import Intent
from kalshi_agent.risk import RiskManager


@pytest.fixture()
def tmp_cfg(tmp_path):
    cfg = AppConfig()
    cfg.state_dir = str(tmp_path)
    return cfg


@pytest.fixture()
def rm(tmp_cfg, tmp_path):
    return RiskManager(tmp_cfg, state_dir=str(tmp_path))


def intent(ticker="T-1", count=10, price=50):
    return Intent(ticker=ticker, side="yes", action="buy", count=count, price_cents=price)


def test_clean_intent_passes(rm, tmp_cfg):
    ok, why = rm.pre_trade_check(intent(), equity_cents=100_000, cash_cents=100_000, positions={})
    assert ok, why


def test_per_market_cap_blocks(rm, tmp_cfg):
    # per-market cap 5% of $1000 = $50 -> 110 @ 50c = $55 must be blocked
    ok, why = rm.pre_trade_check(intent(count=110, price=50), 100_000, 100_000, {})
    assert not ok and "per-market" in why


def test_per_market_cap_counts_existing_basis(rm, tmp_cfg):
    # already hold $40 in T-1 (4%); adding $15 more (30@50c) would take the
    # market to $55 > $50 cap even though each order alone is under the cap
    ok, why = rm.pre_trade_check(intent(count=30, price=50), 100_000, 100_000,
                                 {"T-1": {"basis_cents": 4_000}})
    assert not ok and "per-market" in why


def test_total_exposure_cap_blocks(rm, tmp_cfg):
    # $295 already open in other markets (29.5%); $10 more (20@50c) breaches 30% total cap,
    # while the per-market check (new order alone $10 < $50) passes
    positions = {"A": {"basis_cents": 295_000 / 10}}  # $295 in cents-scale basis... use 29500c
    positions = {"A": {"basis_cents": 29_500}}
    ok, why = rm.pre_trade_check(intent(count=20, price=50), 100_000, 100_000, positions)
    assert not ok and "total exposure" in why


def test_max_positions_blocks(rm, tmp_cfg):
    positions = {f"M{i}": {"basis_cents": 0} for i in range(12)}
    ok, why = rm.pre_trade_check(intent(ticker="NEW"), 100_000, 100_000, positions)
    assert not ok and "positions" in why


def test_existing_position_immune_to_max_positions(rm, tmp_cfg):
    positions = {f"M{i}": {"basis_cents": 0} for i in range(12)}
    positions["T-1"] = {"basis_cents": 1000}
    ok, why = rm.pre_trade_check(intent(), 100_000, 100_000, positions)
    assert ok, why


def test_cash_reserve_blocks(rm, tmp_cfg):
    # equity $1000, cash $150; reserve 20% ($200) -> spending $10 (20@50c) leaves $140 < $200
    ok, why = rm.pre_trade_check(intent(count=20, price=50), 100_000, 15_000, {})
    assert not ok and "reserve" in why


def test_blacklist_and_whitelist(rm, tmp_cfg):
    tmp_cfg.ticker_blacklist = ["BAD-1"]
    ok, why = rm.pre_trade_check(intent(ticker="BAD-1"), 100_000, 100_000, {})
    assert not ok and "blacklist" in why
    tmp_cfg.ticker_blacklist = []
    tmp_cfg.ticker_whitelist = ["ONLY-1"]
    ok, why = rm.pre_trade_check(intent(ticker="OTHER"), 100_000, 100_000, {})
    assert not ok and "whitelist" in why


def test_cooldown_blocks_after_loss(rm, tmp_cfg):
    rm.record_entry_loss("T-1")
    ok, why = rm.pre_trade_check(intent(), 100_000, 100_000, {})
    assert not ok and "cooldown" in why


def test_consecutive_loss_kill_switch(rm, tmp_cfg):
    for i in range(tmp_cfg.consecutive_loss_kill):
        rm.record_settlement(f"MKT{i}", pnl_cents=-500)
    assert rm.state["halted"]
    assert "LOSS_STREAK" in rm.state["halt_reason"]
    ok, why = rm.pre_trade_check(intent(), 100_000, 100_000, {})
    assert not ok and "HALTED" in why
    # a win resets the streak
    rm.resume()
    rm.record_settlement("MKT-X", pnl_cents=300)
    assert rm.state["consecutive_losses"] == 0


def test_daily_loss_limit_halts_and_resets(rm, tmp_cfg):
    rm.record_settlement("MKT-1", pnl_cents=-60_000)  # -$600 on $1000 day equity
    rm.check_daily_loss(40_000, 100_000)
    assert rm.state["halted"]
    assert "DAILY_LOSS" in rm.state["halt_reason"]
    ok, _ = rm.pre_trade_check(intent(), 100_000, 100_000, {})
    assert not ok


def test_halt_state_persists(tmp_cfg):
    rm = RiskManager(tmp_cfg, state_dir=tmp_cfg.state_dir)
    rm.halt("TEST: reason")
    rm2 = RiskManager(tmp_cfg, state_dir=tmp_cfg.state_dir)
    assert rm2.state["halted"] and "TEST" in rm2.state["halt_reason"]
