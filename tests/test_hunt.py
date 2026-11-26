"""All-market hunt (user decree 2026-11-26): platform-wide catalog, bounded
time decree parser, validation rails, green/red chat coloring."""
import json
import time

import pytest

from kalshi_agent import webbets
from kalshi_agent.config import AppConfig
from kalshi_agent import web as webmod


# ------------------------------------------------------------- decree parser --
@pytest.mark.parametrize("text,expected", [
    ("maximize profits for 45 minutes", 45.0),
    ("maximize profits for 90 minutes", 90.0),
    ("hunt for 2 hours", 120.0),
    ("hunt for 1 hour", 60.0),
    ("trade for 30 min", 30.0),
    ("maximize 15 m", 15.0),
    ("maximize profits for 99 hours", 480.0),   # clamped to 8h ceiling
    ("hunt for 0.5 hours", 30.0),
])
def test_hunt_decree_parses(text, expected):
    assert webmod._parse_hunt_decree(text) == expected


@pytest.mark.parametrize("text", [
    "buy 1 yes KXNBA-26NOV25-LAL 22c",
    "funds",
    "what's the weather?",
    "maximize profits",          # decree requires an explicit duration
    "hunt for bananas",
])
def test_hunt_decree_rejects(text):
    assert webmod._parse_hunt_decree(text) is None


# ------------------------------------------------------------- chat colors --
def test_chat_colors_green_and_red():
    assert webmod._chat_color("app", "✅ Order sent. BUY 1 × YES") == "#22c55e"
    assert webmod._chat_color("app", "🎯 HUNT PICK (12m left): BUY") == "#22c55e"
    assert webmod._chat_color("app", "❌ Not sent — risk caps blocked") == "#f87171"
    assert webmod._chat_color("app", "🛑 Hunt stopped.") == "#f87171"
    assert webmod._chat_color("app", "plain agent line") == "#c4b5fd"
    assert webmod._chat_color("you", "✅ anything") == "#93c5fd"


# ---------------------------------------------------------- platform catalog --
def _mk(ticker, *, prov=False, ya=0.20, na=0.82, vol=100):
    return {
        "ticker": ticker, "title": f"Market {ticker}",
        "status": "open", "category": "General",
        "yes_ask_dollars": str(ya), "no_ask_dollars": str(na),
        "yes_bid_dollars": str(max(0.0, ya - 0.01)), "no_bid_dollars": str(max(0.0, na - 0.01)),
        "volume_fp": str(vol), "open_interest_fp": str(vol),
        "is_provisional": prov,
    }


class _FakeMarketClient:
    def __init__(self, rows):
        self._rows = rows

    def get_markets(self, status="open", limit=1000, max_pages=2, **kw):
        return list(self._rows)


def test_platform_catalog_filters_provisional_and_expensive():
    rows = [
        _mk("KXRAIN-CHEAP", ya=0.12, na=0.90, vol=500),
        _mk("KXNFL-PROV", prov=True, ya=0.10, na=0.90, vol=9999),
        _mk("KXNBA-EXPENSIVE", ya=0.85, na=0.20, vol=800),   # no cheap side? 0.20 yes is cheap... make truly expensive:
        _mk("KXNHL-ALLMID", ya=0.55, na=0.48, vol=300),      # neither side <= 30c -> excluded
        _mk("KXEPL-BIGVOL", ya=0.25, na=0.77, vol=10000),
    ]
    rows[2] = _mk("KXNBA-EXPENSIVE", ya=0.85, na=0.78, vol=800)  # both sides >= 30
    cat = webbets.platform_catalog(_FakeMarketClient(rows), max_total=10)
    tickers = [c["ticker"] for c in cat]
    assert "KXRAIN-CHEAP" in tickers
    assert "KXEPL-BIGVOL" in tickers
    assert "KXNFL-PROV" not in tickers
    assert "KXNBA-EXPENSIVE" not in tickers
    assert "KXNHL-ALLMID" not in tickers
    # sorted by volume desc
    assert tickers.index("KXEPL-BIGVOL") < tickers.index("KXRAIN-CHEAP")


# ---------------------------------------------------------------- hunt flow --
_GROQ_OK = {"choices": [{"message": {"content": '{"action":"propose","ticker":"KXRAIN-CHEAP","side":"yes","count":1,"price_cents":null,"rationale":"mispriced rain odds"}'}}]}


class _GroqSession:
    def __init__(self, payload):
        self.payload = payload

    def post(self, url, **kw):
        assert "groq" in url
        class R:
            status_code = 200
            text = ""
            def json(inner):
                return self.payload
        return R()


def test_hunt_proposes_known_ticker(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    for k in ("ANTHROPIC_API_KEY", "LLM_API_KEY", "LLM_PROVIDER", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = AppConfig()
    rows = [_mk("KXRAIN-CHEAP", ya=0.12, na=0.90, vol=500)]
    out = webbets.hunt_best_bet(_FakeMarketClient(rows), cfg=cfg, balance_cents=300,
                                positions=[], session=_GroqSession(_GROQ_OK))
    assert out["action"] == "propose" and out["ticker"] == "KXRAIN-CHEAP"
    assert out["order_side"] == "buy" and out["count"] == 1


def test_hunt_rejects_hallucinated_ticker(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    for k in ("ANTHROPIC_API_KEY", "LLM_API_KEY", "LLM_PROVIDER", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = AppConfig()
    bad = {"choices": [{"message": {"content": '{"action":"propose","ticker":"KXFAKE-NOPE","side":"yes","count":1,"price_cents":null,"rationale":"nope"}'}}]}
    rows = [_mk("KXRAIN-CHEAP", ya=0.12, na=0.90, vol=500)]
    out = webbets.hunt_best_bet(_FakeMarketClient(rows), cfg=cfg, balance_cents=300,
                                positions=[], session=_GroqSession(bad))
    assert out["action"] == "none"
    assert "unknown ticker" in out["message"]


# ------------------------------------------------------ combo (MVE) opt-in --
def test_platform_catalog_combos_opt_in(monkeypatch):
    rows = [
        _mk("KXRAIN-CHEAP", ya=0.12, na=0.90, vol=500),
        _mk("KXMVECROSSCATEGORY-S1-BABB", prov=True, ya=0.14, na=0.88, vol=777),
        _mk("KXMVECROSSCATEGORY-S1-DEAD", prov=True, ya=0.15, na=0.87, vol=0),
    ]

    class _ComboClient:
        def get_markets(self, status="open", limit=1000, max_pages=2, mve_filter=None, **kw):
            return [r for r in rows if (r.get("is_provisional") == (mve_filter == "only"))]

    # combos OFF (default): regular feed only, provisional never admitted
    cat = webbets.platform_catalog(_ComboClient(), max_total=10, allow_combos=False)
    assert [c["ticker"] for c in cat] == ["KXRAIN-CHEAP"]
    # combos ON: liquid combo admitted, dead combo still out
    cat = webbets.platform_catalog(_ComboClient(), max_total=10, allow_combos=True)
    tickers = [c["ticker"] for c in cat]
    assert "KXMVECROSSCATEGORY-S1-BABB" in tickers
    assert "KXMVECROSSCATEGORY-S1-DEAD" not in tickers
    assert "KXRAIN-CHEAP" in tickers
