"""End-to-end agent tests on the simulated exchange.

Checks: deterministic runs, money conservation, settlement correctness vs
sim ground truth, and that the risk engine files state.
"""

import os
import tempfile

from kalshi_agent.agent import Agent
from kalshi_agent.brokers import PaperBroker
from kalshi_agent.config import AppConfig
from kalshi_agent.portfolio import Portfolio
from kalshi_agent.risk import RiskManager
from kalshi_agent.sim import SimExchange


def make_cfg(tmp, **kw) -> AppConfig:
    cfg = AppConfig()
    cfg.state_dir = os.path.join(tmp, "state")
    cfg.reports_dir = os.path.join(tmp, "reports")
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def run_episode(cfg, seed, cycles):
    sim = SimExchange(seed=seed, n_markets=cfg.demo_markets)
    pf = Portfolio(cfg.bankroll_cents, state_dir=cfg.state_dir, name="t")
    risk = RiskManager(cfg, state_dir=cfg.state_dir)
    agent = Agent(cfg, sim, pf, PaperBroker(cfg), risk, start_of_day_equity=float(cfg.bankroll_cents))
    for _ in range(cycles):
        sim.step(cfg.demo_hours_per_cycle)
        if risk.state.get("halted"):
            break
        agent.cycle()
    guard = 0
    while pf.positions and guard < 400:
        sim.step(6.0)
        agent.settle_markets([])
        guard += 1
    return sim, pf, agent


def test_end_to_end_conservation_and_settlements():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = make_cfg(tmp, demo_markets=10, demo_cycles=20, demo_hours_per_cycle=3.0)
        sim, pf, agent = run_episode(cfg, seed=7, cycles=20)
        assert not pf.positions, "wind-down should settle everything"
        # sell fees are already embedded in the realized pnl of closed trades,
        # so only BUY fees are deducted separately from cash
        buy_fees = sum(f.fee_cents for f in pf.fills if f.action == "buy")
        closed = sum(t.pnl_cents for t in pf.closed)
        assert pf.equity_cents({}) == pf.cash_cents
        assert pf.cash_cents == cfg.bankroll_cents + closed - buy_fees
        # settlements must match sim ground truth (NO side pays 100 - settlement_value)
        truth = {s["ticker"]: s["settlement_value"] for s in sim.settlement_log}
        settled_trades = [t for t in pf.closed if t.kind == "settlement"]
        for t in settled_trades:
            expected = truth[t.ticker] if t.side == "yes" else 100 - truth[t.ticker]
            assert expected == t.exit_price_cents


def test_runs_are_deterministic_per_seed():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = make_cfg(tmp, demo_markets=8, demo_cycles=10, demo_hours_per_cycle=4.0)
        _, pf1, _ = run_episode(cfg, seed=99, cycles=10)
        cfg2 = make_cfg(tmp + "-2", demo_markets=8, demo_cycles=10, demo_hours_per_cycle=4.0)
        os.makedirs(cfg2.state_dir, exist_ok=True)
        _, pf2, _ = run_episode(cfg2, seed=99, cycles=10)
        assert pf1.cash_cents == pf2.cash_cents
        assert len(pf1.closed) == len(pf2.closed)


def test_agent_never_overrides_risk_halt():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = make_cfg(tmp, demo_markets=6, demo_cycles=5, consecutive_loss_kill=1)
        sim = SimExchange(seed=1, n_markets=6)
        pf = Portfolio(cfg.bankroll_cents, state_dir=cfg.state_dir, name="t")
        risk = RiskManager(cfg, state_dir=cfg.state_dir)
        risk.halt("TEST: halted on purpose")
        agent = Agent(cfg, sim, pf, PaperBroker(cfg), risk)
        res = agent.cycle()
        # halted risk engine must block every entry
        assert res.entries == 0


def test_risk_file_written():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = make_cfg(tmp, demo_markets=4, demo_cycles=2)
        run_episode(cfg, seed=3, cycles=2)
        assert os.path.exists(os.path.join(cfg.state_dir, "risk_state.json"))
        assert os.path.exists(os.path.join(cfg.state_dir, "t.json"))
