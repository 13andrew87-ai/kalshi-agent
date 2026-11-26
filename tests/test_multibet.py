"""Plain-English multi-bet batches: 'make four 75 cent bets ... most profitable'."""
import json

import pytest

from kalshi_agent import webbets
from kalshi_agent.config import AppConfig
from kalshi_agent.web import WebDashboard, _Handler


CFG = AppConfig()
CATALOG = [{"ticker": f"KXTEST-{i}", "title": f"Test market {i}"} for i in range(6)]

MULTI_PAYLOAD = {
    "choices": [{"message": {"content": json.dumps({
        "action": "multi", "stake_cents": 75, "note": "four cheap asymmetry legs",
        "legs": [
            {"order_side": "buy", "ticker": "KXTEST-0", "side": "yes", "count": 2, "price_cents": None, "rationale": "cheap"},
            {"order_side": "buy", "ticker": "KXTEST-1", "side": "no", "count": 2, "price_cents": None, "rationale": "mispriced"},
            {"order_side": "buy", "ticker": "KXFAKE-99", "side": "yes", "count": 1, "price_cents": None, "rationale": "hallucinated"},
            {"order_side": "buy", "ticker": "KXTEST-2", "side": "yes", "count": 9, "price_cents": None, "rationale": "clamp me"},
        ]})}}]
}


class _Session:
    def post(self, url, **kw):
        class R:
            status_code = 200
            text = ""
            def json(self):
                return MULTI_PAYLOAD
        return R()


def test_interpret_multi_validates_and_clamps(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    for k in ("ANTHROPIC_API_KEY", "LLM_API_KEY", "LLM_PROVIDER", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    out = webbets.interpret_with_claude(
        "Make four 75 cent bets across most likely profitable markets",
        catalog=CATALOG, cfg=CFG, api_key=None, session=_Session())
    assert out["action"] == "multi"
    assert out["stake_cents"] == 75
    tickers = [l["ticker"] for l in out["legs"]]
    assert "KXFAKE-99" not in tickers          # hallucinated leg rejected
    assert "KXTEST-0" in tickers and "KXTEST-1" in tickers
    assert all(l["count"] <= CFG.max_contracts_per_order for l in out["legs"])
    assert len(out["legs"]) == 3


def _account_dash(tmp):
    cfg = AppConfig()
    cfg.state_dir = tmp + "/state"
    cfg.reports_dir = tmp + "/reports"
    return WebDashboard(cfg, mode="account")


def test_batch_stage_queue_and_advance(monkeypatch, tmp_path):
    dash = _account_dash(str(tmp_path))

    fake_bets = {}
    def fake_stage_order(action, *, ticker, side, count, price_cents, rationale=""):
        ask = {"KXTEST-0": 12, "KXTEST-1": 18}[ticker]
        price_cents = price_cents or ask
        return {"ok": True, "bet": {
            "action": "buy", "ticker": ticker, "title": f"mkt {ticker}",
            "side": side, "count": count, "price_cents": price_cents,
            "fee_cents": 2, "cost_cents": count * price_cents + 2,
            "balance_before_cents": 300, "rationale": rationale, "env": "live",
            "checks": [], "checks_ok": True, "created": 1.0}}
    monkeypatch.setattr(dash, "stage_order", fake_stage_order)

    class FakeHandler:
        dashboard = dash
        def __init__(self):
            self.dashboard = dash
        _stage_multi = None  # replaced below

    from kalshi_agent.web import _Handler as RealHandler
    handler = type("H", (), {})()
    handler.dashboard = dash
    handler._stage_multi = RealHandler._stage_multi.__get__(handler, RealHandler)
    handler._advance_batch = RealHandler._advance_batch.__get__(handler, RealHandler)
    handler._batch_prompt = RealHandler._batch_prompt.__get__(handler, RealHandler)
    handler._batch_outcome = RealHandler._batch_outcome.__get__(handler, RealHandler)

    plan = {"action": "multi", "stake_cents": 75, "note": "two legs",
            "legs": [
                {"order_side": "buy", "ticker": "KXTEST-0", "side": "yes", "count": 2, "price_cents": None, "rationale": "cheap"},
                {"order_side": "buy", "ticker": "KXTEST-1", "side": "no", "count": 2, "price_cents": None, "rationale": "mispriced"},
            ]}
    handler._stage_multi(plan)
    assert dash._bet_batch is not None and dash._bet_batch["staged"] == 2
    assert dash._chat_pending is not None
    # each pending leg got an extended 10-minute TTL
    for tok, bet in dash._pending_bets.items():
        assert bet["ttl_s"] == 600
    first = dash._chat_pending
    # simulate YES on bet 1 -> batch advances, chat asks for bet 2
    dash._chat_pending = None
    dash.pending_pop(first)               # pretend confirm happened
    handler._batch_outcome("filled", "KXTEST-0 (2/2 filled)")
    handler._advance_batch()
    assert dash._bet_batch["done"] == 1
    assert dash._chat_pending is not None and dash._chat_pending != first
    # simulate NO on bet 2 -> batch completes and recaps
    second = dash._chat_pending
    dash._chat_pending = None
    dropped = dash.pending_pop(second)
    handler._batch_outcome("passed", dropped["ticker"])
    handler._advance_batch()
    assert dash._bet_batch is None
    assert any("Batch complete" in t for _, t in dash._chat)


def test_batch_stake_shrinks_or_skips(monkeypatch, tmp_path):
    dash = _account_dash(str(tmp_path))
    calls = []

    def fake_stage_order(action, *, ticker, side, count, price_cents, rationale=""):
        calls.append(count)
        price_cents = price_cents or 12
        cost = count * price_cents + 2
        return {"ok": True, "bet": {
            "action": "buy", "ticker": ticker, "title": "mkt", "side": side,
            "count": count, "price_cents": price_cents, "fee_cents": 2,
            "cost_cents": cost, "balance_before_cents": 300, "rationale": rationale,
            "env": "live", "checks": [], "checks_ok": True, "created": 1.0}}
    monkeypatch.setattr(dash, "stage_order", fake_stage_order)

    from kalshi_agent.web import _Handler as RealHandler
    handler = type("H", (), {})()
    handler.dashboard = dash
    for name in ("_stage_multi", "_advance_batch", "_batch_prompt", "_batch_outcome"):
        setattr(handler, name, getattr(RealHandler, name).__get__(handler, RealHandler))

    plan = {"action": "multi", "stake_cents": 30, "note": "",
            "legs": [{"order_side": "buy", "ticker": "KXTEST-0", "side": "yes",
                      "count": 2, "price_cents": None, "rationale": "x"}]}
    handler._stage_multi(plan)
    # 2 contracts cost 26c ≤ 30c — no shrinking needed
    assert calls == [2]
    assert dash._bet_batch["staged"] == 1
