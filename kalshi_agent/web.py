"""Live web dashboard + agent control server (Python stdlib only, no dependencies).

Modes:
  sim   — autonomous agent on the simulated exchange (no credentials, fast clock)
  paper — autonomous agent on REAL Kalshi market data, paper fills (public
          endpoints; no credentials needed for market data)

Real-money live trading intentionally has NO web control from the autonomous agent;
it stays behind the CLI's four opt-in gates (see README). The dashboard's
“Ask to place a bet” prompt box is the single deliberate exception: every bet is
proposed to a human, staged against hard risk caps from config-live.yaml, and
sent to Kalshi only after an explicit per-bet confirmation (buys and sells that
close existing positions — never shorts; IOC limit, fee-aware). See
kalshi_agent/webbets.py.

Endpoints:
  GET  /           dashboard (server-rendered, meta-refresh every 5 s, no JS needed)
  GET  /api/state  JSON snapshot
  GET  /api/funds  signed Kalshi balance JSON
  POST /start      start the agent loop
  POST /pause      pause the agent loop
  POST /reset      wipe state, new universe (auto-restarts if it was running)
  POST /mode       switch sim <-> paper (resets state)
  POST /bet/preview   interpret prompt, stage order, render confirmation
  POST /bet/confirm   re-validate + send staged order
  POST /bet/cancel    discard staged order
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import random
import re
import secrets
import shutil
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .agent import Agent
from .auth import KalshiSigner, generate_keypair
from .brokers import PaperBroker, _fp_count, _fp_price_cents, _fp_fee_cents
from .client import KalshiClient, KalshiApiError
from .config import AppConfig, DEFAULT_BASE_URL, DEMO_BASE_URL
from .models import OrderBook
from .portfolio import Portfolio
from .risk import RiskManager
from .sim import SimExchange
from .report import sparkline_svg
from . import webbets

log = logging.getLogger("kalshi.web")

# --------------------------------------------------------------- password ---
# One shared password guards the whole app (dashboard, credentials, API).
# Case-sensitive.  MUST be supplied via env KALSHI_WEB_PASSWORD — there is
# deliberately no default so a fresh checkout can never authenticate with a
# guessed or source-leaked password.  When unset, every login attempt is
# rejected (fail closed) and a warning is logged at startup.
APP_PASSWORD = os.environ.get("KALSHI_WEB_PASSWORD") or None
if APP_PASSWORD is None:
    log.warning("KALSHI_WEB_PASSWORD is not set — the web gate will reject ALL "
                "logins until it is set (e.g. in the process env). Failing closed.")
_SESSION_COOKIE = "kalshi_session"
_SESSION_MSG = b"kalshi-agent-session:v1"
_SESSION_MAX_AGE = 12 * 3600          # seconds
_MAX_BODY_BYTES = 64 * 1024           # reject absurd POST bodies


def _session_token() -> str:
    return hmac.new(APP_PASSWORD.encode("utf-8"), _SESSION_MSG,
                    hashlib.sha256).hexdigest()


def _pw_matches(candidate: str | None) -> bool:
    """Fail closed when no password is configured."""
    if not candidate or APP_PASSWORD is None:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"),
                               APP_PASSWORD.encode("utf-8"))


def _path_token() -> str:
    """Short token embedded in the authenticated URL prefix (/app/<token>/...).

    The preview gateway strips Cookie AND Authorization headers, so the URL
    itself has to carry the session.  This is the HMAC of the app password —
    the password itself never appears in addresses.  Rotating the password
    invalidates all outstanding links."""
    if APP_PASSWORD is None:
        return "0" * 32   # stable dummy; no prefixed URL will match a real token
    return _session_token()[:32]


def _base() -> str:
    return f"/app/{_path_token()}"


def _strip_base(path: str) -> tuple[bool, str]:
    """Split an optional /app/<token> prefix off a request path.

    Returns (path_was_prefixed_and_valid, app_path)."""
    b = _base()
    if path == b:
        return True, "/"
    if path.startswith(b + "/"):
        return True, path[len(b):]
    if path.startswith("/app/"):
        # looks like a prefixed URL with a wrong token -> do not reveal
        return False, path
    return False, path


def _parse_cookie(header: str | None, name: str) -> str:
    if not header:
        return ""
    for part in header.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() == name:
                return v.strip()
    return ""


def _basic_password(headers) -> str | None:
    auth = headers.get("Authorization") or ""
    if not auth.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(auth[6:].strip()).decode("utf-8")
    except Exception:  # noqa: BLE001
        return None
    if ":" not in decoded:
        return None
    return decoded.partition(":")[2]   # anything:password — password must match


def _is_authed(headers) -> bool:
    if APP_PASSWORD is None:
        return False   # fail closed: no password configured, no access
    # Preferred for browsers: HTTP Basic (survives proxies that strip cookies)
    pw = _basic_password(headers)
    if _pw_matches(pw):
        return True
    # Fallback for direct access where cookies work
    raw = headers.get("Cookie")
    tok = _parse_cookie(raw, _SESSION_COOKIE)
    return bool(tok) and hmac.compare_digest(tok.encode("utf-8"),
                                             _session_token().encode("utf-8"))


def _wipe_web_state(web_state: str) -> None:
    """Wipe the sim/paper state below state/web — but ALWAYS preserve the
    saved Kalshi credentials folder, so Reset / mode-switch never destroys a
    key the user went to the trouble of uploading."""
    if not os.path.isdir(web_state):
        return
    for name in os.listdir(web_state):
        if name == "credentials":
            continue
        p = os.path.join(web_state, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.remove(p)
            except OSError:
                pass
    log.info("dashboard: state wiped (credentials preserved)")


class _RingBufferHandler(logging.Handler):
    """Captures log lines for the dashboard feed."""

    def __init__(self, maxlen: int = 250):
        super().__init__()
        self.ring: deque[str] = deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.ring.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass


def _esc(s) -> str:
    return html.escape(str(s))


class WebDashboard:
    """Owns the agent world (source, portfolio, agent) and the worker thread."""

    def __init__(self, cfg: AppConfig, mode: str = "sim"):
        self.cfg = cfg
        self.mode = mode if mode in ("sim", "paper", "account") else "sim"
        self._control_lock = threading.RLock()
        self._display_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self.sim: SimExchange | None = None
        self.portfolio: Portfolio | None = None
        self.agent: Agent | None = None
        self.log_handler = _RingBufferHandler()
        self.validator = None   # injectable (key_id, pem, base_url) -> (ok, msg) for tests
        logging.getLogger().addHandler(self.log_handler)

        self.credentials: dict = {
            "key_id": "", "pem": "", "env": "", "validated": False,
            "balance_cents": None, "error": "", "fingerprint": "",
            "generated_public_pem": "", "generated_private_pem": "",
        }
        self._load_persisted_credentials()
        self._load_hunt()

        # manual betting (prompt box) — hard caps from config-live.yaml
        self._live_cfg, self._live_cfg_ok = webbets.load_live_cfg()
        self._funds_cache: dict = {"cents": None, "as_of": 0.0, "env": "", "error": ""}
        self._pending_bets: dict[str, dict] = {}   # token -> staged bet (120 s TTL)
        self._chat: list[tuple[str, str]] = [
            ("app", "Real-money console. Type a bet — ticket or plain English — e.g. "
                    "“buy 1 yes KXNBAGAME-26NOV25-LAL 22c” or “close out my chiefs bet”. "
                    "I’ll stage it and ask YES/NO before anything is sent."),
        ]
        self._chat_pending: str | None = None        # token of the bet awaiting YES/NO
        self._bet_queue: list[str] = []              # remaining batch tokens after the current one
        self._bet_batch: dict | None = None          # {"staged","done","filled","refused","passed","note"}
        self._account_cache: dict = {"ts": 0.0, "positions": None, "fills": None, "error": ""}

        self.display: dict = {
            "mode": self.mode, "running": False, "cycles": 0, "started_ts": None,
            "last_cycle_ts": None, "last_error": "", "equity_cents": float(cfg.bankroll_cents),
            "cash_cents": float(cfg.bankroll_cents), "return_pct": 0.0,
            "positions": [], "closed": [], "risk": {}, "signals": [],
            "equity_history": [], "last_res": {}, "universe_seed": None,
        }
        if self.mode != "account":
            self._build_world()
        else:
            self.sim = None
            self.portfolio = None
            self.agent = None

    # ------------------------------------------------------------- world ---
    def _cred_dir(self) -> str:
        return os.path.join(self.cfg.state_dir, "web", "credentials")

    def _load_persisted_credentials(self) -> None:
        """If the user chose to persist credentials earlier, reload them."""
        import hashlib

        meta_path = os.path.join(self._cred_dir(), "meta.json")
        pem_path = os.path.join(self._cred_dir(), "kalshi_private_key.pem")
        try:
            with open(meta_path) as fh:
                meta = json.load(fh)
            with open(pem_path, "r", encoding="utf-8") as fh:
                pem = fh.read()
        except (OSError, json.JSONDecodeError):
            return
        self.credentials.update({
            "key_id": meta.get("key_id", ""), "pem": pem,
            "env": meta.get("env", "demo"), "validated": False,
            "error": "", "balance_cents": None,
            "fingerprint": hashlib.sha256(pem.encode()).hexdigest()[:10],
        })
        self.cfg.api_key_id = meta.get("key_id", self.cfg.api_key_id)
        self.cfg.use_demo_env = meta.get("env", "demo") == "demo"
        self.cfg.base_url = DEMO_BASE_URL if self.cfg.use_demo_env else DEFAULT_BASE_URL
        self.cfg.private_key_path = pem_path
        log.info("dashboard: loaded persisted credentials for key id %s…",
                 (meta.get("key_id") or "")[:6])

    # -------------------------------------------------------- credentials ---
    def save_credentials(self, key_id: str, pem: str, env: str,
                         persist: bool = False) -> tuple[bool, str]:
        """Validate credentials with a real signed call, then store them."""
        import hashlib

        key_id, pem, env = key_id.strip(), pem.strip(), ("demo" if env == "demo" else "live")
        if not key_id:
            return False, "API key id is required"
        if "PRIVATE KEY" not in pem:
            return False, "that doesn't look like a PEM private key (paste the full -----BEGIN/END----- block)"
        base = DEMO_BASE_URL if env == "demo" else DEFAULT_BASE_URL
        try:
            signer = KalshiSigner(key_id, pem.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return False, f"could not parse private key: {exc}"

        if self.validator is not None:
            ok, msg = self.validator(key_id, pem, base)
            if not ok:
                return False, msg
            balance = None
        else:
            probe_cfg = AppConfig()
            probe_cfg.base_url = base
            probe_cfg.rate_limit_per_sec = 4.0
            client = KalshiClient(probe_cfg, signer=signer)
            try:
                bal = client.get_balance()
                balance = bal.get("balance")
            except KalshiApiError as exc:
                hint = ""
                if exc.status in (401, 403):
                    hint = " — check the key id, that the public key is uploaded at kalshi.com, and that the environment matches"
                return False, f"Kalshi rejected the credentials ({exc.status}):{hint} {exc}"
            except Exception as exc:  # noqa: BLE001
                return False, f"network error contacting {base}: {exc}"

        self.credentials.update({
            "key_id": key_id, "pem": pem, "env": env, "validated": True,
            "balance_cents": balance, "error": "",
            "fingerprint": hashlib.sha256(pem.encode("utf-8")).hexdigest()[:10],
        })
        self.cfg.api_key_id = key_id
        self.cfg.use_demo_env = env == "demo"
        self.cfg.base_url = base
        if persist:
            d = self._cred_dir()
            os.makedirs(d, exist_ok=True)
            pem_path = os.path.join(d, "kalshi_private_key.pem")
            _atomic_write(pem_path, pem, mode=0o600)
            _atomic_write(os.path.join(d, "meta.json"),
                          json.dumps({"key_id": key_id, "env": env}))
            self.cfg.private_key_path = pem_path
        log.warning("dashboard: credentials saved for %s… (env=%s, validated)",
                    key_id[:6], env)
        return True, (f"validated — signed call succeeded"
                      + (f", balance {balance / 100:.2f}$" if balance is not None else ""))

    def generate_keypair(self) -> None:
        pub, priv = generate_keypair()
        self.credentials["generated_public_pem"] = pub
        self.credentials["generated_private_pem"] = priv
        log.warning("dashboard: generated a fresh RSA keypair — upload the PUBLIC key at kalshi.com, then save the key id")

    def clear_credentials(self) -> None:
        self.credentials.update({
            "key_id": "", "pem": "", "env": "", "validated": False,
            "balance_cents": None, "error": "",
            "generated_public_pem": "", "generated_private_pem": "",
        })
        self.cfg.api_key_id = ""
        d = self._cred_dir()
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
        self.cfg.private_key_path = ""
        log.warning("dashboard: credentials cleared from memory and disk")

    def _build_world(self) -> None:
        cfg = self.cfg
        state_dir = os.path.join(cfg.state_dir, "web", self.mode)
        os.makedirs(state_dir, exist_ok=True)
        pf = Portfolio(cfg.bankroll_cents, state_dir=state_dir, name="web")
        risk = RiskManager(cfg, state_dir=state_dir)
        broker = PaperBroker(cfg)
        if self.mode == "sim":
            seed = random.randrange(1, 10**6)
            source = SimExchange(seed=seed, n_markets=cfg.demo_markets)
            self.display["universe_seed"] = seed
        else:
            source = KalshiClient(cfg, signer=None)   # public market data
            self.display["universe_seed"] = None
        self.sim = source if isinstance(source, SimExchange) else None
        self.portfolio = pf
        self.agent = Agent(cfg, source, pf, broker, risk,
                           start_of_day_equity=float(cfg.bankroll_cents))

    # ---------------------------------------------------- manual betting ---
    def _signed_client(self) -> KalshiClient:
        cred = self.credentials
        base = DEMO_BASE_URL if cred.get("env") == "demo" else DEFAULT_BASE_URL
        probe = AppConfig()
        probe.base_url = base
        probe.rate_limit_per_sec = 4.0
        return KalshiClient(probe, signer=KalshiSigner(cred["key_id"], cred["pem"].encode("utf-8")))

    def funds_snapshot(self, force: bool = False) -> dict:
        """Signed Kalshi balance for the TOTAL FUNDS card. Cached 30 s;
        force=True re-pulls (used immediately after a bet is placed)."""
        cred = self.credentials
        if not cred.get("validated"):
            return {"cents": None, "as_of": 0.0, "env": "",
                    "error": "save + validate credentials first (API keys link)"}
        c = self._funds_cache
        now = time.time()
        if not force and c["cents"] is not None and now - c["as_of"] < 30 and c["env"] == cred.get("env"):
            return dict(c)
        try:
            bal = self._signed_client().get_balance()
            cents = int(bal.get("balance") or 0)
            self._funds_cache = {"cents": cents, "as_of": now,
                                 "env": cred.get("env") or "live", "error": ""}
        except KalshiApiError as exc:
            hint = " — re-validate credentials" if exc.status in (401, 403) else ""
            self._funds_cache = {"cents": None, "as_of": now,
                                 "env": cred.get("env") or "live",
                                 "error": f"balance fetch failed ({exc.status}){hint}"}
        except Exception as exc:  # noqa: BLE001
            self._funds_cache = {"cents": None, "as_of": now,
                                 "env": cred.get("env") or "live",
                                 "error": f"balance fetch failed: {exc}"}
        return dict(self._funds_cache)

    def _sweep_pending(self) -> None:
        now = time.time()
        for tok in [t for t, b in self._pending_bets.items() if now - b["created"] > b["ttl_s"]]:
            del self._pending_bets[tok]

    def _positions_open(self, client: KalshiClient) -> list[dict]:
        try:
            return client.get_positions("open") or []
        except Exception as exc:  # noqa: BLE001
            log.warning("bet: positions fetch failed: %s", exc)
            return []

    def _halt_state(self) -> tuple[bool, str]:
        return webbets.live_halt_state(os.path.join(self.cfg.state_dir, "live", "risk_state.json"))

    def stage_buy(self, *, ticker: str, side: str, count: int,
                  price_cents: int | None, rationale: str = "") -> dict:
        """Validate a proposed BUY against live exchange state + risk caps."""
        return self.stage_order("buy", ticker=ticker, side=side, count=count,
                                price_cents=price_cents, rationale=rationale)

    def stage_sell(self, *, ticker: str, side: str, count: int,
                   price_cents: int | None, rationale: str = "") -> dict:
        """Validate a proposed SELL (closing only what you hold; never a short).
        Exits are permitted even under a risk halt."""
        return self.stage_order("sell", ticker=ticker, side=side, count=count,
                                price_cents=price_cents, rationale=rationale)

    def stage_order(self, action: str, *, ticker: str, side: str, count: int,
                    price_cents: int | None, rationale: str = "") -> dict:
        """Validate a proposed order against live exchange state + caps.
        Returns {"ok": True, "bet": {...}} or {"ok": False, "error": str, ...}."""
        action = "sell" if action == "sell" else "buy"
        cred = self.credentials
        if not cred.get("validated"):
            return {"ok": False, "error": "save + validate credentials first"}
        client = self._signed_client()
        # market must exist and be tradeable
        try:
            market = client.get_market(ticker)
        except KalshiApiError as exc:
            return {"ok": False, "error": f"market {ticker} not found / not accessible ({exc.status})"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"market lookup failed: {exc}"}
        status = str(market.get("status") or "").lower()
        if status and status not in ("open", "active"):
            return {"ok": False, "error": f"{ticker} is '{status}' — only open markets can be traded"}
        title = str(market.get("title") or "")
        # resolve price: explicit, else best ask (buy) / best bid (sell)
        if price_cents is None:
            try:
                book = OrderBook.from_api(client.get_orderbook(ticker), ticker)
                touch = book.best_ask(side) if action == "buy" else book.best_bid(side)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"couldn't read the order book: {exc}"}
            if touch is None:
                return {"ok": False,
                        "error": (f"no one is selling {side.upper()} on {ticker} right now"
                                  if action == "buy"
                                  else f"no one is buying {side.upper()} on {ticker} right now")}
            price_cents = touch.price_cents
        fee = webbets.estimate_fee_cents(count, price_cents, self._live_cfg.taker_fee_multiplier)
        spend = count * price_cents + fee if action == "buy" else count * price_cents - fee
        try:
            bal = self._signed_client().get_balance()
            balance_cents = int(bal.get("balance") or 0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"couldn't fetch your balance: {exc}"}
        positions = self._positions_open(client)
        halted, halt_reason = self._halt_state()
        if action == "buy":
            assessment = webbets.assess_buy(
                self._live_cfg, balance_cents=balance_cents, count=count,
                price_cents=price_cents, ticker=ticker, positions=positions,
                halted=halted, halt_reason=halt_reason)
        else:
            assessment = webbets.assess_sell(
                self._live_cfg, balance_cents=balance_cents, count=count,
                price_cents=price_cents, ticker=ticker, side=side, positions=positions,
                halted=halted, halt_reason=halt_reason)
        bet = {
            "action": action,
            "ticker": ticker, "title": title, "side": side, "count": count,
            "price_cents": price_cents, "fee_cents": fee, "cost_cents": spend,
            "balance_before_cents": balance_cents, "rationale": rationale,
            "env": cred.get("env") or "live", "checks": assessment["checks"],
            "checks_ok": assessment["ok"],
            "max_contracts": self._live_cfg.max_contracts_per_order,
            "created": time.time(),
        }
        if action == "sell":
            bet["proceeds_cents"] = assessment.get("proceeds_cents", spend)
            bet["held"] = assessment.get("held", 0)
        if not assessment["ok"]:
            return {"ok": False, "error": "risk caps blocked this bet — see the checks below",
                    "bet": bet}
        return {"ok": True, "bet": bet}

    def stage_pending(self, bet: dict, ttl_s: float = 120.0) -> str:
        self._sweep_pending()
        if len(self._pending_bets) >= 8:            # never let the dict grow unbounded
            oldest = min(self._pending_bets, key=lambda t: self._pending_bets[t]["created"])
            del self._pending_bets[oldest]
        token = secrets.token_hex(12)
        bet = dict(bet)
        bet["created"] = time.time()
        bet["ttl_s"] = ttl_s
        self._pending_bets[token] = bet
        return token

    def pending_get(self, token: str) -> dict | None:
        self._sweep_pending()
        bet = self._pending_bets.get(token)
        return dict(bet) if bet else None

    def pending_pop(self, token: str) -> dict | None:
        bet = self._pending_bets.pop(token, None)
        return dict(bet) if bet else None

    def confirm_pending(self, token: str) -> dict:
        """Re-validate a staged bet from scratch, then place it. Single-use."""
        bet = self.pending_pop(token)
        if bet is None:
            return {"ok": False, "error": "that staged bet expired or was already used (2 min limit) — ask again"}
        # re-check EVERYTHING against fresh exchange state
        staged = self.stage_order(bet.get("action", "buy"),
                                  ticker=bet["ticker"], side=bet["side"],
                                  count=bet["count"], price_cents=bet["price_cents"],
                                  rationale=bet.get("rationale", ""))
        if not staged["ok"]:
            return {"ok": False,
                    "error": "refused at send time — " + str(staged.get("error", "risk caps no longer pass")),
                    "bet": staged.get("bet")}
        fresh = staged["bet"]
        cfg = self._live_cfg
        from .models import Intent
        intent = Intent(ticker=fresh["ticker"], side=fresh["side"],
                        action=fresh.get("action", "buy"),
                        count=fresh["count"], price_cents=fresh["price_cents"],
                        rationale=fresh.get("rationale", "") or "manual prompt-box bet")
        tif_map = {"ioc": "immediate_or_cancel", "gtc": "good_till_canceled",
                   "fok": "fill_or_kill"}
        body: dict = {
            "ticker": intent.ticker,
            "type": "limit",
            "action": intent.action,
            "side": intent.side,
            "count": intent.count,
            "time_in_force": tif_map.get(cfg.order_type, "immediate_or_cancel"),
            "client_order_id": intent.client_order_id(),
        }
        if intent.side == "yes":
            body["yes_price"] = intent.price_cents
        else:
            body["no_price"] = intent.price_cents
        env = self.credentials.get("env") or "live"
        log.warning("MANUAL BET %s (%s): %s", fresh.get("action", "buy").upper(), env.upper(), body)
        client = self._signed_client()
        try:
            resp = client.create_order(body)
        except Exception as exc:  # noqa: BLE001
            log.exception("manual bet rejected")
            return {"ok": False, "error": f"Kalshi rejected the order: {exc}", "bet": fresh}
        order = resp.get("order", {}) or {}
        try:
            filled = int(order.get("taker_fill_count") or order.get("filled_count") or 0)
        except (TypeError, ValueError):
            filled = 0
        # refresh TOTAL FUNDS immediately after any placed bet
        funds = self.funds_snapshot(force=True)
        return {
            "ok": True,
            "bet": fresh,
            "filled": filled,
            "order_id": str(order.get("order_id") or ""),
            "funds": funds,
            "env": env,
        }

    # ------------------------------------------------------------ control ---
    def poll(self) -> float:
        return self.cfg.web_sim_poll_seconds if self.mode == "sim" \
            else self.cfg.web_paper_poll_seconds

    def start(self) -> None:
        if self.mode == "account":
            log.warning("account mode: no agent loop — real money moves only via the prompt box")
            return
        with self._control_lock:
            if self._worker and self._worker.is_alive():
                return
            self._stop_event.clear()
            self.display["running"] = True
            if self.display["started_ts"] is None:
                self.display["started_ts"] = time.time()
            self._worker = threading.Thread(target=self._loop, name="agent-loop", daemon=True)
            self._worker.start()
            log.info("dashboard: agent started (%s mode)", self.mode)

    def pause(self) -> None:
        with self._control_lock:
            self._stop_event.set()
            w = self._worker
            if w and w.is_alive():
                w.join(timeout=self.poll() + 30)
            self._worker = None
            self.display["running"] = False
            log.info("dashboard: agent paused")

    def reset(self) -> None:
        with self._control_lock:
            was_running = bool(self.display["running"])
            self.pause()
            web_state = os.path.join(self.cfg.state_dir, "web")
            _wipe_web_state(web_state)   # preserves saved credentials
            self.display.update({
                "cycles": 0, "started_ts": None, "last_cycle_ts": None,
                "last_error": "", "positions": [], "closed": [], "signals": [],
                "equity_history": [], "last_res": {}, "risk": {},
                "equity_cents": float(self.cfg.bankroll_cents),
                "cash_cents": float(self.cfg.bankroll_cents), "return_pct": 0.0,
            })
            self._build_world()
            log.warning("dashboard: state reset (fresh %s universe)", self.mode)
            if was_running:
                self.start()

    def set_mode(self, mode: str) -> None:
        with self._control_lock:
            mode = mode if mode in ("sim", "paper") else "sim"
            if mode == self.mode:
                return
            was_running = bool(self.display["running"])
            self.pause()
            self.mode = mode
            self.display["mode"] = mode
            web_state = os.path.join(self.cfg.state_dir, "web")
            _wipe_web_state(web_state)   # preserves saved credentials
            self.display.update({
                "cycles": 0, "started_ts": None, "last_cycle_ts": None,
                "last_error": "", "positions": [], "closed": [], "signals": [],
                "equity_history": [], "last_res": {}, "risk": {},
                "equity_cents": float(self.cfg.bankroll_cents),
                "cash_cents": float(self.cfg.bankroll_cents), "return_pct": 0.0,
            })
            self._build_world()
            log.warning("dashboard: switched to %s mode", mode)
            if was_running:
                self.start()

    # -------------------------------------------------------------- loop ---
    def _reload_credentials_if_changed(self) -> None:
        """Pick up credentials installed externally (CLI / chat upload)."""
        import hashlib

        pem_path = os.path.join(self._cred_dir(), "kalshi_private_key.pem")
        try:
            with open(pem_path, "r", encoding="utf-8") as fh:
                fp = hashlib.sha256(fh.read().encode("utf-8")).hexdigest()[:10]
        except OSError:
            return
        if fp != self.credentials.get("fingerprint"):
            log.warning("dashboard: new credentials detected on disk — reloading")
            self._load_persisted_credentials()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            err = ""
            res = None
            try:
                if self.mode == "sim" and self.sim is not None:
                    self.sim.step(self.cfg.demo_hours_per_cycle)
                self._reload_credentials_if_changed()
                res = self.agent.cycle()
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                log.exception("cycle failed")
            self._update_display(res, err)
            self._stop_event.wait(self.poll())

    def _update_display(self, res, err: str) -> None:
        pf, agent = self.portfolio, self.agent
        eq = pf.equity_cents(agent.last_marks)
        positions = []
        for t, p in sorted(pf.positions.items()):
            mark = agent.last_marks.get(t, 50)
            positions.append({
                "ticker": t, "yes": p.yes_count, "no": p.no_count,
                "avg_yes": p.avg_yes_price_cents, "avg_no": p.avg_no_price_cents,
                "basis_cents": p.basis_cents(), "mark": mark,
                "value_cents": p.value_cents(mark),
            })
        closed = [{
            "ticker": t.ticker, "side": t.side, "count": t.count,
            "entry": t.entry_price_cents, "exit": t.exit_price_cents,
            "pnl_cents": t.pnl_cents, "why": t.why or t.kind,
        } for t in reversed(pf.closed[-25:])]
        signals = list(getattr(res, "signals_digest", [])) if res else []
        with self._display_lock:
            d = self.display
            d.update({
                "last_cycle_ts": time.time(),
                "last_error": err,
                "equity_cents": eq,
                "cash_cents": pf.cash_cents,
                "return_pct": (eq / pf.starting_cash_cents - 1) * 100,
                "positions": positions,
                "closed": closed,
                "risk": agent.risk.summary(),
                "signals": signals,
                "equity_history": [c for _, c in pf.equity_history[-500:]],
            })
            if res:
                d["cycles"] += 1
                d["last_res"] = {
                    "scanned": res.scanned, "evaluated": res.evaluated,
                    "entries": res.entries, "exits": res.exits,
                    "settled": res.settled, "blocked": res.blocked_by_risk,
                    "fills": res.fills,
                }

    def _account_state(self, force: bool = False) -> dict:
        """Live positions + recent fills straight from Kalshi (cached 20 s)."""
        c = self._account_cache
        now = time.time()
        if not force and c["positions"] is not None and now - c["ts"] < 20:
            return dict(c)
        out = {"ts": now, "positions": [], "fills": [], "error": ""}
        cred = self.credentials
        if not cred.get("validated"):
            out["error"] = "save + validate credentials first"
            self._account_cache = out
            return out
        try:
            client = self._signed_client()
            raw_positions = client.get_positions("open") or []
            positions = []
            exposure_cents = 0
            for rp in raw_positions:
                net = webbets._fp(rp.get("position_fp") or rp.get("position") or 0)
                if abs(net) < 1e-9:
                    continue
                exp = webbets.exposure_cents(rp)
                exposure_cents += exp
                positions.append({
                    "ticker": str(rp.get("ticker") or rp.get("market_ticker") or "?"),
                    "side": "YES" if net > 0 else "NO",
                    "count": int(abs(net)),
                    "exposure_cents": exp,
                    "realized_pnl_cents": None,
                })
            out["positions"] = positions
            out["exposure_cents"] = exposure_cents
            try:
                raw_fills = client.get_fills(limit=8) or []
                fills = []
                for fr in raw_fills:
                    cnt = _fp_count(fr.get("count_fp"), fr.get("count"), fr.get("quantity"))
                    if cnt <= 0:
                        continue
                    fills.append({
                        "ticker": str(fr.get("ticker") or "?"),
                        "action": str(fr.get("action") or "").lower(),
                        "side": str(fr.get("side") or "").lower(),
                        "count": cnt,
                        "price_cents": _fp_price_cents(fr, 0),
                        "fee_cents": _fp_fee_cents(fr),
                        "ts": fr.get("created_time") or fr.get("ts") or fr.get("timestamp") or "",
                    })
                out["fills"] = fills
            except Exception as exc:  # noqa: BLE001
                log.warning("account: fills fetch failed: %s", exc)
                out["fills"] = []
        except KalshiApiError as exc:
            hint = " — re-validate credentials" if exc.status in (401, 403) else ""
            out["error"] = f"positions fetch failed ({exc.status}){hint}"
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"positions fetch failed: {exc}"
        self._account_cache = out
        return dict(out)

    # ---------------------------------------------------- hunt window ----
    def hunt_remaining(self) -> float:
        return max(0.0, getattr(self, "hunt_until", 0.0) - time.time())

    def hunt_active(self) -> bool:
        return self.hunt_remaining() > 0

    def set_hunt(self, minutes: float) -> None:
        self.hunt_until = time.time() + max(0.0, minutes) * 60.0
        try:
            _atomic_write(os.path.join(self.cfg.state_dir, "web", "hunt.json"),
                          json.dumps({"until": self.hunt_until}))
        except Exception:  # noqa: BLE001
            pass                      # in-memory is the source of truth; persistence is best-effort

    def stop_hunt(self) -> None:
        self.hunt_until = 0.0
        try:
            p = os.path.join(self.cfg.state_dir, "web", "hunt.json")
            if os.path.exists(p):
                os.remove(p)
        except Exception:  # noqa: BLE001
            pass

    def _load_hunt(self) -> None:
        self.hunt_until = 0.0
        try:
            with open(os.path.join(self.cfg.state_dir, "web", "hunt.json")) as fh:
                self.hunt_until = float(json.load(fh).get("until", 0.0))
        except Exception:  # noqa: BLE001
            self.hunt_until = 0.0

    def chat(self, role: str, text: str) -> None:
        self._chat.append((role, str(text)))
        self._chat = self._chat[-40:]

    def state(self) -> dict:
        if self.mode == "account":
            d = dict(self.display)
            d["running"] = False
            d["now"] = time.time()
            d["logs"] = list(self.log_handler.ring)[-40:]
            d["funds"] = self.funds_snapshot()
            d["account"] = self._account_state()
            d["chat"] = list(self._chat)
            d["mode"] = "account"
            return d
        with self._display_lock:
            d = dict(self.display)
        d["running"] = bool(self._worker and self._worker.is_alive()
                            and not self._stop_event.is_set())
        d["now"] = time.time()
        d["logs"] = list(self.log_handler.ring)[-40:]
        d["funds"] = self.funds_snapshot()
        return d

    # ------------------------------------------------------------ rendering ---
    def render_page(self) -> str:
        if self.mode == "account":
            return render_account_page(self)
        s = self.state()
        eq = s["equity_cents"] / 100
        ret = s["return_pct"]
        pos_count = len(s["positions"])
        closed_n = len(self.portfolio.closed) if self.portfolio else 0
        wins = sum(1 for t in (self.portfolio.closed if self.portfolio else []) if t.pnl_cents > 0)
        risk = s["risk"]
        halted = bool(risk.get("halted"))
        last_res = s.get("last_res") or {}
        age = f"{s['now'] - s['last_cycle_ts']:.0f}s ago" if s["last_cycle_ts"] else "never"
        err = s["last_error"]

        funds = s.get("funds") or {}
        base = _base()
        status_txt, status_color = (
            ("RUNNING", "#22c55e") if s["running"] else ("PAUSED", "#eab308"))
        if halted:
            status_txt, status_color = "RISK HALT", "#ef4444"
        if err:
            status_txt, status_color = "ERROR", "#ef4444"

        mode_badge = ("SIMULATED EXCHANGE" if self.mode == "sim"
                      else "PAPER · REAL KALSHI DATA")

        btn_start = f'''<form method="post" action="{base}/start" style="display:inline">
<button {"disabled" if s["running"] else ""} style="{_btn_style('#166534', s["running"])}">&#9654; Start agent</button></form>'''
        btn_pause = f'''<form method="post" action="{base}/pause" style="display:inline">
<button {"disabled" if not s["running"] else ""} style="{_btn_style('#7f1d1d', not s["running"])}">&#10074;&#10074; Pause</button></form>'''
        mode_form = f'''<form method="post" action="{base}/mode" style="display:inline">
<select name="mode" style="{_sel_style()}">
<option value="sim" {'selected' if self.mode == 'sim' else ''}>Simulated exchange</option>
<option value="paper" {'selected' if self.mode == 'paper' else ''}>Paper &middot; real Kalshi data</option>
</select>
<button style="{_btn_style('#1e3a8a')}">Switch</button></form>'''
        reset_form = f'''<form method="post" action="{base}/reset" style="display:inline">
<button style="{_btn_style('#374151')}">&#8635; Reset</button></form>'''

        pos_rows = "".join(
            f"<tr><td>{_esc(p['ticker'])}</td><td>{p['yes']}</td><td>{p['no']}</td>"
            f"<td>{p['avg_yes']:.1f}c / {p['avg_no']:.1f}c</td>"
            f"<td>{p['basis_cents'] / 100:.2f}$</td><td>{p['mark']}c</td>"
            f"<td>{p['value_cents'] / 100:.2f}$</td></tr>"
            for p in s["positions"]) or _empty_row(7)

        sig_rows = ""
        for g in s["signals"][:30]:
            if g["tradeable"]:
                st = f"<span style='color:#22c55e'>BUY {g['side'].upper()} {g['size']}@{g['price']}c</span>"
            else:
                st = f"<span style='color:#64748b'>skip: {_esc(g['skip'] or '-')}</span>"
            sig_rows += (f"<tr><td>{_esc(g['ticker'])}</td><td>{g['side'].upper() or '-'}</td>"
                         f"<td>{g['edge']:+.1f}c</td><td>{st}</td></tr>")
        sig_rows = sig_rows or _empty_row(4)

        closed_rows = "".join(
            f"<tr><td>{_esc(t['ticker'])}</td><td>{_esc(t['side'])}</td><td>{t['count']}</td>"
            f"<td>{t['entry']:.1f}c</td><td>{t['exit']:.1f}c</td>"
            f"<td style='color:{'#22c55e' if t['pnl_cents'] >= 0 else '#ef4444'}'>"
            f"{t['pnl_cents'] / 100:+.2f}$</td><td>{_esc(t['why'])}</td></tr>"
            for t in s["closed"]) or _empty_row(7)

        risk_rows = "".join(
            f"<tr><td>{_esc(k)}</td><td><b>{_esc(v)}</b></td></tr>"
            for k, v in risk.items()) or _empty_row(2)

        logs = "\n".join(s["logs"][-25:]) or "(no log lines yet)"
        seed_note = f"universe seed {s['universe_seed']}" if s["universe_seed"] else "live public market data"
        lr = (f"scan {last_res.get('scanned', '-')} &middot; entries {last_res.get('entries', '-')} &middot; "
              f"exits {last_res.get('exits', '-')} &middot; settled {last_res.get('settled', '-')} &middot; "
              f"risk-blocked {last_res.get('blocked', '-')}")
        err_html = (f"<div style='background:#450a0a;border:1px solid #b91c1c;border-radius:8px;"
                    f"padding:10px 14px;margin-bottom:14px;color:#fca5a5;'>last error: {_esc(err)}</div>"
                    if err else "")
        svg = sparkline_svg(s["equity_history"][-300:] or [eq * 100])

        return f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>Kalshi Agent — Live Dashboard</title></head>
<body style="margin:0;background:#0b1220;color:#e2e8f0;font-family:ui-monospace,Menlo,Consolas,monospace;">
<div style="max-width:960px;margin:0 auto;padding:20px;">

<div style="display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:6px;">
<h1 style="font-size:22px;margin:0;">Kalshi Agent <span style="color:#64748b;font-weight:normal;">— live dashboard</span></h1>
<span style="background:{status_color}22;color:{status_color};border:1px solid {status_color};
padding:3px 10px;border-radius:999px;font-size:12px;">{status_txt}</span>
<span style="background:#1e293b;color:#93c5fd;border:1px solid #334155;padding:3px 10px;border-radius:999px;font-size:12px;">{mode_badge}</span>
</div>
<div style="color:#64748b;font-size:12px;margin-bottom:14px;">
{seed_note} &middot; cycles run {s['cycles']} &middot; last cycle {age} &middot; {lr}<br>
page auto-refreshes every 5s &middot; real-money live trading is CLI-gated (see README) &middot;
<a href="{base}/credentials" style="color:#93c5fd;">&#128273; API keys &amp; credentials</a></div>

<div style="margin-bottom:16px;">
{btn_start} {btn_pause} {reset_form} {mode_form} <a href="{base}/credentials" style="color:#93c5fd;font-size:13px;margin-left:8px;">API keys</a><form method="post" action="/logout" style="display:inline"><button style="background:none;border:none;color:#64748b;font-size:13px;cursor:pointer;margin-left:10px;text-decoration:underline;font-family:inherit;">sign out</button></form></div>

{err_html}

<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px;">
{_funds_card(funds)}
{_card('EQUITY', f"{eq:,.2f}$", f"{ret:+.2f}% return", '#22c55e' if ret >= 0 else '#ef4444')}
{_card('CASH', f"{s['cash_cents'] / 100:,.2f}$", 'free to deploy')}
{_card('OPEN POSITIONS', f"{pos_count}", f"max {self.cfg.max_open_positions}")}
{_card('CLOSED TRADES', f"{closed_n}", f"win rate {wins}/{closed_n or 0}")}
{_card('RISK ENGINE', 'HALT' if halted else 'OK',
       f"streak {risk.get('consecutive_losses', 0)}/{self.cfg.consecutive_loss_kill}",
       '#ef4444' if halted else '#22c55e')}
</div>

<div style="margin-bottom:16px;">{svg}</div>

{_bet_panel(self, funds, base)}

{_panel('LAST SCAN — SIGNALS (' + str(len(s['signals'])) + ' markets evaluated)', '''
<table style="width:100%;border-collapse:collapse;font-size:13px;">
<tr style="color:#64748b;text-align:left;"><th>ticker</th><th>side</th><th>edge</th><th>action</th></tr>
''' + sig_rows + "</table>")}

{_panel(f'OPEN POSITIONS ({pos_count})', '''
<table style="width:100%;border-collapse:collapse;font-size:13px;">
<tr style="color:#64748b;text-align:left;"><th>ticker</th><th>yes</th><th>no</th><th>avg Y/N</th><th>basis</th><th>mark</th><th>value</th></tr>
''' + pos_rows + "</table>")}

{_panel('RECENT CLOSED TRADES', '''
<table style="width:100%;border-collapse:collapse;font-size:13px;">
<tr style="color:#64748b;text-align:left;"><th>ticker</th><th>side</th><th>n</th><th>entry</th><th>exit</th><th>pnl</th><th>why</th></tr>
''' + closed_rows + "</table>")}

{_panel('RISK STATE', '''
<table style="width:100%;border-collapse:collapse;font-size:13px;">
''' + risk_rows + "</table>")}

{_panel('LIVE LOG', f"<pre style='margin:0;white-space:pre-wrap;font-size:11px;color:#94a3b8;'>{_esc(logs)}</pre>")}

<div style="color:#475569;font-size:11px;margin-top:18px;">
Simulated/paper results do not guarantee real-world performance. Not financial advice.
Real-money trading requires credentials + config + --live + KALSHI_LIVE_CONFIRM=YES on the CLI.</div>
</div></body></html>"""


def install_credentials(cfg: AppConfig, key_id: str, pem: str, env: str,
                        persist: bool = True, validator=None) -> tuple[bool, str]:
    """Validate + store credentials to the dashboard's credential dir, from
    outside the web app (CLI / chat-uploaded files).  Returns (ok, message)."""
    import hashlib

    key_id, pem, env = key_id.strip(), pem.strip(), ("demo" if env == "demo" else "live")
    if not key_id:
        return False, "API key id is required"
    if "PRIVATE KEY" not in pem:
        return False, "not a PEM private key"
    base = DEMO_BASE_URL if env == "demo" else DEFAULT_BASE_URL
    try:
        signer = KalshiSigner(key_id, pem.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"could not parse private key: {exc}"

    balance = None
    if validator is not None:
        ok, msg = validator(key_id, pem, base)
        if not ok:
            return False, msg
    else:
        probe_cfg = AppConfig()
        probe_cfg.base_url = base
        probe_cfg.rate_limit_per_sec = 4.0
        client = KalshiClient(probe_cfg, signer=signer)
        try:
            balance = client.get_balance().get("balance")
        except KalshiApiError as exc:
            return False, f"Kalshi rejected the credentials ({exc.status}): {exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"network error contacting {base}: {exc}"

    if persist:
        d = os.path.join(cfg.state_dir, "web", "credentials")
        os.makedirs(d, exist_ok=True)
        pem_path = os.path.join(d, "kalshi_private_key.pem")
        _atomic_write(pem_path, pem, mode=0o600)
        _atomic_write(os.path.join(d, "meta.json"),
                      json.dumps({"key_id": key_id, "env": env}))
    fp = hashlib.sha256(pem.encode("utf-8")).hexdigest()[:10]
    bal_txt = f", balance {balance / 100:.2f}$" if balance is not None else ""
    return True, f"validated ({env}) fingerprint {fp}…{bal_txt}"


def _mask(key_id: str) -> str:
    return f"{key_id[:6]}…{key_id[-4:]}" if len(key_id) > 12 else (key_id or "(not set)")


def _cred_status_block(cred: dict, cfg: AppConfig) -> str:
    if cred["key_id"] and cred["validated"]:
        bal = (f"{cred['balance_cents'] / 100:,.2f}$"
               if cred["balance_cents"] is not None else "n/a")
        env_label = "demo (fake money)" if cred["env"] == "demo" else "LIVE PRODUCTION"
        color = "#22c55e" if cred["env"] == "demo" else "#f59e0b"
        return (f"<tr><td>status</td><td><b style='color:#22c55e;'>VALIDATED</b></td></tr>"
                f"<tr><td>key id</td><td>{_esc(_mask(cred['key_id']))}</td></tr>"
                f"<tr><td>key fingerprint</td><td>{_esc(cred['fingerprint'])}…</td></tr>"
                f"<tr><td>environment</td><td style='color:{color};'>{env_label}</td></tr>"
                f"<tr><td>validated balance</td><td>{bal}</td></tr>")
    if cred["key_id"]:
        return (f"<tr><td>status</td><td><b style='color:#eab308;'>saved but not yet validated</b></td></tr>"
                f"<tr><td>key id</td><td>{_esc(_mask(cred['key_id']))}</td></tr>")
    return ("<tr><td>status</td><td style='color:#64748b;'><i>no credentials configured</i>"
            " (sim & paper modes work without them)</td></tr>")


def render_credentials(self: "WebDashboard") -> str:
    base = _base()
    cred = self.credentials
    gen_pub, gen_priv = cred["generated_public_pem"], cred["generated_private_pem"]

    result = ""
    if cred["error"]:
        result = (f"<div style='background:#450a0a;border:1px solid #b91c1c;border-radius:8px;"
                  f"padding:10px 14px;margin-bottom:14px;color:#fca5a5;'>{_esc(cred['error'])}</div>")

    step1_html = _panel("STEP 1 — GET A KEY (pick one)", (
        "<table style='width:100%;border-collapse:collapse;font-size:13px;'><tr><td>"
        f"<form method=\"post\" action=\"{base}/credentials/generate\" style=\"margin:0;\">"
        f"<button style=\"{_btn_style('#1e3a8a')}\">&#10024; Generate a new keypair here (recommended)</button>"
        "</form>"
        "<div style=\"color:#64748b;margin-top:6px;\">…or use an existing key: create one at "
        "kalshi.com &rarr; Settings &rarr; API Keys, then paste the private key below.</div>"
        "</td></tr></table>"))

    gen_html = ""
    if gen_pub:
        gen_html = _panel("GENERATED KEYPAIR — upload the PUBLIC key to Kalshi", (
            "<div style=\"font-size:13px;margin-bottom:10px;\">"
            "1. Copy the <b>public</b> key below &rarr; open <a href=\"https://kalshi.com\" "
            "target=\"_blank\" rel=\"noopener\" style=\"color:#93c5fd;\">kalshi.com</a> &rarr; "
            "Settings &rarr; API Keys &rarr; add key &rarr; paste.<br>"
            "2. Copy the <b>private</b> key somewhere safe NOW (download button below) — it never "
            "leaves this app except to sign requests.<br>"
            "3. Come back and save the <b>key id</b> Kalshi shows you, using the form below.</div>"
            + _panel("PUBLIC KEY (safe to upload)",
                     f"<pre style='white-space:pre-wrap;font-size:11px;color:#86efac;margin:0;'>{_esc(gen_pub)}</pre>")
            + _panel("PRIVATE KEY (secret — store it, do not share)",
                     f"<pre style='white-space:pre-wrap;font-size:11px;color:#fca5a5;margin:0;'>{_esc(gen_priv)}</pre>")
            + (f"<a href=\"{base}/credentials/private.pem\" download=\"kalshi_private_key.pem\" "
               "style=\"color:#93c5fd;font-size:13px;\">&#11015; download private key (.pem)</a>")))

    sel_demo = "selected" if cred.get("env") != "live" else ""
    sel_live = "selected" if cred.get("env") == "live" else ""
    pem_value = _esc(gen_priv) if gen_priv else _esc(cred["pem"])
    save_html = _panel("STEP 2 — SAVE &amp; TEST CREDENTIALS", (
        f"<form method=\"post\" action=\"{base}/credentials/save\">"
        "<div style=\"margin-bottom:10px;\">"
        "<label style=\"color:#64748b;font-size:11px;\">KALSHI API KEY ID</label><br>"
        f"<input name=\"key_id\" value=\"{_esc(cred['key_id'])}\" "
        "placeholder=\"e.g. a952bcbe-ec3b-4b5b-8f9a-…\" style=\"" + _input_style() + "\"></div>"
        "<div style=\"margin-bottom:10px;\">"
        "<label style=\"color:#64748b;font-size:11px;\">PRIVATE KEY (PEM)</label><br>"
        f"<textarea name=\"pem\" rows=\"8\" placeholder=\"-----BEGIN PRIVATE KEY-----&#10;…&#10;-----END PRIVATE KEY-----\" "
        "style=\"" + _input_style() + "\">" + pem_value + "</textarea></div>"
        "<div style=\"margin-bottom:10px;\">"
        "<label style=\"color:#64748b;font-size:11px;\">ENVIRONMENT</label><br>"
        f"<select name=\"env\" style=\"{_sel_style()}\">"
        f"<option value=\"demo\" {sel_demo}>demo-api.kalshi.co — fake money</option>"
        f"<option value=\"live\" {sel_live}>api.elections.kalshi.com — REAL account</option>"
        "</select></div>"
        "<label style=\"font-size:12px;color:#94a3b8;\"><input type=\"checkbox\" name=\"persist\" value=\"1\"> "
        "remember on disk (chmod 600) — off = memory only</label><br><br>"
        f"<button style=\"{_btn_style('#166534')}\">&#128274; Save &amp; test with a signed API call</button>"
        "</form>"))

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Kalshi Agent — API Credentials</title></head>
<body style="margin:0;background:#0b1220;color:#e2e8f0;font-family:ui-monospace,Menlo,Consolas,monospace;">
<div style="max-width:860px;margin:0 auto;padding:20px;">
<h1 style="font-size:22px;margin:0 0 4px;">&#128273; API Credentials</h1>
<div style="color:#64748b;font-size:12px;margin-bottom:14px;">
Used to sign Kalshi Trade API requests (RSA-PSS). Sim &amp; paper modes on the
<a href="{base}/dashboard" style="color:#93c5fd;">dashboard</a> work without any credentials.
<a href="{base}/dashboard" style="color:#93c5fd;">&larr; back to dashboard</a> &middot; <form method="post" action="/logout" style="display:inline"><button style="background:none;border:none;color:#64748b;font-size:12px;cursor:pointer;text-decoration:underline;font-family:inherit;padding:0;">sign out</button></form></div>

<div style="background:#1e293b;border:1px solid #f59e0b;border-radius:10px;padding:12px 14px;margin-bottom:16px;font-size:13px;color:#fcd34d;">
<b>Handle with care:</b> this dashboard is password-protected. Prefer
<b>memory-only</b> storage for keys, and use <i>Clear</i> when done.
Keys are only ever sent to Kalshi's API for request signing — nothing else.</div>

{result}

{_panel('CURRENT STATUS', "<table style='width:100%;border-collapse:collapse;font-size:13px;'>" + _cred_status_block(cred, self.cfg) + "</table>")}

{step1_html}

{gen_html}

{save_html}

<div style="background:#101a2e;border:1px solid #1e293b;border-radius:10px;padding:14px;margin-bottom:16px;">
<form method="post" action="{base}/credentials/clear" style="margin:0;">
<button style="{_btn_style('#7f1d1d')}">&#128465;&#65039; Clear credentials (memory + disk)</button></form></div>

<div style="color:#475569;font-size:11px;">
Validation = a real signed GET /portfolio/balance against the chosen environment.
Real-money order placement is never exposed on the web — it stays behind the CLI
gates (config + --live + KALSHI_LIVE_CONFIRM=YES + --no-dry-run).</div>
</div></body></html>"""


def _atomic_write(path: str, text: str, mode: int | None = None) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def render_login(error: bool = False) -> str:
    err_html = ("<div style='background:#450a0a;border:1px solid #b91c1c;border-radius:8px;"
                "padding:10px 14px;margin-bottom:14px;color:#fca5a5;font-size:13px;'>"
                "Wrong password — try again (capitalization matters).</div>" if error else "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kalshi Agent — Sign in</title></head>
<body style="margin:0;background:#0b1220;color:#e2e8f0;font-family:ui-monospace,Menlo,Consolas,monospace;">
<div style="max-width:400px;margin:12vh auto 0;padding:24px;background:#101a2e;border:1px solid #1e293b;border-radius:14px;">
<div style="font-size:22px;margin-bottom:2px;">&#128274; Kalshi Agent</div>
<div style="color:#64748b;font-size:12px;margin-bottom:18px;">enter the app password to continue</div>
{err_html}
<form method="post" action="/login">
<input type="password" name="password" placeholder="password" autofocus autocomplete="current-password"
 style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:12px;font-size:16px;width:100%;box-sizing:border-box;font-family:inherit;margin-bottom:12px;">
<button style="background:#1e3a8a;color:#fff;border:1px solid #334155;border-radius:8px;padding:12px;font-size:15px;width:100%;cursor:pointer;font-family:inherit;">Unlock</button>
</form>
</div></body></html>"""


def _input_style() -> str:
    return ("background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:8px;"
            "padding:8px;font-size:13px;font-family:inherit;width:100%;box-sizing:border-box;")


def _card(label: str, value: str, sub: str, color: str = "#e2e8f0") -> str:
    return (f"<div style='flex:1;min-width:150px;background:#101a2e;border:1px solid #1e293b;"
            f"border-radius:10px;padding:12px 14px;'>"
            f"<div style='color:#64748b;font-size:11px;'>{label}</div>"
            f"<div style='font-size:22px;color:{color};'>{value}</div>"
            f"<div style='font-size:12px;color:#64748b;'>{sub}</div></div>")


def _panel(title: str, inner: str) -> str:
    return (f"<div style='background:#101a2e;border:1px solid #1e293b;border-radius:10px;"
            f"padding:14px;margin-bottom:16px;'>"
            f"<div style='color:#64748b;font-size:11px;margin-bottom:8px;'>{title}</div>{inner}</div>")


def _empty_row(cols: int) -> str:
    return f"<tr><td colspan='{cols}' style='color:#475569;'><i>none yet</i></td></tr>"


def _btn_style(bg: str, disabled: bool = False) -> str:
    base = (f"background:{bg};color:#fff;border:1px solid #334155;border-radius:8px;"
            f"padding:7px 14px;font-size:13px;cursor:pointer;font-family:inherit;"
            f"margin-right:6px;")
    return base + ("opacity:0.4;cursor:not-allowed;" if disabled else "")


def _sel_style() -> str:
    return ("background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:8px;"
            "padding:7px 8px;font-size:13px;font-family:inherit;margin-right:6px;")


YES_WORDS = {"yes", "y", "yeah", "yep", "yup", "affirmative", "confirm",
             "send", "send it", "do it", "approve", "go", "go ahead"}
NO_WORDS = {"no", "n", "nope", "negative", "cancel", "abort", "stop",
            "don't", "dont", "never mind", "nevermind"}

def _fmt_usd(cents) -> str:
    return f"{(cents or 0) / 100:,.2f}$"


def _env_label(env: str) -> tuple[str, str]:
    """(short label, accent color) for the credential environment."""
    return ("DEMO · play money", "#93c5fd") if env == "demo" else ("REAL MONEY", "#f87171")


def _funds_card(funds: dict) -> str:
    env = (funds or {}).get("env") or "live"
    label, accent = _env_label(env)
    cents = (funds or {}).get("cents")
    err = (funds or {}).get("error") or ""
    as_of = (funds or {}).get("as_of") or 0.0
    if cents is not None:
        age = max(0, int(time.time() - as_of))
        return _card(f'TOTAL FUNDS · KALSHI <span style="font-size:10px;color:{accent}">{label}</span>',
                     f"{_fmt_usd(cents)}",
                     f"signed exchange balance · refreshed {age}s ago", '#38bdf8')
    sub = err or "unavailable"
    return _card(f'TOTAL FUNDS · KALSHI <span style="font-size:10px;color:{accent}">{label}</span>',
                 "—", _esc(sub), '#ef4444')


def _bet_form(base: str, prompt_value: str = "") -> str:
    return f'''<form method="post" action="{base}/bet/preview" style="margin:0">
<textarea name="prompt" rows="2" placeholder="buy 1 yes KXNFLGAME-26NOV25-KC 25c  ·  sell 1 yes KXNFLGAME-26NOV25-KC  ·  or plain English: &quot;close out my Chiefs bet&quot;" required
 style="width:100%;box-sizing:border-box;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:10px;font-size:13px;font-family:inherit;resize:vertical;">{_esc(prompt_value)}</textarea>
<div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;gap:10px;flex-wrap:wrap;">
<span style="color:#64748b;font-size:11px;">You see the full order + fees + risk checks BEFORE anything is sent. Nothing is ever sent automatically.</span>
<button style="{_btn_style('#7c3aed')}">Propose bet &#8594;</button>
</div></form>'''


def _bet_panel(dash: "WebDashboard", funds: dict, base: str) -> str:
    cred = dash.credentials
    if not cred.get("validated"):
        return _panel('ASK TO PLACE A BET',
            '<div style="color:#94a3b8;font-size:13px;">Save + validate your Kalshi credentials first '
            f'— then this box can propose real (or demo) bets, sized by your config-live.yaml rules. '
            f'<a href="{base}/credentials" style="color:#93c5fd;">API keys &amp; credentials</a></div>')
    env = cred.get("env") or "live"
    label, accent = _env_label(env)
    from . import llm as _llm
    llm = _llm.get_client().describe() + ("" if _llm.get_client().configured else " — ticket syntax works")
    cfg_note = "config-live.yaml caps loaded" if dash._live_cfg_ok else "config-live.yaml NOT found — falling back to built-in defaults"
    caps = (f"max {dash._live_cfg.max_contracts_per_order} contracts/order · price band "
            f"{dash._live_cfg.min_price_cents}–{dash._live_cfg.max_price_cents}c · "
            f"IOC limit · fee-aware · buys + closing sells (never shorts) · exits allowed under halt · 8&cent; model edge gate does NOT apply to manual bets")
    warning = ("<div style='background:#450a0a;border:1px solid #b91c1c;border-radius:8px;padding:8px 12px;"
               "margin-bottom:10px;color:#fca5a5;font-size:12px;'><b>REAL MONEY MODE</b> — confirming a bet "
               "sends an actual order to Kalshi. Double-check the market, price and size.</div>"
               if env == "live" else
               "<div style='background:#082f49;border:1px solid #0ea5e9;border-radius:8px;padding:8px 12px;"
               "margin-bottom:10px;color:#7dd3fc;font-size:12px;'>DEMO mode — orders use play money on the "
               "Kalshi demo exchange. Rehearse here first.</div>")
    return _panel(f'ASK TO PLACE A BET <span style="font-size:10px;color:{accent}">{label}</span>',
                  warning + _bet_form(base) +
                  f'<div style="color:#64748b;font-size:11px;margin-top:8px;">{caps} · {cfg_note} · {_esc(llm)}</div>')


def _bet_checks_rows(checks: list) -> str:
    rows = ""
    for label, ok, detail in checks:
        icon = "&#10003;" if ok else "&#10007;"
        color = "#22c55e" if ok else "#ef4444"
        rows += f'<tr><td style="color:{color};width:24px;">{icon}</td><td>{_esc(str(label))}</td><td style="color:#64748b;">{_esc(str(detail))}</td></tr>'
    return f'<table style="width:100%;border-collapse:collapse;font-size:12px;">{rows}</table>'


_BET_STYLE = "max-width:720px;margin:0 auto;padding:20px;color:#e2e8f0;font-family:ui-monospace,Menlo,Consolas,monospace;"


def _chat_color(role: str, text) -> str:
    """Bubble accent: green affirmatives, red negatives, amber warnings, neutral default."""
    if role == "you":
        return "#93c5fd"
    t = str(text).lstrip()
    if t.startswith(("✅", "🎯")):
        return "#22c55e"      # green affirmative
    if t.startswith(("❌", "✗", "🛑")):
        return "#f87171"      # red negative
    if t.startswith("⚠"):
        return "#eab308"      # amber warning
    return "#c4b5fd"          # neutral agent purple


_HUNT_DECREE_RE = re.compile(
    r"^(?:maximize(?:\s+profits)?|hunt|pinched|trade|snipe)\s*(?:for\s*)?"
    r"(\d+(?:\.\d+)?)\s*(minute|minutes|min|mins|m|hour|hours|hr|hrs|h)\b", re.I)
_HUNT_MAX_MINUTES = 8 * 60   # 8h hard ceiling on a decree


def _parse_hunt_decree(low: str) -> float | None:
    """'maximize profits for 45 minutes' / 'hunt for 2 hours' -> minutes (clamped). None if not a decree."""
    m = _HUNT_DECREE_RE.match((low or "").strip())
    if not m:
        return None
    qty = float(m.group(1))
    unit = m.group(2).lower()
    minutes = qty * 60 if unit.startswith(("hour", "hr", "h")) else qty
    minutes = max(1.0, min(minutes, float(_HUNT_MAX_MINUTES)))
    return minutes


def render_account_page(dash: "WebDashboard") -> str:
    """Real-money console: signed Kalshi funds, live positions/fills, bet chat.
    No sim/paper agent — nothing on this page can trade autonomously."""
    base = _base()
    s = dash.state()
    cred = dash.credentials
    env = cred.get("env") or "live"
    label, accent = _env_label(env)
    funds = s["funds"]
    acct = s.get("account") or {}
    positions = acct.get("positions") or []
    fills = acct.get("fills") or []
    acct_err = acct.get("error") or ""
    exposure = acct.get("exposure_cents")
    if exposure is None:
        exposure = sum(p.get("exposure_cents", 0) for p in positions)

    # ---- transcript ----
    bubbles = []
    for role, text in (s.get("chat") or [])[-24:]:
        who = "YOU" if role == "you" else "AGENT"
        color = _chat_color(role, text)
        bubbles.append(
            '<div style="padding:6px 2px;border-bottom:1px solid #1e293b;">'
            f'<span style="color:{color};font-size:10px;letter-spacing:1px;">{who}</span><br>'
            f'<span style="white-space:pre-wrap;font-size:12px;color:#e2e8f0;">{_esc(text)}</span></div>')
    transcript = "".join(bubbles) or '<div style="color:#64748b;font-size:12px;">…</div>'

    # ---- positions table ----
    if positions:
        pos_rows = "".join(
            f"<tr><td>{_esc(p['ticker'])}</td><td>{_esc(p['side'])}</td>"
            f"<td>{p['count']}</td><td>{_fmt_usd(p['exposure_cents'])}</td></tr>"
            for p in positions)
        pos_html = ('<table style="width:100%;border-collapse:collapse;font-size:13px;">'
                    '<tr style="color:#64748b;text-align:left;"><th>ticker</th><th>side</th>'
                    f'<th>contracts</th><th>exposure</th></tr>{pos_rows}</table>')
        open_n = len(positions)
    else:
        open_n = 0
        pos_html = '<div style="color:#64748b;font-size:13px;">No open positions — nothing at risk.</div>'

    # ---- fills table ----
    if fills:
        fill_rows = "".join(
            f"<tr><td>{_esc(str(f['ts'])[:19].replace('T', ' '))}</td>"
            f"<td>{_esc(str(f['action']).upper())} {_esc(str(f['side']).upper())}</td>"
            f"<td>{_esc(f['ticker'])}</td><td>{f['count']}</td><td>{f['price_cents']}c</td>"
            f"<td>{_fmt_usd(f['fee_cents'])}</td></tr>" for f in fills)
        fills_html = ('<table style="width:100%;border-collapse:collapse;font-size:13px;">'
                      '<tr style="color:#64748b;text-align:left;"><th>time (UTC)</th><th>side</th>'
                      f'<th>ticker</th><th>qty</th><th>price</th><th>fee</th></tr>{fill_rows}</table>')
    else:
        fills_html = '<div style="color:#64748b;font-size:13px;">No recent fills.</div>'

    acct_err_html = (
        '<div style="background:#450a0a;border:1px solid #b91c1c;border-radius:8px;padding:10px 14px;'
        f'margin-bottom:14px;color:#fca5a5;font-size:13px;">account read failed: {_esc(acct_err)}</div>'
        if acct_err else "")

    err = ""
    if not cred.get("validated"):
        err = ('<div style="background:#450a0a;border:1px solid #b91c1c;border-radius:8px;padding:12px 14px;'
               'margin-bottom:14px;color:#fca5a5;font-size:13px;"><b>Credentials not validated on this '
               'deployment.</b><br>Total funds and order placement need your key: '
               f'<a href="{base}/credentials" style="color:#93c5fd;">API keys &amp; credentials</a> — '
               'paste your private PEM → pick env (demo first!) → Save &amp; test. Your PEM goes '
               'straight from this browser to the server over TLS.</div>')

    from . import llm as _llm
    _c = _llm.get_client()
    llm_note = (f"{_c.describe()} for plain-English bets" if _c.configured
                else "ticket syntax only (no AI key on server — add free GROQ_API_KEY or GEMINI_API_KEY)")
    caps = (f"max {dash._live_cfg.max_contracts_per_order} contracts/order · entries "
            f"{dash._live_cfg.min_price_cents}–{dash._live_cfg.max_price_cents}c · "
            "IOC limit · fee-aware · buys + closing sells (never shorts) · exits allowed under halt · "
            "8¢ model edge gate not applied to manual bets · funds re-pulled after every order")

    if funds.get("cents") is not None:
        funds_big = _fmt_usd(funds["cents"])
        funds_sub = f"signed exchange balance · as of {max(0, int(time.time() - funds.get('as_of', 0)))}s ago"
    else:
        funds_big = "—"
        funds_sub = _esc(str(funds.get("error") or "unavailable"))

    if dash.hunt_active():
        whisper = int(dash.hunt_remaining() // 60) + (1 if dash.hunt_remaining() % 60 else 0)
        hunt_banner = f'''<div style="background:#052e16;border:1px solid #16a34a;border-radius:10px;padding:10px 14px;margin-bottom:12px;color:#86efac;font-size:13px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
<div><b>🎯 HUNT ACTIVE — {int(whisper)} minute(s) left</b> · all liquid markets · every pick needs your YES</div>
<form method="post" action="{base}/hunt/stop" style="margin:0"><button style="background:#14532d;color:#86efac;border:1px solid #22c55e;border-radius:8px;padding:6px 14px;font-size:12px;cursor:pointer;font-family:inherit;">STOP HUNT 🛑</button></form>
</div>'''
    else:
        hunt_banner = ""

    chat_panel_inner = (
        f'<div style="background:#020617;border:1px solid #1e293b;border-radius:10px;'
        f'padding:10px;height:260px;overflow-y:auto;margin-bottom:10px;">{transcript}</div>'
        f'<form method="post" action="{base}/bet/chat" style="margin:0">'
        f'<div style="display:flex;gap:8px;">'
        f'<input type="text" name="prompt" autocomplete="off" autofocus required '
        f'placeholder="maximize profits for 45 minutes · buy 1 yes KXRAIN-… 8c · yes / no · funds · help" '
        f'style="flex:1;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:8px;'
        f'padding:10px 12px;font-size:13px;font-family:inherit;">'
        f'<button style="{_btn_style("#7c3aed")}">Send</button>'
        f'</div></form>'
        f'<div style="color:#64748b;font-size:11px;margin-top:8px;">{caps} · {_esc(llm_note)}</div>')

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Kalshi — My Account</title></head>
<body style="margin:0;background:#0b1220;color:#e2e8f0;font-family:ui-monospace,Menlo,Consolas,monospace;">
<div style="max-width:960px;margin:0 auto;padding:20px;">

<div style="display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:4px;">
<h1 style="font-size:22px;margin:0;">Kalshi <span style="color:#64748b;font-weight:normal;">— my account</span></h1>
<span style="background:{accent}22;color:{accent};border:1px solid {accent};
 padding:3px 10px;border-radius:999px;font-size:12px;">{_esc(label)}</span>
<span style="background:#1e293b;color:#fca5a5;border:1px solid #334155;padding:3px 10px;border-radius:999px;font-size:12px;">MANUAL ONLY — NOTHING AUTO-SENDS</span>
</div>
<div style="color:#64748b;font-size:12px;margin-bottom:14px;">
No simulated or paper trading here. Every order needs your explicit YES in the box below.
&middot; <a href="{base}/dashboard" style="color:#93c5fd;">&#8635; refresh</a> &middot;
<a href="{base}/credentials" style="color:#93c5fd;">&#128273; API keys</a> &middot;
<form method="post" action="/logout" style="display:inline"><button style="background:none;border:none;color:#64748b;font-size:12px;cursor:pointer;text-decoration:underline;font-family:inherit;padding:0;">sign out</button></form></div>

{err}{acct_err_html}

<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px;">
<div style="background:#0f172a;border:1px solid #1e293b;border-radius:12px;padding:14px 18px;min-width:220px;">
<div style="color:#64748b;font-size:11px;letter-spacing:1px;">TOTAL FUNDS · KALSHI <span style="color:{accent};">{_esc(label)}</span></div>
<div style="font-size:30px;font-weight:bold;color:#38bdf8;margin:6px 0;">{funds_big}</div>
<div style="color:#64748b;font-size:11px;">{funds_sub}</div>
</div>
{_card('OPEN POSITIONS', str(open_n), f"max {dash._live_cfg.max_open_positions} (config-live.yaml)")}
{_card('TOTAL EXPOSURE', _fmt_usd(exposure), 'cost basis per Kalshi')}
{_card('AUTOMATION', 'OFF', 'every order needs your YES', '#eab308')}
</div>

{hunt_banner}
{_panel('ASK TO PLACE A BET', chat_panel_inner)}

{_panel(f'OPEN POSITIONS ({open_n}) — live from Kalshi', pos_html)}

{_panel('RECENT FILLS', fills_html)}

<div style="color:#475569;font-size:11px;margin-top:18px;">
Simulated/paper trading is not shown on this deployment. Real-money orders go to Kalshi only after you type YES.
IOC limit orders — unfilled remainders are cancelled automatically. Fees are estimates (7% of C×P×(1−P)); the exchange's
ledger is authoritative. Not financial advice. Parameters: exactly those in config-live.yaml.</div>
</div></body></html>"""


def render_bet_confirm(dash: "WebDashboard", bet: dict, token: str, error: str = "") -> str:
    action = "SELL" if bet.get("action") == "sell" else "BUY"
    is_sell = action == "SELL"
    env = bet.get("env") or "live"
    if not is_sell:
        cost_rows = (
            f'<tr><td style="color:#64748b;padding:2px 0;">order cost</td><td><b>{_fmt_usd(bet["count"] * bet["price_cents"])}</b></td></tr>'
            f'<tr><td style="color:#64748b;padding:2px 0;">est. taker fee</td><td>{_fmt_usd(bet["fee_cents"])}</td></tr>'
            f'<tr><td style="color:#64748b;padding:2px 0;">total at risk</td><td><b>{_fmt_usd(bet["cost_cents"])}</b></td></tr>'
            f'<tr><td style="color:#64748b;padding:2px 0;">balance before</td><td>{_fmt_usd(bet["balance_before_cents"])}</td></tr>'
            f'<tr><td style="color:#64748b;padding:2px 0;">balance after (if fully filled)</td><td>{_fmt_usd(bet["balance_before_cents"] - bet["cost_cents"])}</td></tr>'
        )
    else:
        cost_rows = (
            f'<tr><td style="color:#64748b;padding:2px 0;">gross proceeds</td><td><b>{_fmt_usd(bet["count"] * bet["price_cents"])}</b></td></tr>'
            f'<tr><td style="color:#64748b;padding:2px 0;">est. taker fee</td><td>{_fmt_usd(bet["fee_cents"])}</td></tr>'
            f'<tr><td style="color:#64748b;padding:2px 0;">net proceeds</td><td><b>{_fmt_usd(bet.get("proceeds_cents", 0))}</b></td></tr>'
            f'<tr><td style="color:#64748b;padding:2px 0;">held before sale</td><td>{bet.get("held", "?")} &times; {_esc(bet["side"].upper())}</td></tr>'
            f'<tr><td style="color:#64748b;padding:2px 0;">balance before</td><td>{_fmt_usd(bet["balance_before_cents"])}</td></tr>'
            f'<tr><td style="color:#64748b;padding:2px 0;">balance after (if fully filled)</td><td>{_fmt_usd(bet["balance_before_cents"] + bet.get("proceeds_cents", 0))}</td></tr>'
        )
    label, accent = _env_label(env)
    base = _base()
    loss_note = ("REAL MONEY" if env == "live" else "DEMO · play money")
    err_html = (f'<div style="background:#450a0a;border:1px solid #b91c1c;border-radius:8px;padding:10px 14px;'
                f'margin-bottom:12px;color:#fca5a5;">{_esc(error)}</div>' if error else "")
    caps_ok = bet.get("checks_ok", False)
    confirm_disabled = "" if caps_ok else "disabled"
    confirm_style = _btn_style('#b91c1c' if env == "live" else '#166534', disabled=not caps_ok)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Confirm bet — Kalshi Agent</title></head>
<body style="margin:0;background:#0b1220;"><div style="{_BET_STYLE}">
<a href="{base}/dashboard" style="color:#64748b;font-size:12px;text-decoration:none;">&larr; back to dashboard</a>
<h1 style="font-size:20px;margin:14px 0 2px;">Confirm your bet <span style="font-size:11px;color:{accent};border:1px solid {accent};border-radius:999px;padding:2px 10px;vertical-align:middle;">{label}</span></h1>
<div style="color:#64748b;font-size:12px;margin-bottom:14px;">This is the only step that can send anything. Review everything, then confirm.</div>
{err_html}
<div style="background:#0f172a;border:1px solid #1e293b;border-radius:12px;padding:16px;margin-bottom:14px;">
<div style="font-size:16px;font-weight:bold;margin-bottom:4px;">{action} {bet['count']} &times; {_esc(bet['side'].upper())} &middot; {_esc(bet['ticker'])}</div>
<div style="color:#94a3b8;font-size:13px;margin-bottom:12px;">{_esc(bet.get('title') or '')}</div>
<table style="width:100%;border-collapse:collapse;font-size:13px;">
<tr><td style="color:#64748b;padding:2px 0;">limit price</td><td><b>{bet['price_cents']}c</b> per contract</td></tr>
{cost_rows}
<tr><td style="color:#64748b;padding:2px 0;">order type</td><td>limit &middot; {_esc(dash._live_cfg.order_type.upper())} &middot; {'sells close what you hold — never shorts' if is_sell else 'IOC · buys'}</td></tr>
</table>
{f'<div style="margin-top:10px;color:#c4b5fd;font-size:12px;border-left:3px solid #7c3aed;padding-left:10px;">rationale: {_esc(bet.get("rationale") or "")}</div>' if bet.get("rationale") else ''}
</div>
<div style="margin-bottom:14px;">{_panel('RISK CHECKS — config-live.yaml standards, exchange-state verified', _bet_checks_rows(bet.get('checks') or []))}</div>
<div style="display:flex;gap:10px;align-items:center;">
<form method="post" action="{base}/bet/confirm" style="margin:0"><input type="hidden" name="token" value="{_esc(token)}"><button {confirm_disabled} style="{confirm_style}">{('&#128293; SEND REAL SELL' if is_sell else '&#128293; SEND REAL ORDER') if env == 'live' else ('Place demo sell' if is_sell else 'Place demo order')}</button></form>
<form method="post" action="{base}/bet/cancel" style="margin:0"><input type="hidden" name="token" value="{_esc(token)}"><button style="{_btn_style('#374151')}">Cancel</button></form>
<span style="color:#64748b;font-size:11px;">single-use token · expires 2 min after it was created · {loss_note}</span>
</div>
<div style="color:#475569;font-size:11px;margin-top:16px;">No guaranteed edge. Manual bets are discretionary — the 8&cent; model edge gate is not applied, every other standard from config-live.yaml is. Not financial advice.</div>
</div></body></html>"""


def render_bet_result(outcome: dict) -> str:
    base = _base()
    if not outcome.get("ok"):
        inner = f"""<h1 style="font-size:20px;margin:18px 0 8px;color:#f87171;">Bet not placed</h1>
<div style="background:#450a0a;border:1px solid #b91c1c;border-radius:8px;padding:12px 14px;color:#fca5a5;margin-bottom:14px;">{_esc(str(outcome.get('error') or 'unknown error'))}</div>"""
        bet = outcome.get("bet") or {}
        if bet.get("checks"):
            inner += _panel('RISK CHECKS AT SEND TIME', _bet_checks_rows(bet["checks"]))
        inner += f'<div style="margin-top:16px;"><a href="{base}/dashboard" style="color:#93c5fd;">&larr; back to dashboard</a></div>'
        return f'<!doctype html><html><head><meta charset="utf-8"><title>Bet failed — Kalshi Agent</title></head><body style="margin:0;background:#0b1220;"><div style="{_BET_STYLE}">{inner}</div></body></html>'
    bet = outcome.get("bet") or {}
    action = "SELL" if bet.get("action") == "sell" else "BUY"
    env = outcome.get("env") or "live"
    filled = outcome.get("filled", 0)
    zero_note = ('<div style="color:#eab308;font-size:12px;margin-top:8px;">IOC limit found no counterparties at that price — nothing filled. No position was changed.</div>' if filled == 0 else '')
    order_id = outcome.get("order_id") or "-"
    funds = outcome.get("funds") or {}
    funds_txt = (f"{_fmt_usd(funds.get('cents'))}" if funds.get("cents") is not None
                 else _esc(str(funds.get("error") or "refresh failed — will retry on next dashboard load")))
    inner = f"""<h1 style="font-size:20px;margin:18px 0 8px;color:#22c55e;">Order sent to Kalshi</h1>
<div style="background:#0f172a;border:1px solid #1e293b;border-radius:12px;padding:16px;margin-bottom:14px;font-size:13px;">
<div style="font-size:15px;font-weight:bold;margin-bottom:8px;">{action} {bet.get('count')} &times; {_esc(str(bet.get('side', '')).upper())} &middot; {_esc(str(bet.get('ticker', '')))} @ {bet.get('price_cents')}c (limit, {_esc(env.upper())})</div>
<div style="color:#94a3b8;">order id: {_esc(str(order_id))}</div>
<div style="color:#94a3b8;">filled (taker): <b>{filled}</b> of {bet.get('count')} contracts</div>
{zero_note}
<div style="color:#38bdf8;margin-top:10px;font-size:15px;">TOTAL FUNDS now: <b>{funds_txt}</b></div>
</div>
<div style="color:#64748b;font-size:12px;margin-bottom:14px;">Funds were re-pulled from Kalshi right after this order — the dashboard tile shows the same fresh number.</div>
<a href="{base}/dashboard" style="color:#93c5fd;">&larr; back to dashboard</a>"""
    return f'<!doctype html><html><head><meta charset="utf-8"><title>Bet placed — Kalshi Agent</title></head><body style="margin:0;background:#0b1220;"><div style="{_BET_STYLE}">{inner}</div></body></html>'


def render_bet_error(msg: str, prompt_value: str = "") -> str:
    base = _base()
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Bet — Kalshi Agent</title></head>
<body style="margin:0;background:#0b1220;"><div style="{_BET_STYLE}">
<a href="{base}/dashboard" style="color:#64748b;font-size:12px;text-decoration:none;">&larr; back to dashboard</a>
<h1 style="font-size:20px;margin:18px 0 8px;">Couldn't stage that bet</h1>
<div style="background:#450a0a;border:1px solid #b91c1c;border-radius:8px;padding:12px 14px;color:#fca5a5;white-space:pre-wrap;margin-bottom:14px;">{_esc(msg)}</div>
{_panel('ASK TO PLACE A BET', _bet_form(base, prompt_value))}
</div></body></html>"""


# ----------------------------------------------------------------- server ---
class _Handler(BaseHTTPRequestHandler):
    dashboard: WebDashboard = None  # injected
    landing: str = "dashboard"      # "dashboard" | "credentials"

    def _page(self) -> None:
        if self.landing == "credentials":
            self._html(render_credentials(self.dashboard))
        else:
            self._html(self.dashboard.render_page())

    def _dashboard_page(self) -> None:
        self._html(self.dashboard.render_page())

    def _html(self, text: str, content_type: str = "text/html; charset=utf-8",
              extra_headers: list[tuple[str, str]] | None = None,
              status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Strict-Transport-Security", "max-age=63072000")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, loc: str, *, set_session: bool = False,
                  clear_session: bool = False) -> None:
        self.send_response(303)
        self.send_header("Location", loc)
        if set_session:
            self.send_header("Set-Cookie",
                             f"{_SESSION_COOKIE}={_session_token()}; Path=/; HttpOnly; "
                             f"SameSite=Lax; Max-Age={_SESSION_MAX_AGE}")
        if clear_session:
            self.send_header("Set-Cookie",
                             f"{_SESSION_COOKIE}=; Path=/; HttpOnly; "
                             f"SameSite=Lax; Max-Age=0")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------------------------------------------------------------- GET ---
    def do_GET(self) -> None:  # noqa: N802
        raw_path = self.path.split("?")[0]
        qs = self.path.partition("?")[2]

        if raw_path == "/healthz":            # liveness probe stays open
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if raw_path in ("/", "/login"):
            # always render the unlock form here; the form posts to /login and
            # the session rides in the resulting /app/<token>/ URL
            self._html(render_login(error="error" in parse_qs(qs)))
            return

        if raw_path == "/app/" + _path_token():
            self._redirect(_base() + "/")
            return

        prefixed, path = _strip_base(raw_path)
        authed = prefixed or _is_authed(self.headers)
        if not authed:
            if path.startswith("/api/") or raw_path.startswith("/api/"):
                self._json({"error": "unauthorized"}, status=401)
            else:
                self._redirect("/")           # -> unlock form
            return

        if path == "/":
            self._page()
        elif path == "/dashboard":
            self._dashboard_page()
        elif path == "/credentials":
            self._html(render_credentials(self.dashboard))
        elif path == "/credentials/private.pem":
            pem = self.dashboard.credentials.get("generated_private_pem") \
                or self.dashboard.credentials.get("pem")
            if not pem:
                self._html("no private key available", "text/plain; charset=utf-8", status=404)
                return
            self._html(pem, content_type="application/x-pem-file",
                       extra_headers=[("Content-Disposition",
                                       "attachment; filename=kalshi_private_key.pem")])
        elif path == "/api/state":
            self._json(self.dashboard.state())
        elif path == "/api/funds":
            self._json(self.dashboard.funds_snapshot())
        else:
            self._html("not found", "text/plain; charset=utf-8", status=404)

    # --------------------------------------------------------------- POST ---
    def do_POST(self) -> None:  # noqa: N802
        raw_path = self.path.split("?")[0]
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        if length > _MAX_BODY_BYTES:
            self._html("payload too large", "text/plain; charset=utf-8", status=413)
            return
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        form = parse_qs(raw)

        if raw_path == "/login":
            pw = (form.get("password") or [""])[0]
            if _pw_matches(pw):
                log.warning("web: successful login from %s", self.client_address[0])
                self._redirect(_base() + "/", set_session=True)
            else:
                log.warning("web: FAILED login from %s", self.client_address[0])
                time.sleep(0.8)   # blunt brute-force attempts
                self._redirect("/?error=1")
            return
        if raw_path == "/logout":
            self._redirect("/", clear_session=True)
            return

        prefixed, path = _strip_base(raw_path)
        authed = prefixed or _is_authed(self.headers)
        if not authed:
            self._redirect("/")
            return

        if path and path.startswith("/bet/"):
            self._handle_bet_post(path, form)
            return

        try:
            if path == "/start":
                self.dashboard.start()
            elif path == "/pause":
                self.dashboard.pause()
            elif path == "/reset":
                self.dashboard.reset()
            elif path == "/mode":
                self.dashboard.set_mode((form.get("mode") or ["sim"])[0])
            elif path == "/credentials/save":
                ok, msg = self.dashboard.save_credentials(
                    (form.get("key_id") or [""])[0],
                    (form.get("pem") or [""])[0],
                    (form.get("env") or ["demo"])[0],
                    persist=bool(form.get("persist")),
                )
                self.dashboard.credentials["error"] = "" if ok else msg
                log.warning("dashboard: credential save -> %s", msg)
            elif path == "/credentials/generate":
                self.dashboard.generate_keypair()
            elif path == "/hunt/stop":
                self.dashboard.stop_hunt()
                self.dashboard.chat("app", "🛑 Hunt stopped. No more picks; open orders unaffected.")
            else:
                log.warning("web: unknown POST path %s", path)
        except Exception:  # noqa: BLE001
            log.exception("web: POST %s failed", path)
        finally:
            suffix = "/credentials" if path.startswith("/credentials") else "/"
            self._redirect(_base() + suffix)

    def _parse_proposal(self, prompt: str) -> tuple[dict | None, str | None]:
        """Returns (proposal, immediate_message). proposal: ticker/side/count/price_cents/rationale."""
        parsed = webbets.parse_bet_prompt(prompt)
        if parsed.get("ok") and parsed.get("action") in ("buy", "sell"):
            return ({"action": parsed["action"], "ticker": parsed["ticker"],
                     "side": parsed["side"],
                     "count": parsed["count"], "price_cents": parsed["price_cents"],
                     "rationale": "manual order ticket"}, None)
        if parsed.get("ok") and parsed.get("action") == "help":
            return (None, parsed["msg"])
        if parsed.get("ok") and parsed.get("action") == "funds":
            return (None, "__FUNDS__")
        if not parsed.get("ok") and webbets.looks_like_ticket(prompt):
            return (None, parsed.get("msg") or "couldn't parse ticket")
        # natural language path — needs an LLM key on the server
        dash = self.dashboard
        cfg = dash._live_cfg
        cred = dash.credentials
        base_url = DEMO_BASE_URL if cred.get("env") == "demo" else DEFAULT_BASE_URL
        pub_cfg = AppConfig()
        pub_cfg.base_url = base_url
        pub_cfg.rate_limit_per_sec = 4.0
        pub = KalshiClient(pub_cfg, signer=None)   # public endpoints only
        try:
            catalog = [{"ticker": c["ticker"], "title": c["title"]}
                       for c in webbets.platform_catalog(pub, max_total=140,
                                                         allow_combos=getattr(self.dashboard._live_cfg,
                                                                              "allow_combo_markets", False))]
            if not catalog:  # platform scan failed — graceful degradation to watchlist
                catalog = webbets.market_catalog(pub, list(cfg.series_watchlist or []),
                                                 per_series=20, max_total=140)
        except Exception as exc:  # noqa: BLE001
            catalog = []
            log.warning("bet: market catalog fetch failed: %s", exc)
        positions = []
        try:
            positions = self.dashboard._positions_open(self.dashboard._signed_client())
        except Exception as exc:  # noqa: BLE001
            log.warning("bet: positions fetch failed (sell grounding skipped): %s", exc)
        out = webbets.interpret_with_claude(prompt, catalog=catalog, positions=positions,
                                            cfg=cfg)
        if out.get("action") == "propose":
            return ({"action": out.get("order_side", "buy"),
                     "ticker": out["ticker"], "side": out["side"], "count": out["count"],
                     "price_cents": out.get("price_cents"),
                     "rationale": out.get("rationale", "")}, None)
        return (None, out.get("message") or "couldn't turn that into a bet")

    def _stage_from_prompt(self, prompt: str) -> None:
        """Parse → validate → either chat a YES/NO challenge or refuse."""
        dash = self.dashboard
        clean = prompt.strip()
        low = clean.lower()
        if low in ("funds", "balance", "cash", "refresh"):
            f = dash.funds_snapshot(force=True)
            txt = (f"TOTAL FUNDS ({(f.get('env') or 'live').upper()}): "
                   + (_fmt_usd(f["cents"]) if f.get("cents") is not None
                      else str(f.get("error") or "unavailable")))
            dash.chat("app", txt)
            return
        if low in ("stop", "stop hunting", "halt hunt", "end hunt", "stop hunt"):
            was = self.dashboard.hunt_active()
            self.dashboard.stop_hunt()
            self.dashboard.chat("app", ("🛑 Hunt STOPPED. No more picks will be staged — "
                                       "open orders are unaffected." if was
                                       else "No hunt is running."))
            return
        m_hunt = _parse_hunt_decree(low)
        if m_hunt is not None:
            minutes = m_hunt
            self.dashboard.set_hunt(minutes)
            self.dashboard.chat("app",
                  f"🎯 HUNT ON for {minutes:g} minute(s) across ALL liquid Kalshi markets — "
                  f"I'll pick the most mispriced tickets for you. Every order still needs your "
                  f"explicit YES in this box; deadline cuts picks off completely. "
                  f"Type 'stop hunting' to end it early.")
            self._stage_hunt(immediate=True)
            return
        if low in ("hunt", "maximize profits", "maximize", "next pick", "pick", "best bet"):
            if self.dashboard.hunt_active():
                self._stage_hunt()
            else:
                self.dashboard.chat("app",
                      "Tell me how long to hunt: e.g. 'maximize profits for 45 minutes' "
                      "(or 2 hours). I decree it, the clock decides when it ends.")
            return
        if low in ("help", "?"):
            dash.chat("app", "I understand:\n"
                             "• buy N yes|no TICKER [at Xc]   e.g. buy 1 yes KXNBA-26NOV25-LAL 22c\n"
                             "• sell N yes|no TICKER [at Xc]  — closes what you hold\n"
                             "• or plain English if Claude is configured\n"
                             "• funds — re-pull your Kalshi balance\n"
                             "• yes / no — answer a pending bet\n"
                             "• “make four 75 cent bets on the most likely markets” — plain-English multi-bet batch, YES each\n"
                             "• maximize profits for N minutes — all-market hunt (any liquid market), time-boxed\n"
                             "• hunt / next pick — fetch my current best pick while a hunt window is live\n"
                             "• stop hunting — kill the window early\n"
                             "Nothing is ever sent without your YES.")
            return
        proposal, immediate = self._parse_proposal(prompt)
        if proposal is None:
            dash.chat("app", immediate if immediate != "__FUNDS__" else "funds refreshed — see the tile above.")
            return
        if proposal.get("action") == "multi":
            self._stage_multi(proposal)
            return
        action = proposal.pop("action", "buy")
        staged = dash.stage_order(action, **proposal)
        if not staged.get("ok"):
            failed = [f"✗ {lbl} — {dtl}" for lbl, ok, dtl in (staged.get("bet") or {}).get("checks", [])
                      if not ok]
            msg = f"Could not stage that: {staged.get('error')}"
            if failed:
                msg += "\n" + "\n".join(failed)
            dash.chat("app", msg)
            return
        bet = staged["bet"]
        token = dash.stage_pending(bet)
        dash._chat_pending = token
        verb = "BUY" if action == "buy" else "SELL"
        cash_line = (f"total cost ≈ {_fmt_usd(bet['cost_cents'])} (incl. ~{_fmt_usd(bet['fee_cents'])} fee)"
                     if action == "buy" else
                     f"net proceeds ≈ {_fmt_usd(bet.get('proceeds_cents', 0))} after ~{_fmt_usd(bet['fee_cents'])} fee")
        cap_line = "caps: all checks pass (config-live.yaml, exchange-verified)"
        dash.chat("app",
                  f"{verb} {bet['count']} × {bet['side'].upper()} · {bet['ticker']}\n"
                  f"{bet.get('title') or ''}\n"
                  f"limit {bet['price_cents']}c (IOC) · {cash_line}\n"
                  f"balance after ≈ {_fmt_usd(bet['balance_before_cents'] + (bet.get('proceeds_cents', 0) if action == 'sell' else -bet['cost_cents']))}\n"
                  f"{cap_line}\n\n"
                  f"Reply YES to send it, or NO to cancel. (2-minute expiry)")

    # ---------------------------------------------------------- batches --
    def _batch_prompt(self, token: str) -> None:
        dash = self.dashboard
        bet = dash.pending_get(token)
        if not bet or dash._bet_batch is None:
            return
        b = dash._bet_batch
        pos = b["staged"] - len(dash._bet_queue)
        cash_line = f"total ≈ {_fmt_usd(bet['cost_cents'])} (incl. ~{_fmt_usd(bet['fee_cents'])} fee)"
        dash._chat_pending = token
        dash.chat("app",
                  f"Bet {pos} of {b['staged']} · BUY {bet['count']} × {bet['side'].upper()} · {bet['ticker']}\n"
                  f"{bet.get('title') or ''}\n"
                  f"limit {bet['price_cents']}c (IOC) · {cash_line}\n"
                  f"rationale: {bet.get('rationale') or 'multi-bet pick'}\n"
                  f"Reply YES to send, or NO to pass.")

    def _advance_batch(self) -> None:
        dash = self.dashboard
        if dash._bet_batch is None:
            return
        if not dash._bet_queue:
            b = dash._bet_batch
            lines = [f"📋 Batch complete — decided {b['done']} of {b['staged']} staged."]
            lines += [f"✅ {t}" for t in b["filled"]]
            lines += [f"⏭️ passed: {t}" for t in b["passed"]]
            lines += [f"✗ not sent: {t}" for t in b["refused"]]
            if b.get("note"):
                lines.append(f"note: {b['note']}")
            dash.chat("app", "\n".join(lines))
            dash._bet_batch = None
            return
        token = dash._bet_queue.pop(0)
        if dash.pending_get(token) is None:      # expired mid-batch — skip silently-ish
            dash._bet_batch["refused"].append("(expired)")
            self._advance_batch()
            return
        self._batch_prompt(token)

    def _batch_outcome(self, key: str, desc: str) -> None:
        dash = self.dashboard
        if dash._bet_batch is not None:
            dash._bet_batch["done"] += 1
            dash._bet_batch[key].append(desc)

    def _stage_multi(self, plan: dict) -> None:
        dash = self.dashboard
        if dash._bet_batch is not None:
            dash.chat("app", "Finish the batch you're on first — answer YES or NO to the pending bet, then ask again.")
            return
        legs = plan.get("legs") or []
        stake = plan.get("stake_cents")
        accepted, refused = [], []
        for leg in legs[:8]:
            count, price = leg["count"], leg.get("price_cents")
            staged = dash.stage_order("buy", ticker=leg["ticker"], side=leg["side"],
                                      count=count, price_cents=price,
                                      rationale=leg.get("rationale") or "multi-bet leg")
            # shrink to fit a named per-bet stake, if any
            while (staged.get("ok") and stake
                   and staged["bet"]["cost_cents"] > stake
                   and staged["bet"]["count"] > 1):
                count -= 1
                staged = dash.stage_order("buy", ticker=leg["ticker"], side=leg["side"],
                                          count=count, price_cents=price,
                                          rationale=leg.get("rationale") or "multi-bet leg")
            if not staged.get("ok"):
                refused.append(f"{leg['ticker']} — {staged.get('error')}")
                continue
            bet = staged["bet"]
            if stake and bet["cost_cents"] > stake:
                refused.append(f"{leg['ticker']} — 1 contract ({_fmt_usd(bet['cost_cents'])}) exceeds your {_fmt_usd(stake)} stake")
                continue
            accepted.append(bet)
        refused_lines = "\n".join(f"✗ skipped {r}" for r in refused)
        if not accepted:
            dash.chat("app", (refused_lines + "\n" if refused_lines else "")
                      + "Nothing staged — every leg tripped a cap. Your money stays put.")
            return
        tokens = [dash.stage_pending(bet, ttl_s=600) for bet in accepted]
        dash._bet_batch = {"staged": len(accepted), "done": 0,
                           "filled": [], "refused": list(refused), "passed": [],
                           "note": plan.get("note") or ""}
        dash._bet_queue = list(tokens)
        plan_lines = "\n".join(
            f"{i + 1}. BUY {b['count']} {b['side'].upper()} {b['ticker']} @ {b['price_cents']}c"
            for i, b in enumerate(accepted))
        stake_txt = f" · per-bet stake ≤ {_fmt_usd(stake)}" if stake else ""
        dash.chat("app", f"🎯 Batch — {len(accepted)} bet(s) it likes best{stake_txt}:\n{plan_lines}\n"
                        f"walking you through them one at a time (YES sends / NO passes, 10-minute window each)."
                        + ("\n" + refused_lines if refused_lines else ""))
        self._advance_batch()

    def _stage_hunt(self, immediate: bool = False) -> None:
        """Run the all-market hunt and stage its pick for YES/NO. Never sends."""
        dash = self.dashboard
        if not dash.hunt_active():
            dash.chat("app", "🛑 The hunt window has closed — decree another one ('maximize profits for 30 minutes').")
            return
        cred = dash.credentials
        base_url = DEMO_BASE_URL if cred.get("env") == "demo" else DEFAULT_BASE_URL
        pub_cfg = AppConfig()
        pub_cfg.base_url = base_url
        pub_cfg.rate_limit_per_sec = 4.0
        pub = KalshiClient(pub_cfg, signer=None)   # public market data
        try:
            bal = dash._signed_client().get_balance()
            balance_cents = int(bal.get("balance") or 0)
        except Exception:  # noqa: BLE001
            balance_cents = 0
        try:
            positions = dash._positions_open(dash._signed_client())
        except Exception:  # noqa: BLE001
            positions = []
        out = webbets.hunt_best_bet(pub, cfg=dash._live_cfg,
                                    balance_cents=balance_cents, positions=positions)
        if out.get("action") != "propose":
            dash.chat("app", f"🔍 Hunt passed this round: {out.get('message') or 'nothing compelling on the board'}\n"
                             f"(window live — type 'hunt' to re-scan)")
            return
        staged = dash.stage_order(out.get("order_side", "buy"), ticker=out["ticker"],
                                  side=out["side"], count=out["count"],
                                  price_cents=out.get("price_cents"),
                                  rationale=out.get("rationale", ""))
        if not staged.get("ok"):
            failed = [f"✗ {lbl} — {dtl}" for lbl, ok, dtl in (staged.get("bet") or {}).get("checks", [])
                      if not ok]
            msg = f"🔍 Hunt found {out['ticker']} but caps blocked it: {staged.get('error')}"
            if failed:
                msg += "\n" + "\n".join(failed)
            dash.chat("app", msg)
            return
        bet = staged["bet"]
        token = dash.stage_pending(bet)
        dash._chat_pending = token
        mins_left = int(dash.hunt_remaining() // 60)
        cash_line = f"total cost ≈ {_fmt_usd(bet['cost_cents'])} (incl. ~{_fmt_usd(bet['fee_cents'])} fee)"
        dash.chat("app",
                  f"🎯 HUNT PICK ({mins_left}m left): BUY {bet['count']} × {bet['side'].upper()} · {bet['ticker']}\n"
                  f"{bet.get('title') or ''}\n"
                  f"limit {bet['price_cents']}c (IOC) · {cash_line}\n"
                  f"rationale: {bet.get('rationale') or 'hunt pick'}\n"
                  f"caps: all checks pass (config-live.yaml, exchange-verified)\n\n"
                  f"Reply YES to send it, or NO to pass. (2-minute expiry)")

    def _handle_bet_post(self, path: str, form: dict) -> None:
        dash = self.dashboard
        base = _base()
        try:
            if path == "/bet/chat":
                prompt = (form.get("prompt") or [""])[0].strip()
                if not prompt:
                    self._redirect(base + "/dashboard")
                    return
                low = prompt.lower()
                dash.chat("you", prompt)
                pending = dash._chat_pending
                if low in YES_WORDS:
                    if pending:
                        dash._chat_pending = None
                        outcome = dash.confirm_pending(pending)
                        if outcome.get("ok"):
                            bet = outcome.get("bet") or {}
                            funds = outcome.get("funds") or {}
                            funds_txt = (_fmt_usd(funds["cents"])
                                         if funds.get("cents") is not None
                                         else str(funds.get("error") or "refresh failed"))
                            dash.chat("app",
                                      f"✅ Order sent. {str(bet.get('action', 'buy')).upper()} "
                                      f"{bet.get('count')} × {str(bet.get('side', '')).upper()} "
                                      f"{bet.get('ticker')} @ {bet.get('price_cents')}c · filled {outcome.get('filled')} of "
                                      f"{bet.get('count')} (IOC — unfilled remainder cancelled)\n"
                                      f"order id: {outcome.get('order_id') or '-'}\n"
                                      f"TOTAL FUNDS now: {funds_txt}")
                            self._batch_outcome("filled",
                                                f"{bet.get('ticker')} ({outcome.get('filled')}/{bet.get('count')} filled)")
                        else:
                            dash.chat("app", f"❌ Not sent — {outcome.get('error', 'unknown error')}")
                            self._batch_outcome("refused",
                                                f"{str((outcome.get('bet') or {}).get('ticker') or 'bet')} — {outcome.get('error', 'refused')}")
                        self._advance_batch()
                    else:
                        dash.chat("app", "Nothing waiting for a YES — describe a bet first (type help).")
                elif low in NO_WORDS:
                    if pending:
                        dash._chat_pending = None
                        dropped = dash.pending_pop(pending)
                        dash.chat("app", "Cancelled — nothing was sent.")
                        if dash._bet_batch is not None and dropped:
                            self._batch_outcome("passed", str(dropped.get("ticker") or "bet"))
                        self._advance_batch()
                    else:
                        dash.chat("app", "Nothing pending. (Yes, I know you hate confirmation pages.)")
                else:
                    self._stage_from_prompt(prompt)
                self._redirect(base + "/dashboard")
                return
            if path == "/bet/cancel":
                token = (form.get("token") or [""])[0]
                dash.pending_pop(token)
                self._redirect(base + "/dashboard")
                return
            if path == "/bet/confirm":
                token = (form.get("token") or [""])[0]
                outcome = dash.confirm_pending(token)
                self._html(render_bet_result(outcome))
                return
            if path == "/bet/preview":
                prompt = (form.get("prompt") or [""])[0].strip()
                if not prompt:
                    self._html(render_bet_error("empty prompt"))
                    return
                proposal, immediate = self._parse_proposal(prompt)
                if immediate == "__FUNDS__":
                    self._redirect(base + "/dashboard")
                    return
                if immediate:
                    self._html(render_bet_error(immediate, prompt))
                    return
                staged = dash.stage_order(proposal.pop("action", "buy"), **proposal)
                if not staged.get("ok"):
                    bet = staged.get("bet") or {}
                    if bet.get("checks"):            # risk-cap failure: show the checklist
                        self._html(render_bet_confirm(dash, bet, "", error=staged.get("error", "")))
                    else:
                        self._html(render_bet_error(staged.get("error") or "couldn't stage that bet", prompt))
                    return
                token = dash.stage_pending(staged["bet"])
                self._html(render_bet_confirm(dash, staged["bet"], token))
                return
            self._html("not found", "text/plain; charset=utf-8", status=404)
        except Exception:  # noqa: BLE001
            log.exception("bet POST %s failed", path)
            self._html(render_bet_error("internal error — see the log; nothing was sent"))

    def log_message(self, fmt, *args) -> None:  # quiet default access log
        log.debug("http %s", fmt % args)

    def version_string(self) -> str:
        return "kalshi-agent-web"


def serve(cfg: AppConfig, port: int = 8080, mode: str = "sim", autostart: bool = True,
          landing: str = "dashboard") -> None:
    logging.basicConfig(level=cfg.log_level, force=True,
                        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s")
    dash = WebDashboard(cfg, mode=mode)
    _Handler.dashboard = dash
    _Handler.landing = landing if landing in ("dashboard", "credentials") else "dashboard"
    httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    httpd.daemon_threads = True
    if autostart:
        dash.start()
    log.warning("dashboard serving on http://0.0.0.0:%d (mode=%s)", port, dash.mode)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        dash.pause()
