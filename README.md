# Kalshi Agent

A production-grade **prediction-market trading agent** for [Kalshi](https://kalshi.com) —
the CFTC-regulated exchange for binary event contracts. It scans markets, builds
probability estimates, sizes positions with fractional Kelly after fees, enforces
hard risk limits, executes orders, tracks P&L, and reports.

```
scan -> predict -> evaluate edge/EV -> risk gate -> execute -> manage exits -> settle -> report
```

> **Straight talk first.** Nothing traded with real money is "foolproof," and no
> predictor has guaranteed edge. What this project *does* prove: the machinery
> (accounting, fees, fills, risk limits, settlement) is correct under 57 automated
> tests; every order passes exactly one auditable risk gate; and defaults are
> bounded so the worst modeled case is a small drawdown, not a blown account.
> The alpha comes from *your* model — this is the chassis you bolt it into.

---

## Quickstart (no account needed)

```bash
pip install -r requirements.txt

# 1. Run the whole agent on a simulated exchange — no credentials, no risk
python -m kalshi_agent.cli demo

# 2. Monte-Carlo backtest: 40 independent simulated market universes
python -m kalshi_agent.cli backtest

# 3. Or serve the LIVE WEB DASHBOARD and open it in a browser
python -m kalshi_agent.cli web --port 8080 --mode sim
#    → http://localhost:8080  (Start/Pause/Reset buttons, live equity,
#      signals, positions, trades, risk state, log feed — auto-refreshes)

# 4. Look at the reports
open reports/status.html
```

`web --mode paper` runs the same dashboard agent against **real Kalshi market
data** (public endpoints, paper fills). The autonomous agent never gets web-based
real-money control — it stays behind the CLI gates described below.

### The "Ask to place a bet" prompt box

The dashboard also has a manual bet box (top of page), for discretionary bets:

- **Total funds** tile shows your **signed Kalshi exchange balance** (auto-refreshed
  every 5s and re-pulled immediately after any bet is placed).
- Type a ticket — `buy 2 yes KXNFLGAME-26NOV25-KC at 25c` (price optional; falls back
  to the best ask) — or plain English; with `ANTHROPIC_API_KEY` set on the server,
  Claude Haiku interprets the request and may only name real, currently-open markets
  from the watchlist series. Without the key, ticket syntax still works.
- **Nothing is sent automatically.** You get a confirmation page with the exact order,
  fees, balance-before/after, and every hard cap from `config-live.yaml` re-checked
  against fresh exchange state (contracts per order, price band, cash reserve,
  open-position limit, per-market and total exposure, risk halt). Confirm is
  single-use, expires after 2 minutes, and re-validates everything at send time.
- **Buys only, IOC limit orders, fee-aware.** Your `config-live.yaml` standards are
  exactly where they were — the 8¢ model edge gate is *not* applied to discretionary
  manual bets (it measures the model, not you); every other standard is enforced.
- Set `ANTHROPIC_API_KEY` (and optionally `ANTHROPIC_MODEL`) via `fly secrets set` —
  never commit it. `ANTHROPIC_API_KEY` is the only new secret; without it the
  natural-language path is disabled and the UI says so.

The demo spins up 18 synthetic markets whose hidden true probabilities evolve as
mean-reverting random walks, quotes imperfect books around them, and runs the
identical code path used live. Every fill, exit, block, and settlement is logged.

## What it does, step by step

1. **Scan** — fetch open markets, filter by liquidity, volume, category,
   close-time window, and price band (skips near-certain and near-impossible
   contracts where fees and tails eat you).
2. **Predict** — ensemble of four auditable components:
   - `consensus` — the order-book mid (instantaneous, noisy)
   - `ema` — EMA of candle closes: the price anchor built from many prints
   - `momentum` — short-window drift continuation
   - `base_rate` — optional per-category prior, decaying as settlement nears
3. **Evaluate** — for each side, compute edge after Kalshi-style taker fees
   (`fee = 0.07 × C × P × (1-P)`); require edge ≥ threshold; size with
   **exact Kelly for binary contracts** `n* = W(p−c) / (c(1−c))` where `c` is
   cost-per-contract including fee, then apply fractional Kelly and caps.
4. **Risk gate** — every intent must pass `RiskManager.pre_trade_check`:
   exits are always allowed (risk-reducing), entries must clear all caps.
5. **Execute** — paper broker fills against the evaluated book with IOC
   semantics; live broker sends IOC limit orders only (fill-now-or-cancel:
   no resting-order management to get wrong).
6. **Manage** — stop-loss, take-profit, thesis-reversal, and time exits.
7. **Settle** — resolved markets pay out (YES wins pay 100¢), risk state updates.
8. **Report** — self-contained HTML dashboard (inline CSS/SVG) + Markdown + JSON.

## Risk controls (the part that makes it hard to blow up)

| Control | Default | Behavior |
|---|---|---|
| Per-market cost-basis cap | 2% of equity | counts **existing** basis in that market |
| Total open exposure cap | 15% of equity | all positions' cost basis |
| Max open positions | 8 | |
| Cash reserve floor | 20% of equity | never spend below it |
| Daily realized-loss halt | −5% | halts trading until next UTC day |
| Loss-streak kill switch | 6 in a row | manual resume required |
| Stop-loss / take-profit | 15¢ / +5¢ | backtest-validated exits (16-combo sweep, 3 seeds) |
| Per-market cooldown | 60 min | after exiting at a loss |
| Ticker allow/deny lists | configurable | |
| Price band guard | 8¢–92¢ | no longshots, no near-certainties |
| Live-order gates | 4 of them | see below |

Exits (sells) bypass the exposure caps by design — **you must always be able to
get out.** They still require the engine not be halted and the ticker allowed.

## Going live — the gates

Live trading requires **all** of these, in order:

1. Credentials configured (`config.yaml` or `KALSHI_API_KEY_ID` +
   `KALSHI_PRIVATE_KEY_PATH`) — create a key at kalshi.com → Settings → API Keys,
   or run `python -m kalshi_agent.cli genkey` and upload the printed public key.
2. `mode: live` **and** `live_trading_enabled: true` in `config.yaml`.
3. The `--live` flag on the command line.
4. `KALSHI_LIVE_CONFIRM=YES` in the environment.
5. To actually send orders: `dry_run: false` in config **and** `--no-dry-run`.
   Otherwise the broker logs the exact order it *would* send.

```bash
# recommended path
python -m kalshi_agent.cli config-check        # verifies signing works
export KALSHI_DEMO=1                           # aim at demo-api.kalshi.co (fake money)
python -m kalshi_agent.cli live --live         # dry-run orders against the demo exchange
python -m kalshi_agent.cli paper               # paper-trade against REAL market data
# only when consistently profitable, and at sizes you can lose:
export KALSHI_LIVE_CONFIRM=YES
python -m kalshi_agent.cli live --live --no-dry-run
```

Safety commands: `cancel-all` (panic button), `resume` (clear a risk halt),
`status`, `report`, `analyze TICKER`, `scan`.

## API integration notes

- Base URL: `https://api.elections.kalshi.com/trade-api/v2`
  (demo: `https://demo-api.kalshi.co/trade-api/v2` via `KALSHI_DEMO=1`).
- Auth: RSA-PSS-SHA256 over `timestamp_ms + METHOD + path` (path excludes query),
  headers `KALSHI-ACCESS-KEY / -TIMESTAMP / -SIGNATURE`. Implemented in
  `kalshi_agent/auth.py` and verified by tests against a real keypair.
- Client features token-bucket rate limiting (default 8 req/s), exponential
  backoff on 429/5xx, pagination, and defensive parsing of the compressed
  order-book level format.
- Prices are integer cents (1–99) everywhere.

## Backtest methodology & current numbers

`kalshi-agent backtest` runs N independent simulated universes (different seeds =
different market sets) through the identical agent code path, with ground truth
known, then aggregates. With the shipped defaults (40 episodes × 96 cycles × 3h):

```
mean return        +10.9%      median +9.2%
stdev              ±9.4%       min −7.7%   max +37.3%
positive episodes  90%         t-stat 7.3
mean max drawdown  3.3%        win rate 67.5%   halted episodes 9
```

**Read this honestly:** these are *simulated* numbers on a synthetic market whose
inefficiencies the default predictor is shaped to find. They validate the
machinery and show the risk engine bounding losses — they do **not** predict real
Kalshi profitability. Real edge requires information or modeling the market does
not have. Use the backtester to measure *your* model's edge before risking money.

## Project layout

```
kalshi_agent/
  config.py      all knobs, YAML + env loading, validation
  auth.py        RSA-PSS signer + keypair generator
  client.py      REST client: retries, rate limits, pagination
  sim.py         deterministic simulated exchange (ground truth known)
  predictor.py   ensemble probability model
  strategy.py    edge/EV/fee math, Kelly sizing, entry+exit logic
  risk.py        hard risk gates with persistent state
  portfolio.py   cash/positions/P&L accounting (JSON persistence)
  brokers.py     PaperBroker (fills vs book) + LiveBroker (IOC orders)
  agent.py       the orchestrating cycle
  backtest.py    Monte-Carlo episodes + aggregate stats
  report.py      HTML dashboard (self-contained) + Markdown + JSON
  cli.py         all commands
tests/           57 tests: fees, Kelly, accounting identity, risk gates,
                 signing, client retries, paper fills, end-to-end sim runs
config.example.yaml
```

## Configuration

Copy `config.example.yaml` → `config.yaml` and edit. Every key is documented
inline. Environment overrides: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`,
`KALSHI_DEMO=1`, `KALSHI_LIVE_CONFIRM=YES`.

To plug in your own model: subclass `Predictor` and override `predict()` to
return a `Prediction(p_blend, confidence, components)` — strategy, risk, sizing,
and execution all stay unchanged. The base-rate table (`base_rates_path`) takes
a YAML map like `{Sports: 0.5, Economics: 0.48}`.

## Tests

```bash
python -m pytest tests/ -q
```

Covers: fee formula, Kelly optimum, entry/exit logic, every risk gate (including
the per-market cap counting *existing* basis), portfolio conservation identity
(`cash = bankroll + realized − buy fees`), settlement vs ground truth,
RSA-PSS signature verification, client retry/backoff/auth errors, paper fill
mechanics, determinism per seed, and the risk halt blocking entries end-to-end.

## Known limitations

- Live fills are reconciled from `GET /portfolio/fills`; if Kalshi renames
  response fields, update `brokers.LiveBroker`.
- No WebSocket feed — polls REST. Add streaming if you need sub-second reaction.
- Taker-only (IOC). Maker/post-only quoting is a natural extension of the same
  strategy objects.
- The default predictor is a framework, not an oracle. Its honest job is to be
  predictable, auditable, and replaceable.

**Not financial advice. Event contracts can and do lose money. Trade at your own
risk, start in paper mode, and never risk what you cannot afford to lose.**

## Public access (outside the preview frame)

The deployed dashboard is publicly reachable at the Arena preview URL for this
workspace's `API Keys & Dashboard` process (see the running process in your
Arena session — the URL looks like `https://<sandbox-id>.arena.site/`).

- It is **not** bound to the preview iframe: anyone on the internet can reach
  it and see the sign-in page.
- Everything except `/healthz` sits behind the password gate (env
  `KALSHI_WEB_PASSWORD`, 128-bit random — stored outside git at
  `state/web_password.txt`, mode 0600). Successful sign-in redirects to the
  token-prefixed URL, which proxies correctly through the Arena gateway.
- Rotating: stop the web process, restart with a new
  `KALSHI_WEB_PASSWORD` — the URL token rotates with it and all old links die.
- Do **not** commit the password or the URL into a public repo without
  understanding that the app fronts a REAL-money account (live trading +
  API-key download behind the gate).
- Durability: the public URL lives as long as this sandbox and the web
  process live. A permanent host requires your own infrastructure (see above) —
  treat this as published-but-ephemeral, not production hosting.

## Deployment

Self-hosting / production: see **DEPLOY.md** — includes a secret-free
`Dockerfile` (fail-closed gate), `docker-compose.yml`, a Fly.io blueprint
(`fly.toml`), a Render blueprint (`render.yaml`), and step-by-step notes for a
named Cloudflare tunnel. The image never contains credentials; everything is
injected at runtime via env vars + a `state` volume.
