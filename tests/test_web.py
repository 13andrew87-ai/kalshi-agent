"""Web dashboard smoke tests: page renders, API works, agent loop runs."""

import json
import tempfile
import threading
import time
import urllib.request
import http.cookiejar
from http.server import ThreadingHTTPServer

from kalshi_agent.config import AppConfig
from kalshi_agent.web import WebDashboard, _Handler


def make_dashboard(tmp: str, **kw) -> WebDashboard:
    cfg = AppConfig()
    cfg.state_dir = tmp + "/state"
    cfg.reports_dir = tmp + "/reports"
    cfg.web_sim_poll_seconds = 0.3
    for k, v in kw.items():
        setattr(cfg, k, v)
    return WebDashboard(cfg, mode="sim")


def test_page_renders_and_state_api():
    with tempfile.TemporaryDirectory() as tmp:
        dash = make_dashboard(tmp)
        page = dash.render_page()
        assert "Kalshi Agent" in page and "SIMULATED EXCHANGE" in page
        assert "Start agent" in page
        state = dash.state()
        assert state["mode"] == "sim" and state["cycles"] == 0
        assert state["equity_cents"] == 100_000


def test_server_roundtrip_and_agent_loop():
    with tempfile.TemporaryDirectory() as tmp:
        dash = make_dashboard(tmp)
        _Handler.dashboard = dash
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        try:
            with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
                assert r.status == 200 and r.read() == b"ok"
            # sign in (password is case-sensitive: "unit-test-gate-password-9f3a")
            with opener.open(f"http://127.0.0.1:{port}/login",
                             data=b"password=unit-test-gate-password-9f3a", timeout=5) as r:
                assert r.status == 200 and b"Kalshi Agent" in r.read()
            with opener.open(f"http://127.0.0.1:{port}/", timeout=5) as r:
                assert r.status == 200 and b"Kalshi Agent" in r.read()

            dash.start()
            deadline = time.time() + 10
            while time.time() < deadline:
                if dash.state()["cycles"] >= 2:
                    break
                time.sleep(0.2)
            st = dash.state()
            assert st["cycles"] >= 2, "sim agent should cycle fast"
            assert st["running"] is True
            assert isinstance(st["signals"], list)

            # JSON API reflects the running agent
            with opener.open(f"http://127.0.0.1:{port}/api/state", timeout=5) as r:
                api = json.loads(r.read())
            assert api["cycles"] >= 2 and api["running"] is True

            dash.pause()
            assert dash.state()["running"] is False

            # POST control roundtrip: start again via HTTP (urllib follows the
            # 303 redirect back to "/" so the final status is 200)
            req = urllib.request.Request(f"http://127.0.0.1:{port}/start", data=b"", method="POST")
            with opener.open(req, timeout=5) as r:
                assert r.status in (200, 303)
            deadline = time.time() + 10
            while time.time() < deadline:
                if dash.state()["running"]:
                    break
                time.sleep(0.2)
            assert dash.state()["running"] is True
            dash.pause()
        finally:
            httpd.shutdown()


def test_reset_and_mode_switch():
    with tempfile.TemporaryDirectory() as tmp:
        dash = make_dashboard(tmp)
        dash.start()
        time.sleep(1.5)
        dash.pause()
        first = dash.state()
        assert first["cycles"] >= 1
        dash.reset()
        st = dash.state()
        assert st["cycles"] == 0 and st["running"] is False
        dash.set_mode("paper")     # builds a real-data world (no requests made until started)
        assert dash.state()["mode"] == "paper"
        dash.set_mode("sim")
        assert dash.state()["mode"] == "sim"
