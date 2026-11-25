"""Monte-Carlo backtester.

Runs N independent simulated exchange episodes (different seeds = different
market universes) through the exact same agent code path used live —
predictor, strategy, risk engine, paper broker — and aggregates results.

Honest framing: this validates that the *machinery* (sizing, fees, risk
limits, settlement accounting) behaves and is net-positive on an artificial
inefficient market.  It does NOT prove real-world edge; that depends on the
quality of your predictions against real Kalshi markets.
"""

from __future__ import annotations

import logging
import statistics
import tempfile
import time
from dataclasses import dataclass, field

from .agent import Agent
from .brokers import PaperBroker
from .config import AppConfig
from .portfolio import Portfolio
from .risk import RiskManager
from .sim import SimExchange

log = logging.getLogger("kalshi.backtest")


@dataclass
class EpisodeResult:
    seed: int
    return_pct: float
    trades: int
    wins: int
    max_drawdown_pct: float
    fees_cents: float = 0.0
    halted: bool = False
    equity_curve: list[float] = field(default_factory=list)


def run_episode(seed: int, cfg: AppConfig, cycles: int, hours_per_cycle: float) -> EpisodeResult:
    sim = SimExchange(seed=seed, n_markets=cfg.demo_markets)
    with tempfile.TemporaryDirectory(prefix="ka-bt-") as tmp:
        pf = Portfolio(cfg.bankroll_cents, state_dir=tmp, name=f"bt{seed}")
        risk = RiskManager(cfg, state_dir=tmp)
        broker = PaperBroker(cfg)
        agent = Agent(cfg, sim, pf, broker, risk, start_of_day_equity=float(cfg.bankroll_cents))
        fees = 0.0
        for _ in range(cycles):
            sim.step(hours_per_cycle)
            if risk.state.get("halted"):
                break
            agent.cycle()
        # wind down: let everything settle
        guard = 0
        while pf.positions and guard < 400:
            sim.step(6.0)
            agent.settle_markets([])
            guard += 1
        fees = sum(f.fee_cents for f in pf.fills)
        eq = pf.equity_cents({})
        curve = [c / 100 for _, c in pf.equity_history] or [cfg.bankroll_cents / 100]
        peak, mdd = curve[0], 0.0
        for v in curve:
            peak = max(peak, v)
            mdd = max(mdd, (peak - v) / max(peak, 1))
        wins = sum(1 for t in pf.closed if t.pnl_cents > 0)
        return EpisodeResult(
            seed=seed,
            return_pct=(eq / cfg.bankroll_cents - 1) * 100,
            trades=len(pf.closed),
            wins=wins,
            max_drawdown_pct=mdd * 100,
            fees_cents=fees,
            halted=bool(risk.state.get("halted")),
            equity_curve=curve,
        )


def run_backtest(cfg: AppConfig, episodes: int | None = None, cycles: int | None = None,
                 hours_per_cycle: float | None = None, base_seed: int | None = None) -> dict:
    n_ep = episodes or cfg.backtest_episodes
    n_cy = cycles or cfg.demo_cycles
    hpc = hours_per_cycle or cfg.demo_hours_per_cycle
    seed0 = base_seed if base_seed is not None else cfg.demo_seed
    t0 = time.time()
    results = [run_episode(seed0 + i, cfg, n_cy, hpc) for i in range(n_ep)]
    rets = [r.return_pct for r in results]
    positive = sum(1 for r in rets if r > 0)
    agg = {
        "episodes": n_ep,
        "cycles_per_episode": n_cy,
        "hours_per_cycle": hpc,
        "mean_return_pct": round(statistics.mean(rets), 3),
        "median_return_pct": round(statistics.median(rets), 3),
        "stdev_return_pct": round(statistics.stdev(rets), 3) if n_ep > 1 else 0.0,
        "min_return_pct": round(min(rets), 3),
        "max_return_pct": round(max(rets), 3),
        "positive_episodes": positive,
        "positive_rate": round(positive / n_ep, 3),
        "t_stat": round(statistics.mean(rets) / (statistics.stdev(rets) / n_ep ** 0.5), 2)
        if n_ep > 1 and statistics.stdev(rets) > 0 else 0.0,
        "mean_trades": round(statistics.mean([r.trades for r in results]), 1),
        "mean_win_rate": round(
            statistics.mean([r.wins / r.trades for r in results if r.trades] or [0.0]), 3),
        "mean_max_drawdown_pct": round(statistics.mean([r.max_drawdown_pct for r in results]), 2),
        "total_fees_dollars": round(sum(r.fees_cents for r in results) / 100, 2),
        "halted_episodes": sum(1 for r in results if r.halted),
        "elapsed_s": round(time.time() - t0, 1),
        "episode_returns": [r.return_pct for r in results],
    }
    log.info("backtest: %d episodes, mean return %.2f%% (+/- %.2f), positive rate %.2f",
             n_ep, agg["mean_return_pct"], agg["stdev_return_pct"], agg["positive_rate"])
    return agg
