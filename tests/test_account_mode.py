"""Account mode (real-money console) + chat YES/NO confirm flow."""

import re
import pytest
import tempfile
import threading
import http.cookiejar
import urllib.request
from http.server import ThreadingHTTPServer

from kalshi_agent.config import AppConfig
from kalshi_agent.web import WebDashboard, _Handler, _base


class FakeClient:
    def __init__(self):
        self.orders = []
        self.balance = 10_000

    def get_balance(self):
        return {"balance": self.balance}

    def get_positions(self, status="open"):
        return [{"ticker": "KXNFLGAME-X", "position_fp": "2.00",
                 "market_exposure_fp": "0.50"}]

    def get_fills(self, min_ts=None, limit=8):
        return [{"ticker": "KXNFLGAME-X", "action": "buy", "side": "yes",
                 "count_fp": "2.00", "yes_price": 25, "fee_cost": "0.04",
                 "created_time": "2026-11-24T18:00:00Z"}]

    def get_market(self, ticker):
        return {"ticker": ticker, "status": "open", "title": "Fake Market"}

    def get_orderbook(self, ticker, depth=None):
        return {"orderbook": {"yes": [[25, 10]], "no": [[72, 10]]}}

    def create_order(self, body):
        self.orders.append(body)
        leg_cents = float(body["price"]) * 100   # v2: YES-leg price in fixed-point dollars
        cnt = float(body["count"])
        is_credit = (body["side"] == "ask")      # ask = selling YES == taking money in
        self.balance += int(cnt * leg_cents) if is_credit else -int(cnt * leg_cents) - 1
        return {"order_id": "ord-acct-1", "fill_count": str(body["count"]), "remaining_count": "0.00", "ts_ms": 1}


def make_dash(tmp_dir):
    cfg = AppConfig()
    cfg.state_dir = str(tmp_dir) + "/state"
    cfg.reports_dir = str(tmp_dir) + "/reports"
    dash = WebDashboard(cfg, mode="account")
    dash.credentials.update({"key_id": "k" * 36, "pem": "-----BEGIN PRIVATE KEY-----x",
                             "env": "demo", "validated": True, "fingerprint": "abc"})
    fake = FakeClient()
    dash._signed_client = lambda: fake
    return dash, fake


def http_session(dash):
    _Handler.dashboard = dash
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.open(f"http://127.0.0.1:{port}/login",
                data=b"password=unit-test-gate-password-9f3a", timeout=5).read()
    return httpd, opener, port


def test_account_mode_has_no_sim_ui():
    with tempfile.TemporaryDirectory() as tmp:
        dash, _ = make_dash(tmp)
        # state must not crash without an agent world
        s = dash.state()
        assert s["mode"] == "account" and s["running"] is False
        assert s["funds"]["cents"] == 10_000
        page = dash.render_page()
        assert "TOTAL FUNDS" in page
        assert "MANUAL ONLY" in page
        assert "ASK TO PLACE A BET" in page
        assert "SIMULATED EXCHANGE" not in page
        assert "PAPER" not in page
        assert "Start agent" not in page
        assert "SIGNALS" not in page.upper().replace("ASK TO PLACE A BET", "")
        assert "KXNFLGAME-X" in page          # live position row
        assert "RECENT FILLS" in page


def test_account_mode_unvalidated_shows_banner():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = AppConfig()
        cfg.state_dir = str(tmp) + "/state"; cfg.reports_dir = str(tmp) + "/reports"
        dash = WebDashboard(cfg, mode="account")
        page = dash.render_page()
        assert "TOTAL FUNDS" in page and "Credentials not validated" in page


def test_chat_yes_flow_places_order():
    with tempfile.TemporaryDirectory() as tmp:
        dash, fake = make_dash(tmp)
        httpd, opener, port = http_session(dash)
        base = _base()
        r = opener.open(f"http://127.0.0.1:{port}{base}/dashboard", timeout=5).read().decode()
        assert "ASK TO PLACE A BET" in r and "MANUAL ONLY" in r
        # propose
        req = urllib.request.Request(f"http://127.0.0.1:{port}{base}/bet/chat",
                                     data=b"prompt=buy+1+yes+KXNFLGAME-X+25c")
        opener.open(req, timeout=5).read()
        assert dash._chat_pending is not None
        # confirm YES
        req = urllib.request.Request(f"http://127.0.0.1:{port}{base}/bet/chat",
                                     data=b"prompt=yes")
        body = opener.open(req, timeout=5).read().decode()
        assert fake.orders and fake.orders[0]["side"] == "bid"
        assert float(fake.orders[0]["price"]) == pytest.approx(0.25)
        assert dash._chat_pending is None
        assert "TOTAL FUNDS now" in body
        httpd.shutdown()


def test_chat_no_flow_cancels():
    with tempfile.TemporaryDirectory() as tmp:
        dash, fake = make_dash(tmp)
        httpd, opener, port = http_session(dash)
        base = _base()
        req = urllib.request.Request(f"http://127.0.0.1:{port}{base}/bet/chat",
                                     data=b"prompt=buy+1+yes+KXNFLGAME-X+25c")
        opener.open(req, timeout=5).read()
        req = urllib.request.Request(f"http://127.0.0.1:{port}{base}/bet/chat",
                                     data=b"prompt=NO")
        body = opener.open(req, timeout=5).read().decode()
        assert not fake.orders
        assert "Cancelled" in body
        assert dash._chat_pending is None
        httpd.shutdown()


def test_chat_caps_block_shows_failed_checks():
    with tempfile.TemporaryDirectory() as tmp:
        dash, fake = make_dash(tmp)
        httpd, opener, port = http_session(dash)
        base = _base()
        # live price band caps entries at 30c — 45c must be refused by caps
        req = urllib.request.Request(f"http://127.0.0.1:{port}{base}/bet/chat",
                                     data=b"prompt=buy+1+yes+KXNFLGAME-X+45c")
        body = opener.open(req, timeout=5).read().decode()
        assert not fake.orders
        assert "Could not stage that" in body
        httpd.shutdown()


def test_chat_funds_command():
    with tempfile.TemporaryDirectory() as tmp:
        dash, _ = make_dash(tmp)
        httpd, opener, port = http_session(dash)
        base = _base()
        req = urllib.request.Request(f"http://127.0.0.1:{port}{base}/bet/chat",
                                     data=b"prompt=funds")
        body = opener.open(req, timeout=5).read().decode()
        assert "TOTAL FUNDS" in body
        httpd.shutdown()
