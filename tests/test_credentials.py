"""Credentials page + save/generate/clear flow tests (validator stubbed)."""

import tempfile

from kalshi_agent.auth import generate_keypair
from kalshi_agent.web import WebDashboard


def make_dash(tmp: str) -> WebDashboard:
    cfg = AppConfig_()
    cfg.state_dir = tmp + "/state"
    cfg.reports_dir = tmp + "/reports"
    return WebDashboard(cfg, mode="sim")


def AppConfig_():
    from kalshi_agent.config import AppConfig

    return AppConfig()


def test_credentials_page_renders_empty():
    with tempfile.TemporaryDirectory() as tmp:
        dash = make_dash(tmp)
        page = dash.render_page.__self__ if False else None  # noqa: F841
        from kalshi_agent.web import render_credentials

        page = render_credentials(dash)
        assert "API Credentials" in page
        assert "no credentials configured" in page
        assert "/credentials/save" in page


def test_generate_keypair_shows_pems_and_download():
    with tempfile.TemporaryDirectory() as tmp:
        dash = make_dash(tmp)
        dash.generate_keypair()
        assert "BEGIN PRIVATE KEY" in dash.credentials["generated_private_pem"]
        assert "BEGIN PUBLIC KEY" in dash.credentials["generated_public_pem"]
        from kalshi_agent.web import render_credentials

        page = render_credentials(dash)
        assert "/credentials/private.pem" in page and "PUBLIC KEY" in page


def test_save_rejects_garbage_input():
    with tempfile.TemporaryDirectory() as tmp:
        dash = make_dash(tmp)
        ok, msg = dash.save_credentials("", "", "demo")
        assert not ok and "key id" in msg
        ok, msg = dash.save_credentials("kid", "not a pem", "demo")
        assert not ok and "PEM" in msg


def test_save_with_stub_validator_success_and_failure():
    with tempfile.TemporaryDirectory() as tmp:
        dash = make_dash(tmp)
        pub, priv = generate_keypair()
        dash.validator = lambda kid, pem, base: (base.endswith("trade-api/v2"), "")
        ok, msg = dash.save_credentials("test-key-id", priv, "demo", persist=True)
        assert ok, msg
        assert dash.credentials["validated"] is True
        assert dash.cfg.api_key_id == "test-key-id"
        assert dash.cfg.use_demo_env is True
        # persisted to disk with tight perms
        import os

        pem_path = dash.cfg.private_key_path
        assert os.path.exists(pem_path)
        assert oct(os.stat(pem_path).st_mode & 0o777) == "0o600"
        # reloaded on a fresh dashboard instance
        dash2 = WebDashboard(dash.cfg, mode="sim")
        assert dash2.credentials["key_id"] == "test-key-id"

        # failure path
        dash.validator = lambda kid, pem, base: (False, "rejected by stub")
        ok, msg = dash.save_credentials("other", priv, "live")
        assert not ok and "stub" in msg


def test_clear_credentials():
    with tempfile.TemporaryDirectory() as tmp:
        dash = make_dash(tmp)
        _, priv = generate_keypair()
        dash.validator = lambda *a: (True, "")
        dash.save_credentials("kid", priv, "demo", persist=True)
        dash.clear_credentials()
        assert dash.credentials["key_id"] == "" and not dash.credentials["pem"]
        import os

        assert not os.path.exists(dash._cred_dir())


def test_signed_probe_against_real_demo_endpoint_fails_cleanly():
    """Garbage key id + valid PEM must produce a clean error, not a crash.
    (Runs only if the network is reachable; skipped otherwise.)"""
    import requests

    try:
        requests.get("https://demo.elections.kalshi.com/trade-api/v2/exchange/status", timeout=4)
    except Exception:
        print("network unreachable — skipping live probe test")
        return
    with tempfile.TemporaryDirectory() as tmp:
        dash = make_dash(tmp)
        _, priv = generate_keypair()
        ok, msg = dash.save_credentials("bogus-key-id-000", priv, "demo")
        assert not ok
        assert ("401" in msg or "403" in msg or "rejected" in msg.lower())
