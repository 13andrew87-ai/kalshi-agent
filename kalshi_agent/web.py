"""Live web dashboard + agent control server (Python stdlib only, no dependencies).

Modes:
  sim   — autonomous agent on the simulated exchange (no credentials, fast clock)
  paper — autonomous agent on REAL Kalshi market data, paper fills (public
          endpoints; no credentials needed for market data)

Real-money live trading intentionally has NO web control: it stays behind the
CLI's four opt-in gates (see README).

Endpoints:
  GET  /           dashboard (server-rendered, meta-refresh every 5 s, no JS needed)
  GET  /api/state  JSON snapshot
  POST /start      start the agent loop
  POST /pause      pause the agent loop
  POST /reset      wipe state, new universe (auto-restarts if it was running)
  POST /mode       switch sim <-> paper (resets state)
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
import shutil
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .agent import Agent
from .auth import KalshiSigner, generate_keypair
from .brokers import PaperBroker
from .client import KalshiClient, KalshiApiError
from .config import AppConfig, DEFAULT_BASE_URL, DEMO_BASE_URL
from .portfolio import Portfolio
from .risk import RiskManager
from .sim import SimExchange
from .report import sparkline_svg

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
        self.mode = mode if mode in ("sim", "paper") else "sim"
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

        self.display: dict = {
            "mode": self.mode, "running": False, "cycles": 0, "started_ts": None,
            "last_cycle_ts": None, "last_error": "", "equity_cents": float(cfg.bankroll_cents),
            "cash_cents": float(cfg.bankroll_cents), "return_pct": 0.0,
            "positions": [], "closed": [], "risk": {}, "signals": [],
            "equity_history": [], "last_res": {}, "universe_seed": None,
        }
        self._build_world()

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

    # ------------------------------------------------------------ control ---
    def poll(self) -> float:
        return self.cfg.web_sim_poll_seconds if self.mode == "sim" \
            else self.cfg.web_paper_poll_seconds

    def start(self) -> None:
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

    def state(self) -> dict:
        with self._display_lock:
            d = dict(self.display)
        d["running"] = bool(self._worker and self._worker.is_alive()
                            and not self._stop_event.is_set())
        d["now"] = time.time()
        d["logs"] = list(self.log_handler.ring)[-40:]
        return d

    # ------------------------------------------------------------ rendering ---
    def render_page(self) -> str:
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
{_card('EQUITY', f"{eq:,.2f}$", f"{ret:+.2f}% return", '#22c55e' if ret >= 0 else '#ef4444')}
{_card('CASH', f"{s['cash_cents'] / 100:,.2f}$", 'free to deploy')}
{_card('OPEN POSITIONS', f"{pos_count}", f"max {self.cfg.max_open_positions}")}
{_card('CLOSED TRADES', f"{closed_n}", f"win rate {wins}/{closed_n or 0}")}
{_card('RISK ENGINE', 'HALT' if halted else 'OK',
       f"streak {risk.get('consecutive_losses', 0)}/{self.cfg.consecutive_loss_kill}",
       '#ef4444' if halted else '#22c55e')}
</div>

<div style="margin-bottom:16px;">{svg}</div>

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
<a href="{base}/" style="color:#93c5fd;">dashboard</a> work without any credentials.
<a href="{base}/" style="color:#93c5fd;">&larr; back to dashboard</a> &middot; <form method="post" action="/logout" style="display:inline"><button style="background:none;border:none;color:#64748b;font-size:12px;cursor:pointer;text-decoration:underline;font-family:inherit;padding:0;">sign out</button></form></div>

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


# ----------------------------------------------------------------- server ---
class _Handler(BaseHTTPRequestHandler):
    dashboard: WebDashboard = None  # injected
    landing: str = "dashboard"      # "dashboard" | "credentials"

    def _page(self) -> None:
        if self.landing == "credentials":
            self._html(render_credentials(self.dashboard))
        else:
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
            elif path == "/credentials/clear":
                self.dashboard.clear_credentials()
            else:
                log.warning("web: unknown POST path %s", path)
        except Exception:  # noqa: BLE001
            log.exception("web: POST %s failed", path)
        finally:
            suffix = "/credentials" if path.startswith("/credentials") else "/"
            self._redirect(_base() + suffix)

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
