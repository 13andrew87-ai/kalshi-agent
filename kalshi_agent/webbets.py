"""Manual "prompt box" betting for the web dashboard.

Design rules — the live agent's standards are kept exactly where they are:

* REAL orders are placed only after a two-step, explicit confirmation
  (single-use token, 120-second TTL, full order + caps shown first).
* Orders go out as IOC limit orders with the same body shape LiveBroker
  builds for the live agent (time_in_force from config-live.yaml).
* The HARD caps from config-live.yaml are enforced against the exchange's own
  state (signed balance + open positions) before anything is staged AND again
  at confirm time: max contracts per order, min/max price band, min cash
  reserve, max open positions, per-market and total exposure caps, plus the
  price band the live strategy trades within.
* The model's edge gate (8c) is NOT applied — a manual discretionary bet is
  not a model bet. Everything else (size, price band, exposure, reserve,
  halt) stays where config-live.yaml puts it.
* Buys, and sells that CLOSE existing positions (never shorts). Everything is logged.
* Sells are validated against the exchange's own position (signed position_fp;
  positive = YES contracts, negative = NO). Exits are permitted even under a
  risk halt — a halt stops new bets, not de-risking.

Natural language:
    If an AI key is configured (see llm.py — Groq/Gemini free tiers work),
    free text is interpreted into a
    structured bet proposal, grounded on the live list of open markets in the
    watchlist series (so it can only name real, currently-tradable tickers).
    Without one, only the deterministic ticket syntax works.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any

from . import llm as _llm
from .config import AppConfig
from .models import Market

LIVE_CFG_PATH = os.environ.get("KALSHI_LIVE_CONFIG", "config-live.yaml")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")  # retained for back-compat

_ORDER_RE = re.compile(
    r"^(buy|sell)\s+(\d{1,4})\s+(yes|no)\s+([A-Za-z0-9][A-Za-z0-9\-_]{2,63})"
    r"(?:\s*(?:at|@)?\s*(\d{1,2})c?)?\s*$",
    re.IGNORECASE,
)
_BUY_RE = _ORDER_RE  # backwards-compatible alias
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-_]{1,63}$")

_HELP = ("format: buy <n> <yes|no> <TICKER> [at <price>c]  ·  or  sell <n> <yes|no> <TICKER> [at <price>c]\n"
         "e.g. buy 1 yes KXNBAGAME-26NOV25-ABC 42c   (price optional — buy: best ask, sell: best bid)\n"
         "or plain English, e.g. \"put a buck on the Chiefs to win tonight\"  ·  \"close out my Chiefs bet\"\n"
         "sells only close what you hold — the box will not short\n"
         "other commands: funds  ·  help")


# ------------------------------------------------------------ parsing -------
def parse_bet_prompt(text: str) -> dict[str, Any]:
    """Deterministic ticket syntax. No LLM involved."""
    t = (text or "").strip()
    low = t.lower()
    if low in ("funds", "balance", "refresh", "cash"):
        return {"ok": True, "action": "funds"}
    if low in ("help", "?", "h"):
        return {"ok": True, "action": "help", "msg": _HELP}
    m = _ORDER_RE.match(low)
    if not m:
        return {"ok": False, "msg": "couldn't parse that — " + _HELP.replace("\n", " ")}
    action, count_s, side, ticker, price_s = m.groups()
    count = int(count_s)
    ticker = ticker.upper()
    price = int(price_s) if price_s else None
    if count < 1:
        return {"ok": False, "msg": "count must be at least 1"}
    if count > 1000:
        return {"ok": False, "msg": "refusing a count that large from a prompt box"}
    if price is not None and not (1 <= price <= 99):
        return {"ok": False, "msg": "price must be between 1c and 99c"}
    return {"ok": True, "action": action, "count": count, "side": side,
            "ticker": ticker, "price_cents": price}


def looks_like_ticket(text: str) -> bool:
    return bool(_ORDER_RE.match((text or "").strip().lower()))


# ------------------------------------------------------------ economics -----
def estimate_fee_cents(count: int, price_cents: int, multiplier: float = 0.07) -> int:
    """Kalshi taker fee: multiplier * C * P * (1-P); ceiling — never under-reserve."""
    p = price_cents / 100.0
    return int(math.ceil(multiplier * count * p * (1.0 - p) * 100.0))


def _fp(v: Any) -> float:
    try:
        return float(str(v))
    except Exception:  # noqa: BLE001
        return 0.0


def exposure_cents(p: dict[str, Any]) -> int:
    """Best-effort cost basis of one exchange position, in cents.

    Kalshi's fixed-point exposure is a dollar string with 4 decimals; legacy
    fields are integer cents. Missing data -> 0 (the bet itself still counts).
    """
    for key in ("market_exposure_fp", "market_exposure", "exposure_fp",
                "position_fp_cost", "cost_fp"):
        v = p.get(key)
        if v is None or v == "":
            continue
        s = str(v)
        try:
            f = float(s)
        except ValueError:
            continue
        return int(round(f * 100)) if ("." in s or "e" in s.lower()) else int(f)
    return 0


def open_position_count(positions: list[dict[str, Any]] | None) -> int:
    if not positions:
        return 0
    n = 0
    for p in positions:
        if abs(_fp(p.get("position_fp") or p.get("position") or 0)) > 0:
            n += 1
    return n


def position_side_ticker(p: dict[str, Any]) -> str:
    return str(p.get("ticker") or p.get("market_ticker") or "")


def held_contracts(positions: list[dict[str, Any]] | None, ticker: str, side: str) -> int:
    """Contracts held of (ticker, side), from exchange position rows.

    Kalshi's position_fp is SIGNED: positive = YES contracts, negative = NO.
    Explicit per-side fields win if present (defensive).
    """
    n = 0
    for p in positions or []:
        if position_side_ticker(p) != ticker:
            continue
        if side == "yes":
            for k in ("yes_contracts", "yes_count"):
                if p.get(k) is not None:
                    n += int(_fp(p.get(k))); break
            else:
                net = _fp(p.get("position_fp") or p.get("position") or 0)
                if net > 0:
                    n += int(net)
        else:
            for k in ("no_contracts", "no_count"):
                if p.get(k) is not None:
                    n += int(_fp(p.get(k))); break
            else:
                net = _fp(p.get("position_fp") or p.get("position") or 0)
                if net < 0:
                    n += int(-net)
    return max(0, n)


def held_positions_summary(positions: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in positions or []:
        t = position_side_ticker(p)
        if not t:
            continue
        net = _fp(p.get("position_fp") or p.get("position") or 0)
        if net > 0:
            out.append({"ticker": t, "side": "yes", "count": int(net)})
        elif net < 0:
            out.append({"ticker": t, "side": "no", "count": int(-net)})
    return out


def assess_sell(cfg: AppConfig, *, balance_cents: int, count: int, price_cents: int,
                ticker: str, side: str, positions: list[dict[str, Any]] | None,
                halted: bool, halt_reason: str) -> dict[str, Any]:
    """Checks for a SELL that closes an existing position (never a short)."""
    fee = estimate_fee_cents(count, price_cents, cfg.taker_fee_multiplier)
    proceeds = count * price_cents - fee
    held = held_contracts(positions, ticker, side)
    checks: list[tuple[str, bool, str]] = []

    ok = 1 <= price_cents <= 99
    checks.append(("limit price between 1c and 99c", ok, f"{price_cents}c"))

    ok = count <= cfg.max_contracts_per_order
    checks.append((f"≤ {cfg.max_contracts_per_order} contracts per order", ok, f"selling {count}"))

    ok = count <= held
    checks.append((f"closing only what you hold ({held} {side.upper()} of {ticker})", ok,
                   f"selling {count}"))

    checks.append(("risk halt check — exits always permitted", True,
                   (halt_reason + " (halt blocks NEW bets, not exits)" if halted
                    else "no halt")))

    checks.append(("cash effect of this sale", True,
                   f"+{proceeds}c after est. fee of {fee}c"))

    return {
        "fee_cents": fee,
        "order_cost_cents": -proceeds,          # negative = cash IN
        "proceeds_cents": proceeds,
        "held": held,
        "checks": checks,
        "ok": all(ok for _, ok, _ in checks),
    }


def find_halt(obj: Any) -> tuple[bool, str]:
    if isinstance(obj, dict):
        if obj.get("halted") is True:
            return True, str(obj.get("halt_reason") or obj.get("reason") or "risk halt active")
        for v in obj.values():
            found, why = find_halt(v)
            if found:
                return True, why
    elif isinstance(obj, list):
        for v in obj:
            found, why = find_halt(v)
            if found:
                return True, why
    return False, ""


def live_halt_state(live_portfolio_path: str = "state/live/risk_state.json") -> tuple[bool, str]:
    """Risk-halt flag from the LOCAL live agent (best-effort, read-only,
    informational only — the Fly machine cannot see this file unless they
    share the state volume)."""
    try:
        import json
        with open(live_portfolio_path, encoding="utf-8") as fh:
            return find_halt(json.load(fh))
    except FileNotFoundError:
        return False, ""
    except Exception:  # noqa: BLE001
        return False, ""


def assess_buy(cfg: AppConfig, *, balance_cents: int, count: int, price_cents: int,
               ticker: str, positions: list[dict[str, Any]] | None,
               halted: bool, halt_reason: str) -> dict[str, Any]:
    """Evaluate every hard cap for a manual BUY. Pure — fully testable."""
    fee = estimate_fee_cents(count, price_cents, cfg.taker_fee_multiplier)
    spend = count * price_cents + fee
    checks: list[tuple[str, bool, str]] = []

    ok = count <= cfg.max_contracts_per_order
    checks.append((f"≤ {cfg.max_contracts_per_order} contracts per order", ok, f"asked for {count}"))

    ok = cfg.min_price_cents <= price_cents <= cfg.max_price_cents
    checks.append((f"price in [{cfg.min_price_cents}c, {cfg.max_price_cents}c] band", ok,
                   f"{price_cents}c"))

    reserve = int(balance_cents * cfg.min_cash_reserve_pct / 100)
    ok = balance_cents - spend >= reserve
    checks.append((f"keeps {cfg.min_cash_reserve_pct:.0f}% cash reserve ({reserve}c)", ok,
                   f"spend {spend}c of {balance_cents}c"))

    open_n = open_position_count(positions)
    ok = open_n < cfg.max_open_positions
    checks.append((f"≤ {cfg.max_open_positions} open positions", ok, f"currently {open_n}"))

    positions = positions or []
    total_exp = sum(exposure_cents(p) for p in positions)
    tkr_exp = sum(exposure_cents(p) for p in positions
                  if str(p.get("ticker") or p.get("market_ticker") or "") == ticker)
    per_cap = balance_cents * cfg.max_pct_bankroll_per_market / 100
    tot_cap = balance_cents * cfg.max_total_exposure_pct / 100
    ok = tkr_exp + spend <= per_cap
    checks.append((f"≤ {cfg.max_pct_bankroll_per_market:.0f}% per-market exposure ({int(per_cap)}c)",
                   ok, f"this bet lands at {tkr_exp + spend}c"))
    ok = total_exp + spend <= tot_cap
    checks.append((f"≤ {cfg.max_total_exposure_pct:.0f}% total exposure ({int(tot_cap)}c)",
                   ok, f"this bet lands at {total_exp + spend}c"))

    checks.append(("risk halt clear", not halted, halt_reason if halted else "none"))

    return {
        "fee_cents": fee,
        "order_cost_cents": spend,
        "checks": checks,
        "ok": all(ok for _, ok, _ in checks),
    }


def load_live_cfg(path: str | None = None) -> tuple[AppConfig, bool]:
    """Manual-bet standards live in config-live.yaml — load it or fall back to
    AppConfig defaults (and say so in the UI)."""
    p = Path(path or LIVE_CFG_PATH)
    try:
        cfg = AppConfig.load(str(p))
        cfg.dry_run = False          # the confirm page IS the gate
        cfg.live_trading_enabled = True
        return cfg, True
    except Exception:  # noqa: BLE001
        cfg = AppConfig()
        cfg.dry_run = False
        cfg.live_trading_enabled = True
        return cfg, False


# --------------------------------------------------------- market catalog ---
def platform_catalog(client: Any, max_total: int = 160) -> list[dict[str, Any]]:
    """Top liquid OPEN markets across the whole exchange (not just watchlist).

    Used when the user decrees an all-market hunt. Cheap-side filter: keeps only
    markets with at least one side inside the [8c, 30c] band, so suggestions are
    actually actionable under config-live.yaml. Sorted by volume, most liquid
    first. Never raises — network failure returns []."""
    try:
        rows = client.get_markets(status="open", limit=1000, max_pages=2,
                                  mve_filter="exclude")
    except Exception as exc:  # noqa: BLE001
        log.warning("platform catalog fetch failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for d in rows:
        try:
            m = Market.from_api(d)
        except Exception:  # noqa: BLE001
            continue
        if m.status != "open" or m.is_provisional:
            continue
        cheap = min(m.yes_ask if m.yes_ask > 0 else 101,
                    m.no_ask if m.no_ask > 0 else 101)
        if cheap < 8 or cheap > 30:
            continue
        title = (m.title or m.subtitle or "").strip()
        if not title:
            continue
        out.append({"ticker": m.ticker, "title": title[:80],
                    "yes_ask": m.yes_ask, "no_ask": m.no_ask,
                    "yes_bid": m.yes_bid, "no_bid": m.no_bid,
                    "volume": m.volume or 0})
    out.sort(key=lambda c: -int(c.get("volume") or 0))
    return out[:max_total]


_HUNT_SYSTEM = """You are an aggressive but disciplined Kalshi hunt engine with a TINY bankroll.
Pick exactly ONE bet from the candidate list that offers the best risk-adjusted profit.
Rules, in priority order:
1. ONLY name tickers from the CANDIDATES list. Never invent tickers or prices.
2. Side: buy "yes" if the event is more likely than the price implies, else buy "no".
3. Size is 1-{max_contracts} contracts. Price must sit inside the candidate's ask.
4. Think like a bettor: mispriced probabilities, asymmetric payouts, late-but-live underdogs. Avoid coin-flip favorites at long prices; prefer concrete edges.
5. You may also choose to PASS if nothing is genuinely mispriced — then action must be "none".
6. Reply with ONLY compact JSON, nothing else: {{"action":"propose","ticker":"...","side":"yes"|"no","count":N,"price_cents":null|N,"rationale":"one punchy sentence"}} or {{"action":"none","message":"one specific sentence on why you passed"}}
"""


def hunt_best_bet(client: Any, *, cfg: AppConfig, balance_cents: int,
                  positions: list[dict[str, Any]] | None = None,
                  session: Any = None) -> dict[str, Any]:
    """All-market hunt: pick ONE mispriced ticket across the board (any series).

    Returns the same shape as interpret_with_claude so the staging path is
    shared. Never places an order. `client` is the market-data (public) client."""
    llm = _llm.LLMClient(session=session)
    if not llm.configured:
        return {"action": "none",
                "message": "hunt needs an AI key on the server — add a FREE one: "
                           "fly secrets set GROQ_API_KEY=... (console.groq.com, free)"}
    cat = platform_catalog(client, max_total=140)
    if not cat:
        return {"action": "none",
                "message": "couldn't scan open markets right now (network) — try again in a minute"}
    held = held_positions_summary(positions)
    positions_txt = positions_catalog(held)
    cat_txt = "\n".join(
        f"{c['ticker']} | {c['title']} | yes_ask {c['yes_ask']}c | no_ask {c['no_ask']}c | vol {c['volume']}"
        for c in cat)
    sys_p = _HUNT_SYSTEM.format(max_contracts=cfg.max_contracts_per_order)
    user_msg = (f"BALANCE: {balance_cents}c available\nOPEN POSITIONS:\n{positions_txt}\n\n"
                f"CANDIDATES (top liquid open markets, any series):\n{cat_txt}\n")
    text = llm.complete(system=sys_p, user=user_msg, max_tokens=300)
    if text is None:
        return {"action": "none", "message": f"{llm.provider} AI call failed — hunt again shortly or type a bet"}
    obj = parse_llm_json(text)
    if not obj:
        return {"action": "none", "message": "hunt model didn't return JSON — try again or type a bet"}
    if obj.get("action") != "propose":
        return {"action": "none", "message": str(obj.get("message") or "passed on the board — no mispricing worth the bankroll")}
    ticker = str(obj.get("ticker") or "").upper().strip()
    side = str(obj.get("side") or "").lower()
    try:
        count = int(obj.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    price = obj.get("price_cents", None)
    try:
        price = int(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    if not _TICKER_RE.match(ticker):
        return {"action": "none", "message": "hunt proposed a malformed ticker — pass discarded"}
    if side not in ("yes", "no"):
        return {"action": "none", "message": "hunt proposed an invalid side — pass discarded"}
    known = {c["ticker"] for c in cat}
    if ticker not in known:
        return {"action": "none", "message": f"hunt proposed an unknown ticker {ticker!r} — pass discarded"}
    count = max(1, min(count, cfg.max_contracts_per_order))
    return {"action": "propose", "order_side": "buy", "ticker": ticker, "side": side,
            "count": count, "price_cents": price,
            "rationale": str(obj.get("rationale") or "hunt pick")[:200]}


def market_catalog(client: Any, series_list: list[str], per_series: int = 25,
                   max_total: int = 150) -> list[dict[str, str]]:
    """Compact list of currently-open markets for grounding the LLM.
    Only real, tradable tickers are returned."""
    out: list[dict[str, str]] = []
    for series in series_list:
        if len(out) >= max_total:
            break
        try:
            resp = client.request("GET", "/markets",
                                  params={"series_ticker": series,
                                          "status": "open",
                                          "limit": per_series})
            markets = resp.get("markets", [])
        except Exception:  # noqa: BLE001
            continue
        for m in markets:
            if len(out) >= max_total:
                break
            t = str(m.get("ticker") or "").strip()
            title = str(m.get("title") or m.get("subtitle") or "").strip()
            if t and title:
                out.append({"ticker": t, "title": title[:80]})
    return out


_LLM_SYSTEM = """You translate a user's request into ONE Kalshi order proposal, or explain why not.
Rules, in priority order:
1. You may ONLY name tickers from the OPEN MARKETS list (for buys) or the OPEN POSITIONS list (for sells) provided in the user message. Never invent tickers.
2. order_side is "buy" or "sell". A sell CLOSES an existing position — you may only sell tickers in OPEN POSITIONS, in the side held, in a count <= held. Never propose a short.
3. A "yes" buy pays off if the event happens; a "no" buy pays if it doesn't. Selling YES/NO closes that leg.
4. Size is 1-{max_contracts} contracts.
5. If the request doesn't map to a real open market / held position, or is too vague, action must be "none" with a short, specific message naming what IS available.
6. price_cents: integer 1-99, or null to take the current best ask (buys) / best bid (sells). Prefer null unless the user named a price.
7. Reply with ONLY compact JSON: {{"action":"propose","order_side":"buy"|"sell","ticker":"...","side":"yes"|"no","count":N,"price_cents":null|N,"rationale":"one short sentence"}} or {{"action":"none","message":"..."}}
Do not add commentary outside the JSON."""


def parse_llm_json(text: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) else None



def positions_catalog(held: list[dict[str, Any]]) -> str:
    lines = [f"{h['ticker']} | {h['side']} | holding {h['count']}" for h in held]
    return "\n".join(lines) if lines else "(no open positions)"


def interpret_with_claude(prompt: str, *, catalog: list[dict[str, str]],
                          positions: list[dict[str, Any]] | None = None,
                          cfg: AppConfig,
                          api_key: str | None = None,
                          session: Any = None) -> dict[str, Any]:
    """AI path: free text -> structured proposal (any configured provider). Never places an order."""
    client = _llm.LLMClient(session=session, api_key=api_key)
    if not client.configured:
        return {"action": "none",
                "message": "natural language needs an AI key on the server — add a FREE one: "
                           "fly secrets set GROQ_API_KEY=... (console.groq.com, free, no card) "
                           "or GEMINI_API_KEY=... (aistudio.google.com, free) — "
                           "or use ticket syntax: buy 1 yes TICKER @ 42c"}
    markets_txt = "\n".join(f"{m['ticker']} | {m['title']}" for m in catalog) or "(no open markets found in watchlist series)"
    held = held_positions_summary(positions)
    positions_txt = positions_catalog(held)
    sys_p = _LLM_SYSTEM.format(max_contracts=cfg.max_contracts_per_order)
    user_msg = (f"USER REQUEST:\n{prompt}\n\nOPEN MARKETS:\n{markets_txt}\n\nOPEN POSITIONS:\n{positions_txt}\n")
    held_map = {(h["ticker"], h["side"]): h["count"] for h in held}
    text = client.complete(system=sys_p, user=user_msg, max_tokens=400)
    if text is None:
        return {"action": "none",
                "message": f"{client.provider} AI call failed (no credits / outage?) — "
                           f"ticket syntax always works: buy 1 yes TICKER @ 42c"}
    obj = parse_llm_json(text)
    if not obj:
        return {"action": "none", "message": "model didn't return JSON — try ticket syntax"}
    if obj.get("action") != "propose":
        return {"action": "none", "message": str(obj.get("message") or "not a bettable request")}
    order_side = str(obj.get("order_side") or obj.get("direction") or "buy").lower()
    if order_side not in ("buy", "sell"):
        return {"action": "none", "message": "model proposed an invalid order_side — rejected"}
    side = str(obj.get("side") or "").lower()
    ticker = str(obj.get("ticker") or "").upper().strip()
    try:
        count = int(obj.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    price = obj.get("price_cents", None)
    try:
        price = int(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    if not _TICKER_RE.match(ticker):
        return {"action": "none", "message": "model proposed a malformed ticker — rejected"}
    if side not in ("yes", "no"):
        return {"action": "none", "message": "model proposed an invalid side — rejected"}
    count = max(1, min(count, cfg.max_contracts_per_order))
    rationale = str(obj.get("rationale") or "")[:200]
    if order_side == "sell":
        held_n = held_map.get((ticker, side), 0)
        if held_n <= 0:
            return {"action": "none",
                    "message": f"you don't hold any {side.upper()} in {ticker} — nothing to sell"}
        count = min(count, held_n)
    else:
        known = {m["ticker"] for m in catalog}
        if ticker not in known:
            return {"action": "none",
                    "message": f"model proposed an unknown/untradable ticker {ticker!r} — rejected"}
    return {"action": "propose", "order_side": order_side, "ticker": ticker, "side": side,
            "count": count, "price_cents": price, "rationale": rationale}
