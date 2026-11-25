"""AI-managed exits overlay: hard floor, fallback semantics, rate limits."""

import time

from kalshi_agent.config import AppConfig
from kalshi_agent.ai_exits import AIExitAdvisor
from kalshi_agent.models import Intent
from kalshi_agent.strategy import Strategy


class FakeMarket:
    ticker = "KXTEST-1"
    title = "Fake market title"
    mid_cents = 50
    status = "open"

    def hours_to_close(self):
        return 48.0


class FakeLevel:
    def __init__(self, p):
        self.price_cents = p
        self.quantity = 10


class FakeBook:
    def __init__(self, bid_yes=None, bid_no=None):
        self._b = {"yes": bid_yes, "no": bid_no}

    def best_bid(self, side):
        p = self._b.get(side)
        return FakeLevel(p) if p is not None else None


class FakePrediction:
    p_blend = 0.62


def _cfg():
    c = AppConfig()
    c.stop_loss_cents = 15
    c.take_profit_cents = 5
    c.min_edge_cents = 8
    c.ai_exits_enabled = True
    c.ai_exit_poll_minutes = 5
    return c


def _resp(payload):
    class R:
        status_code = 200
        text = ""

        def json(self):
            return {"content": [{"type": "text", "text": payload}]}
    return R()


class FakeSession:
    def __init__(self, payload=None, status=200):
        self.calls = 0
        self.payload = payload or '{"action":"hold","confidence":"high","reason":"edge intact"}'
        self.status = status

    def post(self, *a, **kw):
        self.calls += 1
        if self.status != 200:
            class R:
                status_code = self.status
                text = "boom"

                def json(self):
                    return {"error": {"message": "credit balance too low"}}
            return R()
        return _resp(self.payload)


def _strategy_intent(cfg):
    """The fixed TAKE-PROFIT intent the strategy would produce."""
    s = Strategy(cfg)
    return Intent(ticker="KXTEST-1", side="yes", action="sell", count=1,
                  price_cents=45, rationale="TAKE-PROFIT: mark 45c >= entry+5c")


def test_hard_floor_never_reaches_llm():
    cfg = _cfg()
    sess = FakeSession('{"action":"hold","confidence":"high","reason":"hold"}')
    adv = AIExitAdvisor(cfg, api_key="k", session=sess, min_interval_minutes=0.001)
    intent = adv.decide(market=FakeMarket(), book=FakeBook(bid_yes=20),
                        prediction=FakePrediction(), intent=None,
                        position_yes=1, position_no=0, avg_price_cents=40.0)
    assert intent is not None and intent.action == "sell"
    assert "STOP" in intent.rationale
    assert sess.calls == 0                     # LLM was not consulted


def test_strategy_hard_stop_passthrough_without_llm():
    cfg = _cfg()
    sess = FakeSession()
    adv = AIExitAdvisor(cfg, api_key="k", session=sess, min_interval_minutes=0.001)
    # bid 26 is above the -15c floor (pnl -14c) so the floor layer doesn't fire;
    # the strategy's own STOP intent must pass through untouched.
    stop = Intent(ticker="KXTEST-1", side="yes", action="sell", count=1,
                  price_cents=26, rationale="STOP: mark 26c <= entry 40c - 15c (strategy math)")
    out = adv.decide(market=FakeMarket(), book=FakeBook(bid_yes=26),
                     prediction=FakePrediction(), intent=stop,
                     position_yes=1, position_no=0, avg_price_cents=40.0)
    assert out is stop
    assert sess.calls == 0


def test_ai_hold_overrides_take_profit():
    cfg = _cfg()
    sess = FakeSession('{"action":"hold","confidence":"high","reason":"momentum intact, fair value higher"}')
    adv = AIExitAdvisor(cfg, api_key="k", session=sess, min_interval_minutes=0.001)
    tp = _strategy_intent(cfg)
    out = adv.decide(market=FakeMarket(), book=FakeBook(bid_yes=45),
                     prediction=FakePrediction(), intent=tp,
                     position_yes=1, position_no=0, avg_price_cents=40.0)
    assert out is None                          # sale suppressed
    assert sess.calls == 1


def test_ai_sell_when_strategy_silent():
    cfg = _cfg()
    sess = FakeSession('{"action":"sell","confidence":"medium","reason":"edge fully priced in, momentum stalling"}')
    adv = AIExitAdvisor(cfg, api_key="k", session=sess, min_interval_minutes=0.001)
    out = adv.decide(market=FakeMarket(), book=FakeBook(bid_yes=44),
                     prediction=FakePrediction(), intent=None,
                     position_yes=2, position_no=0, avg_price_cents=40.0)
    assert out is not None and out.action == "sell" and out.count == 2
    assert out.rationale.startswith("AI-EXIT")


def test_low_confidence_defers_to_fixed_rules():
    cfg = _cfg()
    sess = FakeSession('{"action":"sell","confidence":"low","reason":"torn"}')
    adv = AIExitAdvisor(cfg, api_key="k", session=sess, min_interval_minutes=0.001)
    tp = _strategy_intent(cfg)
    out = adv.decide(market=FakeMarket(), book=FakeBook(bid_yes=45),
                     prediction=FakePrediction(), intent=tp,
                     position_yes=1, position_no=0, avg_price_cents=40.0)
    assert out is tp                            # unchanged fixed behavior


def test_credit_outage_degrades_to_fixed_rules():
    cfg = _cfg()
    sess = FakeSession(status=400)
    adv = AIExitAdvisor(cfg, api_key="k", session=sess, min_interval_minutes=0.001)
    tp = _strategy_intent(cfg)
    out = adv.decide(market=FakeMarket(), book=FakeBook(bid_yes=45),
                     prediction=FakePrediction(), intent=tp,
                     position_yes=1, position_no=0, avg_price_cents=40.0)
    assert out is tp                            # fixed rules survive API failure
    assert sess.calls == 1


def test_rate_limit_skips_llm_between_checks():
    cfg = _cfg()
    sess = FakeSession('{"action":"hold","confidence":"high","reason":"hold"}')
    adv = AIExitAdvisor(cfg, api_key="k", session=sess, min_interval_minutes=5)
    tp = _strategy_intent(cfg)
    out1 = adv.decide(market=FakeMarket(), book=FakeBook(bid_yes=45),
                      prediction=FakePrediction(), intent=tp,
                      position_yes=1, position_no=0, avg_price_cents=40.0)
    assert out1 is None                         # AI suppressed the sale
    out2 = adv.decide(market=FakeMarket(), book=FakeBook(bid_yes=45),
                      prediction=FakePrediction(), intent=tp,
                      position_yes=1, position_no=0, avg_price_cents=40.0)
    assert out2 is tp                           # fixed rules govern between polls
    assert sess.calls == 1


def test_no_api_key_falls_back_loudly(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
              "CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "OPENAI_API_KEY",
              "LLM_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    cfg = _cfg()
    adv = AIExitAdvisor(cfg, api_key=None, session=FakeSession(), min_interval_minutes=0.001)
    tp = _strategy_intent(cfg)
    out = adv.decide(market=FakeMarket(), book=FakeBook(bid_yes=45),
                     prediction=FakePrediction(), intent=tp,
                     position_yes=1, position_no=0, avg_price_cents=40.0)
    assert out is tp
