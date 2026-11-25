"""Command-line interface.

    kalshi-agent demo          run the full agent on the simulated exchange (no credentials)
    kalshi-agent scan          list liquid open markets on Kalshi (real API)
    kalshi-agent analyze TICK  predict + evaluate one market (real API)
    kalshi-agent paper         run the agent in paper mode on real Kalshi data
    kalshi-agent live          run the agent live (multi-gated: see README)
    kalshi-agent status        print portfolio + risk state
    kalshi-agent report        regenerate status reports (md + html)
    kalshi-agent backtest      Monte-Carlo backtest over simulated episodes
    kalshi-agent config-check  validate config/credentials with a signed API call
    kalshi-agent genkey        generate an RSA keypair for Kalshi
    kalshi-agent cancel-all    cancel all resting live orders (panic button)
    kalshi-agent resume        clear a risk-engine halt
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

from . import __version__
from .agent import Agent
from .auth import KalshiSigner, generate_keypair
from .backtest import run_backtest
from .brokers import LiveBroker, PaperBroker
from .client import KalshiApiError, KalshiClient
from .config import AppConfig
from .models import Market, OrderBook, merge_candles
from .portfolio import Portfolio
from .predictor import BaseRateTable, Predictor
from .report import dump_json, write_reports
from .risk import RiskManager
from .strategy import Strategy
from .sim import SimExchange

log = logging.getLogger("kalshi.cli")


# ------------------------------------------------------------------ setup ---
def make_client(cfg: AppConfig, require_auth: bool = False) -> KalshiClient:
    signer = None
    if cfg.has_credentials():
        signer = KalshiSigner.from_file(cfg.api_key_id, cfg.private_key_path)
    elif require_auth:
        raise SystemExit(
            "Credentials required. Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH "
            "(or run `kalshi-agent genkey` and configure config.yaml)."
        )
    return KalshiClient(cfg, signer=signer)


def setup_logging(cfg: AppConfig) -> None:
    os.makedirs(cfg.state_dir, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(os.path.join(cfg.state_dir, "agent.log")))
    except OSError:
        pass
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO),
                        format=fmt, handlers=handlers, force=True)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build_agent(cfg: AppConfig, source, portfolio: Portfolio, state_dir: str) -> Agent:
    broker = PaperBroker(cfg) if cfg.mode == "paper" else LiveBroker(cfg, make_client(cfg, require_auth=True))
    risk = RiskManager(cfg, state_dir=state_dir)
    return Agent(cfg, source, portfolio, broker, risk,
                 start_of_day_equity=float(portfolio.starting_cash_cents))


# ------------------------------------------------------------------ demo ---
def cmd_demo(args: argparse.Namespace, cfg: AppConfig) -> None:
    cfg.mode = "paper"
    seed = args.seed if args.seed is not None else cfg.demo_seed
    cycles = args.cycles if args.cycles is not None else cfg.demo_cycles
    n_markets = args.markets if args.markets is not None else cfg.demo_markets
    hours = cfg.demo_hours_per_cycle
    print(f"=== Kalshi Agent DEMO — simulated exchange, {n_markets} markets, "
          f"{cycles} cycles x {hours}h, seed {seed}, bankroll {cfg.bankroll_cents / 100:.0f}$ ===")
    print("No credentials used; every fill is synthetic. This proves the machinery,")
    print("not real-world edge.\n")

    sim = SimExchange(seed=seed, n_markets=n_markets)
    state_dir = os.path.join(cfg.state_dir, "demo")
    pf = Portfolio(cfg.bankroll_cents, state_dir=state_dir, name="demo")
    agent = build_agent(cfg, sim, pf, state_dir)

    for i in range(cycles):
        sim.step(hours)
        if agent.risk.state.get("halted"):
            print(f"cycle {i + 1:>3}: RISK HALT — {agent.risk.state['halt_reason']}")
            break
        res = agent.cycle()
        print(f"cycle {i + 1:>3} | equity {res.equity_cents / 100:>8,.2f}$ | "
              f"cash {res.cash_cents / 100:>8,.2f}$ | pos {len(pf.positions):>2} | "
              f"entries {res.entries:>2} exits {res.exits:>2} settled {res.settled:>2} "
              f"blocked {res.blocked_by_risk:>2}")

    # wind down: advance the sim until every held market settles
    guard = 0
    while pf.positions and guard < 400:
        sim.step(6.0)
        agent.settle_markets([])
        guard += 1

    risk_summary = agent.risk.summary()
    snap = pf.snapshot({})
    print("\n=== FINAL (all markets settled) ===")
    print(pf.summary_text({}))
    settled_log = sim.settlement_log
    print(f"\nsim settlements: {len(settled_log)} markets resolved "
          f"({sum(1 for s in settled_log if s['settlement_value'] == 100)} YES / "
          f"{len(settled_log) - sum(1 for s in settled_log if s['settlement_value'] == 100)} NO)")

    extra = ""
    if pf.closed:
        best = max(pf.closed, key=lambda t: t.pnl_cents)
        worst = min(pf.closed, key=lambda t: t.pnl_cents)
        extra = (f"<div style='background:#101a2e;border:1px solid #1e293b;border-radius:10px;"
                 f"padding:14px;margin-bottom:16px;font-size:13px;'>"
                 f"<div style='color:#64748b;font-size:11px;margin-bottom:8px;'>RUN NOTES</div>"
                 f"best trade {best.ticker} {best.pnl_cents / 100:+.2f}$ &nbsp; worst {worst.ticker} "
                 f"{worst.pnl_cents / 100:+.2f}$ &nbsp; fees paid "
                 f"{sum(f.fee_cents for f in pf.fills) / 100:.2f}$</div>")
    _, htm = write_reports(cfg, pf, risk_summary, title=f"Kalshi Agent — Demo (seed {seed})",
                           extra_panels=extra)
    print(f"report: {htm}")
    print(f"portfolio state: {pf.path}")
    dump_json(os.path.join(cfg.reports_dir, "demo_summary.json"), {
        "final": snap, "risk": risk_summary,
        "settlements": settled_log, "closed_trades": [
            {k: getattr(t, k) for k in ("ticker", "side", "count", "entry_price_cents",
                                        "exit_price_cents", "pnl_cents", "why")}
            for t in pf.closed]})


# ------------------------------------------------------------- real data ---
def cmd_scan(args: argparse.Namespace, cfg: AppConfig) -> None:
    client = make_client(cfg)
    markets = client.get_markets(status="open", limit=cfg.scan_limit, max_pages=args.pages)
    rows = []
    for d in markets:
        m = Market.from_api(d)
        if m.status != "open":
            continue
        if cfg.categories and m.category not in cfg.categories:
            continue
        rows.append(m)
    rows.sort(key=lambda m: -(m.liquidity_cents or m.volume * 100))
    print(f"{'ticker':<34} {'cat':<12} {'yes bid/ask':>11} {'vol':>8} {'liq $':>8}  title")
    for m in rows[: args.top]:
        print(f"{m.ticker:<34} {m.category[:11]:<12} {m.yes_bid:>4}/{m.yes_ask:<4} "
              f"{m.volume:>8} {m.liquidity_cents / 100:>8.0f}  {m.title[:60]}")
    print(f"\n{len(rows)} open markets (showing top {min(args.top, len(rows))} by liquidity)")


def cmd_analyze(args: argparse.Namespace, cfg: AppConfig) -> None:
    client = make_client(cfg)
    md = Market.from_api(client.get_market(args.ticker))
    ob = OrderBook.from_api(client.get_orderbook(args.ticker), args.ticker)
    end = int(time.time())
    candles = merge_candles(client.get_candlesticks(
        args.ticker, end - cfg.candle_lookback_hours * 3600, end,
        period_interval=cfg.candle_interval_minutes))
    pred = Predictor(cfg, BaseRateTable.from_yaml(cfg.base_rates_path)).predict(md, ob, candles)
    sig = Strategy(cfg).evaluate_entry(md, ob, pred, cfg.bankroll_cents, candles)
    print(f"market   : {md.ticker}  [{md.category}]  status={md.status}")
    print(f"title    : {md.title} — {md.subtitle}")
    print(f"close    : {md.close_time}  ({md.hours_to_close():.1f}h)  vol={md.volume} liq={md.liquidity_cents / 100:.0f}$")
    print(f"book     : yes {ob.best_bid('yes').price_cents if ob.best_bid('yes') else '-'}c bid / "
          f"{ob.best_ask('yes').price_cents if ob.best_ask('yes') else '-'}c ask  "
          f"spread {ob.spread_cents()}c  mid {ob.mid_cents()}")
    print(f"model    : p={pred.p_blend:.3f}  confidence={pred.confidence:.2f}")
    for k, v in pred.components.items():
        print(f"   {k:<10} {v:.3f}")
    for n in pred.notes:
        print(f"   note: {n}")
    print(f"signal   : side={sig.side or '-'} ask={sig.exec_price_cents or '-'}c "
          f"edge={sig.edge_cents:+.1f}c ev={sig.ev_cents:+.1f}c size={sig.kelly_contracts}")
    print(f"verdict  : {'TRADE ' + str(sig.kelly_contracts) + ' contracts' if sig.tradeable else 'NO TRADE — ' + sig.skip_reason}")
    for r in sig.reasons:
        print(f"   {r}")


def cmd_paper(args: argparse.Namespace, cfg: AppConfig) -> None:
    cfg.mode = "paper"
    client = make_client(cfg, require_auth=False)
    state_dir = os.path.join(cfg.state_dir, "paper")
    pf = Portfolio.load(state_dir + "/paper.json", name="paper") \
        if os.path.exists(state_dir + "/paper.json") else Portfolio(cfg.bankroll_cents, state_dir=state_dir, name="paper")
    agent = build_agent(cfg, client, pf, state_dir)
    try:
        agent.run(cycles=args.cycles, poll_seconds=args.poll)
    except KeyboardInterrupt:
        pass
    print("\n" + pf.summary_text(agent.last_marks))
    _, htm = write_reports(cfg, pf, agent.risk.summary(), title="Kalshi Agent — Paper (real data)")
    print(f"report: {htm}")


# ----------------------------------------------------------------- live ---
def cmd_live(args: argparse.Namespace, cfg: AppConfig) -> None:
    confirm = os.environ.get("KALSHI_LIVE_CONFIRM", "")
    if not args.live:
        raise SystemExit("Refusing to trade live without the --live flag.")
    if confirm != "YES":
        raise SystemExit("Refusing to trade live without KALSHI_LIVE_CONFIRM=YES in the environment.")
    if not cfg.live_trading_enabled:
        raise SystemExit("Refusing to trade live without live_trading_enabled: true in config.yaml.")
    if not cfg.has_credentials():
        raise SystemExit("Missing credentials: KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH.")
    if cfg.dry_run and args.no_dry_run:
        cfg.dry_run = False
        print("DRY-RUN OFF — real orders will be sent. Press Ctrl+C within 5s to abort.")
        time.sleep(5)
    client = make_client(cfg, require_auth=True)
    bal = client.get_balance()
    log.info("exchange balance: %.2f$ (demo env: %s)", bal.get("balance", 0) / 100, cfg.use_demo_env)
    state_dir = os.path.join(cfg.state_dir, "live")
    pf = Portfolio.load(state_dir + "/live.json", name="live") \
        if os.path.exists(state_dir + "/live.json") else Portfolio(bal.get("balance", 0), state_dir=state_dir, name="live")
    # cross-check local mirror vs the exchange — loud warnings, never blocks.
    # If these diverge, a fill was missed in reconciliation or something was
    # traded outside this agent; trust the exchange over state/live/live.json.
    try:
        exch_positions = client.get_positions(status="open")
        exch_open = sum(1 for p in exch_positions
                        if float(p.get("position_fp") or p.get("position") or 0) != 0)
        local_open = sum(1 for p in pf.positions.values() if p.yes_count or p.no_count)
        if exch_open != local_open:
            log.warning("DIVERGENCE: exchange shows %d open position(s), local mirror shows %d — "
                        "reconcile before trusting local P&L", exch_open, local_open)
        bal_cents = bal.get("balance")
        if bal_cents is not None and abs(float(bal_cents) - pf.cash_cents) > 5:
            log.warning("DIVERGENCE: exchange cash %.2f$ vs local cash %.2f$ — "
                        "an unreconciled fill may exist", bal_cents / 100, pf.cash_cents / 100)
    except Exception as exc:  # noqa: BLE001
        log.warning("exchange cross-check failed (continuing): %s", exc)
    agent = build_agent(cfg, client, pf, state_dir)
    agent.run(cycles=args.cycles, poll_seconds=args.poll)


def cmd_status(args: argparse.Namespace, cfg: AppConfig) -> None:
    shown = False
    for name in ("demo", "paper", "live"):
        path = os.path.join(cfg.state_dir, name, f"{name}.json")
        if os.path.exists(path):
            pf = Portfolio.load(path, name=name)
            print(f"--- {name} mode state ---")
            print(pf.summary_text({}))
            print()
            shown = True
    if not shown:
        print("No portfolio state yet. Run `kalshi-agent demo` first.")
    risk_path = os.path.join(cfg.state_dir, "risk_state.json")
    if os.path.exists(risk_path):
        import json

        with open(risk_path) as fh:
            print("risk state:", json.dumps(json.load(fh), indent=2)[:400])


def cmd_report(args: argparse.Namespace, cfg: AppConfig) -> None:
    for name in ("demo", "paper", "live"):
        path = os.path.join(cfg.state_dir, name, f"{name}.json")
        if os.path.exists(path):
            pf = Portfolio.load(path, name=name)
            import json

            rp = os.path.join(cfg.state_dir, name, "risk_state.json")
            rs = {}
            if os.path.exists(rp):
                with open(rp) as fh:
                    rs = json.load(fh)
            from .risk import RiskManager  # local: only for summary shape

            summary = {"halted": rs.get("halted", False), "halt_reason": rs.get("halt_reason", ""),
                       "consecutive_losses": rs.get("consecutive_losses", 0)}
            _, htm = write_reports(cfg, pf, summary, title=f"Kalshi Agent — {name}")
            print(f"{name}: {htm}")
            return
    print("No state found — run `kalshi-agent demo` first.")


def cmd_backtest(args: argparse.Namespace, cfg: AppConfig) -> None:
    agg = run_backtest(cfg, episodes=args.episodes, cycles=args.cycles, base_seed=args.seed)
    print("\n=== BACKTEST (simulated episodes, same code path as live) ===")
    for k, v in agg.items():
        if k != "episode_returns":
            print(f"  {k:<24} {v}")
    dump_json(os.path.join(cfg.reports_dir, "backtest.json"), agg)
    print(f"saved -> {os.path.join(cfg.reports_dir, 'backtest.json')}")


def cmd_config_check(args: argparse.Namespace, cfg: AppConfig) -> None:
    print(f"mode            : {cfg.mode}")
    print(f"demo env        : {cfg.use_demo_env}")
    print(f"base url        : {cfg.base_url}")
    print(f"api key id      : {cfg.api_key_id or '(not set)'}")
    print(f"private key     : {cfg.private_key_path or '(not set)'} "
          f"{'(exists)' if cfg.private_key_path and os.path.exists(cfg.private_key_path) else ''}")
    print(f"live trading    : {cfg.live_trading_enabled} (dry_run={cfg.dry_run})")
    if cfg.has_credentials():
        client = make_client(cfg)
        try:
            bal = client.get_balance()
            print(f"signed API call : OK — balance {bal.get('balance', 0) / 100:.2f}$")
        except KalshiApiError as exc:
            print(f"signed API call : FAILED — {exc}")
    else:
        print("signed API call : skipped (no credentials) — market-data commands still work")


def cmd_genkey(args: argparse.Namespace) -> None:
    pub, priv = generate_keypair()
    path = args.out
    if os.path.exists(path) and not args.force:
        raise SystemExit(f"{path} already exists; use --force to overwrite.")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(priv)
    os.chmod(path, 0o600)
    print(f"private key written to {path} (chmod 600). NEVER commit or share it.")
    print("\nUpload this PUBLIC key at kalshi.com -> Settings -> API Keys -> Add key:\n")
    print(pub)


def cmd_web(args: argparse.Namespace, cfg: AppConfig) -> None:
    """Serve the live web dashboard (sim or paper-real-data mode)."""
    from .web import serve

    if args.scan_limit:
        cfg.scan_limit = min(cfg.scan_limit, args.scan_limit)
    serve(cfg, port=args.port, mode=args.mode, autostart=not args.no_autostart,
          landing=args.landing)


def cmd_install_key(args: argparse.Namespace, cfg: AppConfig) -> None:
    """Install + validate credentials from a PEM file (e.g. chat upload)."""
    from .web import install_credentials

    pem_path = args.pem_path
    if not pem_path and args.uploads:
        cands = sorted(p for p in os.listdir(args.uploads) if p.endswith(".pem"))
        if not cands:
            raise SystemExit(f"no .pem files found in {args.uploads} — attach the key file in chat first")
        pem_path = os.path.join(args.uploads, cands[-1])
        print(f"using {pem_path}")
    if not pem_path or not os.path.exists(pem_path):
        raise SystemExit(f"key file not found: {pem_path}")
    with open(pem_path, "r", encoding="utf-8") as fh:
        pem = fh.read()
    validator = (lambda kid, p, base: (True, "skipped by --skip-validation")) \
        if args.skip_validation else None
    ok, msg = install_credentials(cfg, args.key_id, pem, args.env,
                                  persist=not args.no_persist, validator=validator)
    print(("OK  — " if ok else "FAILED — ") + msg)
    if ok:
        print("the running dashboard will pick this up automatically (status page -> VALIDATED)")
        if not args.no_persist:
            print(f"stored: {os.path.join(cfg.state_dir, 'web', 'credentials')}")


def cmd_cancel_all(args: argparse.Namespace, cfg: AppConfig) -> None:
    client = make_client(cfg, require_auth=True)
    n = client.cancel_all_orders()
    print(f"canceled {n} resting order(s)")


def cmd_resume(args: argparse.Namespace, cfg: AppConfig) -> None:
    mode = getattr(args, "mode", "live")
    dirs = []
    if mode in ("live", "all"):
        dirs.append(os.path.join(cfg.state_dir, "live"))
    if mode in ("paper", "all"):
        dirs.append(os.path.join(cfg.state_dir, "paper"))
    if mode in ("demo", "all"):
        dirs.append(os.path.join(cfg.state_dir, "demo"))
    if mode == "all":
        dirs.append(cfg.state_dir)
    for d in dirs:
        RiskManager(cfg, state_dir=d).resume()
        print(f"risk halt cleared in {d}/risk_state.json")
    print("note: a RUNNING agent caches risk state in memory — restart it to pick this up")


# ------------------------------------------------------------------ main ---
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kalshi-agent", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yaml", help="path to config.yaml")
    p.add_argument("--version", action="version", version=f"kalshi-agent {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, help_: str, fn) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    d = add("demo", "run the full agent on the simulated exchange (no credentials)", cmd_demo)
    d.add_argument("--seed", type=int)
    d.add_argument("--cycles", type=int)
    d.add_argument("--markets", type=int)
    d.add_argument("--bankroll", type=int, help="starting cash in dollars")

    s = add("scan", "list liquid open markets", cmd_scan)
    s.add_argument("--pages", type=int, default=2)
    s.add_argument("--top", type=int, default=25)

    a = add("analyze", "predict + evaluate a single market ticker", cmd_analyze)
    a.add_argument("ticker")

    pp = add("paper", "run the agent in paper mode on real Kalshi data", cmd_paper)
    pp.add_argument("--cycles", type=int, default=None)
    pp.add_argument("--poll", type=int, default=30)

    lv = add("live", "run the agent LIVE (requires --live + env confirm + config)", cmd_live)
    lv.add_argument("--live", action="store_true", help="explicit live opt-in")
    lv.add_argument("--cycles", type=int, default=None)
    lv.add_argument("--poll", type=int, default=30)
    lv.add_argument("--no-dry-run", action="store_true", help="actually send orders")

    add("status", "print portfolio + risk state", cmd_status)
    add("report", "regenerate status reports", cmd_report)

    bt = add("backtest", "Monte-Carlo backtest on simulated episodes", cmd_backtest)
    bt.add_argument("--episodes", type=int)
    bt.add_argument("--cycles", type=int)
    bt.add_argument("--seed", type=int)

    add("config-check", "validate config + credentials", cmd_config_check)

    g = sub.add_parser("genkey", help="generate an RSA keypair for Kalshi")
    g.add_argument("--out", default="kalshi_private_key.pem")
    g.add_argument("--force", action="store_true")

    w = add("web", "serve the live web dashboard (sim or paper-real-data)", cmd_web)
    w.add_argument("--port", type=int, default=8080)
    w.add_argument("--mode", default="sim", choices=["sim", "paper", "account"],
                   help="account = real-money console: signed funds + manual-only prompt box, no agent/simulator")
    w.add_argument("--scan-limit", type=int, help="cap markets scanned per cycle (paper mode)")
    w.add_argument("--no-autostart", action="store_true", help="don't start the agent on boot")
    w.add_argument("--landing", default="dashboard", choices=["dashboard", "credentials"],
                   help="which page the root URL shows")

    ik = add("install-key", "install + validate Kalshi credentials from a PEM file",
             cmd_install_key)
    ik.add_argument("--key-id", required=True)
    ik.add_argument("--pem-path", default="", help="path to private key PEM")
    ik.add_argument("--uploads", default="/home/user/uploads",
                    help="folder to auto-find a .pem when --pem-path is omitted")
    ik.add_argument("--env", default="demo", choices=["demo", "live"])
    ik.add_argument("--skip-validation", action="store_true",
                    help="store without the signed test call (validate later on the dashboard)")
    ik.add_argument("--no-persist", action="store_true", help="validate only, do not store")

    add("cancel-all", "cancel all resting live orders", cmd_cancel_all)
    r = add("resume", "clear a risk-engine halt", cmd_resume)
    r.add_argument("--mode", default="live", choices=["live", "paper", "demo", "all"],
                   help="which risk state file to clear (default: live)")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    cfg = AppConfig.load(args.config)
    if args.cmd == "demo" and getattr(args, "bankroll", None):
        cfg.bankroll_cents = args.bankroll * 100
    setup_logging(cfg)
    if args.cmd == "genkey":
        cmd_genkey(args)
        return
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
