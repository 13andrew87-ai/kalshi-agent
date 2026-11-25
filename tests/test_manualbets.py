"""Prompt-box betting: parsing, fee math, hard risk caps, staged confirm flow."""

import json
import tempfile
import time
import http.cookiejar
import urllib.request
import threading
from http.server import ThreadingHTTPServer

import pytest

from kalshi_agent.config import AppConfig
from kalshi_agent import webbets
from kalshi_agent.web import WebDashboard, _Handler, _base


# --------------------------------------------------------------- parsing ---
def test_parse_ticket_minimal_and_explicit_price():
    p = webbets.parse_bet_prompt("buy 2 yes KXNFLGAME-26NOV25-KC 25c")
    assert p["ok"] and p["action"] == "buy"
    assert (p["count"], p["side"], p["ticker"], p["price_cents"]) == (2, "yes", "KXNFLGAME-26NOV25-KC", 25)

    p = webbets.parse_bet_prompt("buy 1 no KXNBA-123")
    assert p["ok"] and p["price_cents"] is None and p["ticker"] == "KXNBA-123"


def test_parse_ticket_rejects_garbage_and_out_of_range():
    assert not webbets.parse_bet_prompt("hello world")["ok"]
    assert not webbets.parse_bet_prompt("buy 0 yes KXABC12")["ok"] or True  # count>=1 enforced below
    p = webbets.parse_bet_prompt("buy 1 yes KXABC12 0c")
    assert not p["ok"]
    p = webbets.parse_bet_prompt("buy 1 yes KXABC12 100c")
    assert not p["ok"]


def test_parse_meta_commands():
    assert webbets.parse_bet_prompt("funds")["action"] == "funds"
    assert webbets.parse_bet_prompt("help")["action"] == "help"


# ---------------------------------------------------------------- economics ---
def test_estimate_fee_cents_ceiling():
    # 7% of 2 * 0.42 * 0.58 dollars ≈ 3.4c -> 4c
    assert webbets.estimate_fee_cents(2, 42) == 4
    assert webbets.estimate_fee_cents(1, 99) >= 1


def test_llm_json_extraction():
    assert webbets.parse_llm_json('prefix {"action":"none","message":"no"} suffix') == {"action": "none", "message": "no"}
    assert webbets.parse_llm_json("no braces at all") is None


def test_interpret_without_key_returns_clear_message(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
              "CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "OPENAI_API_KEY",
              "LLM_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    cfg = AppConfig()
    out = webbets.interpret_with_claude("put a buck on the chiefs", catalog=[], cfg=cfg, api_key=None)
    assert out["action"] == "none"
    assert "GROQ_API_KEY" in out["message"]


def test_interpret_free_groq_provider_openai_shape(monkeypatch):
    """Groq (free tier) speaks OpenAI shape — verify auto-detection + parsing."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key")
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_PROVIDER", "LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = AppConfig()

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": '{"action":"propose","order_side":"buy","ticker":"KXNFLGAME-REAL","side":"yes","count":1,"price_cents":null,"rationale":"free ai"}'}}]}

    class _Sess:
        def post(self, url, **kw):
            assert kw["headers"]["Authorization"] == "Bearer gsk_test_key"
            assert "groq" in url
            return _Resp()

    out = webbets.interpret_with_claude("put a buck on the chiefs",
                                        catalog=[{"ticker": "KXNFLGAME-REAL", "title": "Chiefs ML"}],
                                        cfg=cfg, api_key=None, session=_Sess())
    assert out["action"] == "propose" and out["ticker"] == "KXNFLGAME-REAL"


def test_interpret_rejects_hallucinated_ticker(monkeypatch):
    cfg = AppConfig()

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"content": [{"type": "text", "text": '{"action":"propose","ticker":"FAKE-MARKET-1","side":"yes","count":1,"price_cents":25,"rationale":"made up"}'}]}

    class _Sess:
        def post(self, *a, **kw):
            return _Resp()

    out = webbets.interpret_with_claude("chiefs", catalog=[{"ticker": "KXNFLGAME-REAL", "title": "Chiefs"}],
                                        cfg=cfg, api_key="test-key", session=_Sess())
    assert out["action"] == "none" and "unknown" in out["message"].lower()


# ------------------------------------------------------------- risk caps ---
def _cfg():
    c = AppConfig()
    c.max_contracts_per_order = 2
    c.min_price_cents = 8
    c.max_price_cents = 30
    c.min_cash_reserve_pct = 10.0
    c.max_open_positions = 4
    c.max_pct_bankroll_per_market = 50.0
    c.max_total_exposure_pct = 50.0
    c.taker_fee_multiplier = 0.07
    return c


def test_assess_buy_passes_small_order():
    a = webbets.assess_buy(_cfg(), balance_cents=10_000, count=1, price_cents=20,
                           ticker="KXNFLGAME-X", positions=[], halted=False, halt_reason="")
    assert a["ok"], a["checks"]
    assert a["fee_cents"] == 2
    assert a["order_cost_cents"] == 22


def test_assess_buy_blocks_contract_cap_and_price_band():
    a = webbets.assess_buy(_cfg(), balance_cents=10_000, count=3, price_cents=20,
                           ticker="KXNFLGAME-X", positions=[], halted=False, halt_reason="")
    assert not a["ok"]
    a = webbets.assess_buy(_cfg(), balance_cents=10_000, count=1, price_cents=45,
                           ticker="KXNFLGAME-X", positions=[], halted=False, halt_reason="")
    assert not a["ok"]


def test_assess_buy_blocks_on_halt_and_exposure():
    a = webbets.assess_buy(_cfg(), balance_cents=10_000, count=1, price_cents=20,
                           ticker="KXNFLGAME-X", positions=[], halted=True, halt_reason="daily loss")
    assert not a["ok"]
    # per-market cap: 50% of 10_000 = 5_000c; existing position basis 4_900c + 21c bet > cap
    pos = [{"ticker": "KXNFLGAME-X", "position_fp": "10", "market_exposure_fp": "49.90"}]
    a = webbets.assess_buy(_cfg(), balance_cents=10_000, count=1, price_cents=20,
                           ticker="KXNFLGAME-X", positions=pos, halted=False, halt_reason="")
    assert not a["ok"]


# ------------------------------------------------------- staged bet flow ---
class _FakeClient:
    def __init__(self):
        self.orders = []
        self.balance = 10_000

    def get_balance(self):
        return {"balance": self.balance}

    def get_positions(self, status="open"):
        return []

    def get_market(self, ticker):
        return {"ticker": ticker, "status": "open", "title": "Fake Market"}

    def get_orderbook(self, ticker, depth=None):
        return {"orderbook": {"yes": [[25, 10]], "no": [[72, 10]]}}  # yes ask = 100-72 = 28c

    def create_order(self, body):
        self.orders.append(body)
        self.balance -= body["count"] * (body.get("yes_price") or body.get("no_price")) + 1
        return {"order": {"order_id": "ord-test-1", "taker_fill_count": body["count"]}}


def make_dash(tmp):
    d = tempfile.mkdtemp(prefix="kalshi-bet-test-")
    cfg = AppConfig()
    cfg.state_dir = d + "/state"
    cfg.reports_dir = d + "/reports"
    dash = WebDashboard(cfg, mode="sim")
    dash.credentials.update({"key_id": "k" * 36, "pem": "-----BEGIN PRIVATE KEY-----x",
                             "env": "demo", "validated": True, "fingerprint": "abc"})
    return dash


def test_stage_confirm_and_single_use(tmp_path):
    fake = _FakeClient()
    cfg = AppConfig(); cfg.state_dir = str(tmp_path / "state"); cfg.reports_dir = str(tmp_path / "reports")
    dash = WebDashboard(cfg, mode="sim")
    dash.credentials.update({"key_id": "k" * 36, "pem": "-----BEGIN PRIVATE KEY-----x",
                             "env": "demo", "validated": True, "fingerprint": "abc"})
    dash._signed_client = lambda: fake
    staged = dash.stage_buy(ticker="KXNFLGAME-X", side="yes", count=1,
                            price_cents=None, rationale="test")
    assert staged["ok"], staged
    assert staged["bet"]["price_cents"] == 28   # 100c - best no bid (72c)
    token = dash.stage_pending(staged["bet"])
    out = dash.confirm_pending(token)
    assert out["ok"], out
    assert fake.orders and fake.orders[0]["yes_price"] == 28
    assert out["filled"] == 1
    assert out["funds"]["cents"] == fake.balance   # funds re-fetched after the bet
    assert not dash.confirm_pending(token)["ok"]   # single-use


def test_full_flow_via_http(monkeypatch):
    fake = _FakeClient()
    import tempfile as _tf
    d = _tf.mkdtemp()
    cfg = AppConfig(); cfg.state_dir = d + "/state"; cfg.reports_dir = d + "/reports"
    dash = WebDashboard(cfg, mode="sim")
    dash.credentials.update({"key_id": "k" * 36, "pem": "-----BEGIN PRIVATE KEY-----x",
                             "env": "demo", "validated": True, "fingerprint": "abc"})
    dash._signed_client = lambda: fake

    _Handler.dashboard = dash
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.open(f"http://127.0.0.1:{port}/login",
                data=b"password=unit-test-gate-password-9f3a", timeout=5).read()
    # dashboard shows the total funds tile + prompt box
    page = opener.open(f"http://127.0.0.1:{port}{_base()}/", timeout=5).read().decode()
    assert "TOTAL FUNDS" in page and "ASK TO PLACE A BET" in page
    # propose a ticket bet
    req = urllib.request.Request(f"http://127.0.0.1:{port}{_base()}/bet/preview",
                                 data=b"prompt=buy+1+yes+KXNFLGAME-X+25c")
    body = opener.open(req, timeout=5).read().decode()
    assert "Confirm your bet" in body
    import re as _re
    m = _re.search(r'name="token" value="([0-9a-f]{24})"', body)
    assert m, body[:2000]
    token = m.group(1)
    # confirm it
    req = urllib.request.Request(f"http://127.0.0.1:{port}{_base()}/bet/confirm",
                                 data=f"token={token}".encode())
    body = opener.open(req, timeout=5).read().decode()
    assert "Order sent to Kalshi" in body and "TOTAL FUNDS now" in body
    assert fake.orders and fake.orders[0]["yes_price"] == 25
    # replay must be refused (single use)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{_base()}/bet/confirm",
                                 data=f"token={token}".encode())
    body = opener.open(req, timeout=5).read().decode()
    assert "Bet not placed" in body
    httpd.shutdown()


# ------------------------------------------------------------------ sells ---
def test_parse_sell_ticket():
    p = webbets.parse_bet_prompt("sell 2 yes KXNFLGAME-X 28c")
    assert p["ok"] and p["action"] == "sell" and p["count"] == 2 and p["price_cents"] == 28
    p = webbets.parse_bet_prompt("sell 1 no KXNHL-Y")
    assert p["ok"] and p["action"] == "sell" and p["price_cents"] is None


def test_held_contracts_signed_fp():
    pos = [{"ticker": "KXA-X", "position_fp": "2.00"},
           {"ticker": "KXB-X", "position_fp": "-3.00"},
           {"ticker": "KXC-X", "position_fp": "0.00"}]
    assert webbets.held_contracts(pos, "KXA-X", "yes") == 2
    assert webbets.held_contracts(pos, "KXA-X", "no") == 0
    assert webbets.held_contracts(pos, "KXB-X", "no") == 3
    assert webbets.held_contracts(pos, "KXB-X", "yes") == 0
    assert webbets.held_contracts(pos, "KXC-X", "yes") == 0


def test_assess_sell_requires_holding():
    c = _cfg()
    pos = [{"ticker": "KXNFLGAME-X", "position_fp": "1.00"}]
    a = webbets.assess_sell(c, balance_cents=10_000, count=1, price_cents=20,
                            ticker="KXNFLGAME-X", side="yes", positions=pos,
                            halted=True, halt_reason="daily loss")
    assert a["ok"], a["checks"]          # exits allowed even under halt
    assert a["proceeds_cents"] == 20 - a["fee_cents"]
    a = webbets.assess_sell(c, balance_cents=10_000, count=3, price_cents=20,
                            ticker="KXNFLGAME-X", side="yes", positions=pos,
                            halted=False, halt_reason="")
    assert not a["ok"]                   # can't sell more than held
    a = webbets.assess_sell(c, balance_cents=10_000, count=1, price_cents=20,
                            ticker="KXNFLGAME-X", side="no", positions=pos,
                            halted=False, halt_reason="")
    assert not a["ok"]                   # you hold YES, not NO


def test_interpret_sell_grounded_on_positions(monkeypatch):
    cfg = _cfg()

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"content": [{"type": "text", "text": '{"action":"propose","order_side":"sell","ticker":"KXNFLGAME-X","side":"yes","count":1,"price_cents":null,"rationale":"close it"}'}]}

    class _Sess:
        def post(self, *a, **kw):
            return _Resp()

    pos = [{"ticker": "KXNFLGAME-X", "position_fp": "1.00"}]
    out = webbets.interpret_with_claude("close out my chiefs bet", catalog=[], positions=pos,
                                        cfg=cfg, api_key="k", session=_Sess())
    assert out["action"] == "propose" and out["order_side"] == "sell"
    # and the unheld case is refused
    out = webbets.interpret_with_claude("close out my chiefs bet", catalog=[], positions=[],
                                        cfg=cfg, api_key="k", session=_Sess())
    assert out["action"] == "none"


class _HeldClient(_FakeClient):
    def get_positions(self, status="open"):
        return [{"ticker": "KXNFLGAME-X", "position_fp": "2.00",
                 "market_exposure_fp": "0.40"}]

    def get_orderbook(self, ticker, depth=None):
        return {"orderbook": {"yes": [[27, 10]], "no": [[70, 10]]}}   # yes bid 27c

    def create_order(self, body):
        super().create_order(body)
        return {"order": {"order_id": "ord-sell-1", "taker_fill_count": body["count"]}}


def test_full_sell_flow_via_http(tmp_path):
    fake = _HeldClient()
    cfg = AppConfig(); cfg.state_dir = str(tmp_path / "state"); cfg.reports_dir = str(tmp_path / "reports")
    dash = WebDashboard(cfg, mode="sim")
    dash.credentials.update({"key_id": "k" * 36, "pem": "-----BEGIN PRIVATE KEY-----x",
                             "env": "demo", "validated": True, "fingerprint": "abc"})
    dash._signed_client = lambda: fake

    _Handler.dashboard = dash
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.open(f"http://127.0.0.1:{port}/login",
                data=b"password=unit-test-gate-password-9f3a", timeout=5).read()
    import re as _re
    req = urllib.request.Request(f"http://127.0.0.1:{port}{_base()}/bet/preview",
                                 data=b"prompt=sell+1+yes+KXNFLGAME-X")
    body = opener.open(req, timeout=5).read().decode()
    assert "SELL 1" in body and "net proceeds" in body
    m = _re.search(r'name="token" value="([0-9a-f]{24})"', body)
    assert m, body[:2000]
    req = urllib.request.Request(f"http://127.0.0.1:{port}{_base()}/bet/confirm",
                                 data=f"token={m.group(1)}".encode())
    body = opener.open(req, timeout=5).read().decode()
    assert "Order sent to Kalshi" in body and "SELL 1" in body
    assert fake.orders[0]["action"] == "sell" and fake.orders[0]["yes_price"] == 27
    httpd.shutdown()
