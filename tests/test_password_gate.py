"""Password gate: every app route requires the shared password (case-sensitive).

Two auth paths are supported:
  1. URL path token (/app/<token>/...) — required because the preview gateway
     strips Cookie and Authorization headers inbound
  2. HTTP Basic / session cookie — works on direct access (localhost)
"""

import base64
import http.client
import tempfile
import threading
from http.server import ThreadingHTTPServer

import pytest

from kalshi_agent.config import AppConfig
from kalshi_agent.web import (WebDashboard, _Handler, _session_token,
                              _SESSION_COOKIE, _path_token, _base)


@pytest.fixture()
def server():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = AppConfig()
        cfg.state_dir = tmp + "/state"
        cfg.reports_dir = tmp + "/reports"
        dash = WebDashboard(cfg, mode="sim")
        _Handler.dashboard = dash
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            yield httpd.server_address[1], dash
        finally:
            httpd.shutdown()
            dash.pause()


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def req(port, method, path, body=None, cookie=None, authorization=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = cookie
    if authorization:
        headers["Authorization"] = authorization
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), data


def test_healthz_open_without_password(server):
    port, _ = server
    status, _, body = req(port, "GET", "/healthz")
    assert status == 200 and body == b"ok"


def test_root_shows_unlock_form_when_signed_out(server):
    port, _ = server
    status, _, body = req(port, "GET", "/")
    assert status == 200 and b"password" in body.lower()


def test_api_state_requires_auth(server):
    port, _ = server
    status, _, body = req(port, "GET", "/api/state")
    assert status == 401


def test_private_key_download_blocked_when_signed_out(server):
    port, _ = server
    status, headers, _ = req(port, "GET", "/credentials/private.pem")
    assert status == 303 and headers.get("Location") == "/"


def test_basic_auth_any_username_correct_password(server):
    port, _ = server
    for user in ("app", "wrong-user", "admin", ""):
        status, _, body = req(port, "GET", "/api/state", authorization=basic(user, "unit-test-gate-password-9f3a"))
        assert status == 200 and b"equity_cents" in body, user


def test_basic_auth_password_case_sensitive(server):
    port, _ = server
    pw = "unit-test-gate-password-9f3a"
    wrongs = (pw.upper(), pw.title(), pw[:-1], pw + "x", pw + " ", pw[:5], pw.replace("-", "_"))
    for wrong in dict.fromkeys(wrongs):
        if wrong == pw:
            continue
        status, _, _ = req(port, "GET", "/api/state", authorization=basic("app", wrong))
        assert status == 401, wrong


def test_prefixed_url_grants_access_without_headers(server):
    port, _ = server
    status, _, body = req(port, "GET", _base() + "/")
    assert status == 200 and b"Kalshi Agent" in body
    status, _, body = req(port, "GET", _base() + "/api/state")
    assert status == 200 and b"equity_cents" in body


def test_prefixed_url_unlocks_credentials_page(server):
    port, _ = server
    status, _, body = req(port, "GET", _base() + "/credentials")
    assert status == 200 and b"API Credentials" in body


def test_prefixed_post_actions_work(server):
    port, _ = server
    status, headers, _ = req(port, "POST", _base() + "/start")
    assert status == 303 and headers.get("Location") == _base() + "/"


def test_wrong_url_token_rejected(server):
    port, _ = server
    status, headers, _ = req(port, "GET", "/app/wrongtoken123/")
    assert status == 303 and headers.get("Location") == "/"
    status, headers, _ = req(port, "GET", "/app/wrongtoken123/api/state")
    assert status == 303
    status, headers, _ = req(port, "POST", "/app/wrongtoken123/start")
    assert status == 303


def test_wrong_password_rejected_login_form(server):
    port, _ = server
    status, headers, _ = req(port, "POST", "/login", b"password=hunter2")
    assert status == 303 and headers.get("Location") == "/?error=1"
    assert "Set-Cookie" not in headers


def test_password_is_case_sensitive_login_form(server):
    port, _ = server
    pw = "unit-test-gate-password-9f3a"
    wrongs = (pw.upper(), pw.title(), pw[:-1], pw + "x", pw[:5], pw.replace("-", "_"))
    for wrong in dict.fromkeys(wrongs):
        if wrong == pw:
            continue
        status, headers, _ = req(port, "POST", "/login", f"password={wrong}".encode())
        assert headers.get("Location") == "/?error=1", wrong


def test_correct_password_redirects_to_prefixed_url_and_sets_cookie(server):
    port, _ = server
    status, headers, _ = req(port, "POST", "/login", b"password=unit-test-gate-password-9f3a")
    assert status == 303 and headers.get("Location") == _base() + "/"
    cookie = headers["Set-Cookie"].split(";")[0]
    assert cookie.startswith(_SESSION_COOKIE + "=")
    assert "HttpOnly" in headers["Set-Cookie"]


def test_cookie_session_still_works(server):
    port, _ = server
    cookie = f"{_SESSION_COOKIE}={_session_token()}"
    status, _, body = req(port, "GET", "/api/state", cookie=cookie)
    assert status == 200 and b"equity_cents" in body


def test_prefixed_api_state_cookie_not_needed(server):
    port, _ = server
    status, _, body = req(port, "GET", _base() + "/api/state", cookie=None)
    assert status == 200


def test_logout_clears_session(server):
    port, _ = server
    status, headers, _ = req(port, "POST", "/login", b"password=unit-test-gate-password-9f3a")
    cookie = headers["Set-Cookie"].split(";")[0]
    status, headers, _ = req(port, "POST", "/logout", cookie=cookie)
    assert status == 303 and "Max-Age=0" in headers.get("Set-Cookie", "")


def test_path_token_stable_and_secret():
    assert _path_token() == _path_token()
    assert len(_path_token()) == 32
    assert _path_token() != "unit-test-gate-password-9f3a"


def test_security_headers_present(server):
    port, _ = server
    status, headers, _ = req(port, "GET", "/")
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("Strict-Transport-Security") == "max-age=63072000"
    assert headers.get("X-Frame-Options") == "DENY"
    assert headers.get("Referrer-Policy") == "no-referrer"


def test_session_cookie_is_httponly_samesite(server):
    port, _ = server
    status, headers, _ = req(port, "POST", "/login", b"password=unit-test-gate-password-9f3a")
    sc = headers.get("Set-Cookie", "")
    assert "HttpOnly" in sc and "SameSite=Lax" in sc
