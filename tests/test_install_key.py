"""install_credentials + external-reload tests."""

import json
import os
import tempfile

from kalshi_agent.auth import generate_keypair
from kalshi_agent.config import AppConfig
from kalshi_agent.web import WebDashboard, install_credentials


def make_cfg(tmp: str) -> AppConfig:
    cfg = AppConfig()
    cfg.state_dir = os.path.join(tmp, "state")
    cfg.reports_dir = os.path.join(tmp, "reports")
    return cfg


def test_install_credentials_persists_and_dashboard_picks_it_up():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = make_cfg(tmp)
        _, priv = generate_keypair()
        ok, msg = install_credentials(cfg, "chat-key-id", priv, "demo",
                                      validator=lambda *a: (True, ""))
        assert ok, msg
        d = os.path.join(cfg.state_dir, "web", "credentials")
        assert os.path.exists(os.path.join(d, "kalshi_private_key.pem"))
        assert os.path.exists(os.path.join(d, "meta.json"))
        with open(os.path.join(d, "meta.json")) as fh:
            meta = json.load(fh)
        assert meta == {"key_id": "chat-key-id", "env": "demo"}

        dash = WebDashboard(cfg, mode="sim")
        assert dash.credentials["key_id"] == "chat-key-id"
        assert dash.credentials["validated"] is False   # not yet probed in-session


def test_dashboard_reloads_when_new_key_appears_on_disk():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = make_cfg(tmp)
        dash = WebDashboard(cfg, mode="sim")
        assert dash.credentials["key_id"] == ""
        _, priv1 = generate_keypair()
        _, priv2 = generate_keypair()
        assert priv1 != priv2

        ok, _ = install_credentials(cfg, "key-one", priv1, "demo",
                                    validator=lambda *a: (True, ""))
        assert ok
        dash._reload_credentials_if_changed()
        assert dash.credentials["key_id"] == "key-one"

        ok, _ = install_credentials(cfg, "key-two", priv2, "live",
                                    validator=lambda *a: (True, ""))
        assert ok
        dash._reload_credentials_if_changed()
        assert dash.credentials["key_id"] == "key-two"
        assert dash.credentials["env"] == "live"
        assert dash.cfg.use_demo_env is False


def test_install_rejects_bad_input():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = make_cfg(tmp)
        ok, msg = install_credentials(cfg, "", "x", "demo", validator=lambda *a: (True, ""))
        assert not ok and "key id" in msg
        ok, msg = install_credentials(cfg, "kid", "not pem", "demo", validator=lambda *a: (True, ""))
        assert not ok and "PEM" in msg
