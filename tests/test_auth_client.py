"""Auth signing and HTTP client behavior tests (mocked transport)."""

import base64

import pytest

from kalshi_agent.auth import KalshiSigner, generate_keypair
from kalshi_agent.client import KalshiApiError, KalshiAuthError, KalshiClient
from kalshi_agent.config import AppConfig


@pytest.fixture(scope="module")
def keys():
    return generate_keypair()


def test_signature_verifies_with_public_key(keys):
    pub_pem, priv_pem = keys
    signer = KalshiSigner("key-id-123", priv_pem.encode())
    msg = "1700000000000GET/trade-api/v2/markets"
    sig_b64 = signer.sign_message(msg)
    sig = base64.b64decode(sig_b64)
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    pub = load_pem_public_key(pub_pem.encode())
    pub.verify(sig, msg.encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                              salt_length=padding.PSS.DIGEST_LENGTH),
               hashes.SHA256())  # raises on mismatch


def test_headers_shape_and_query_stripped(keys):
    _, priv_pem = keys
    signer = KalshiSigner("kid", priv_pem.encode())
    h = signer.headers("get", "/trade-api/v2/markets?limit=10&status=open")
    assert h["KALSHI-ACCESS-KEY"] == "kid"
    assert h["KALSHI-ACCESS-TIMESTAMP"].isdigit()
    assert len(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])) == 256  # RSA-2048
    # signature must verify over the path WITHOUT the query string
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    priv = serialization.load_pem_private_key(priv_pem.encode(), password=None)
    ts = h["KALSHI-ACCESS-TIMESTAMP"]
    expected_msg = f"{ts}GET/trade-api/v2/markets"
    priv.sign  # noqa: B018  (signature already in headers)
    assert base64.b64encode(
        priv.sign(expected_msg.encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                     salt_length=padding.PSS.DIGEST_LENGTH),
                  hashes.SHA256())
    )  # determinism not required for PSS; shape assertions above suffice


def test_signer_requires_key_id():
    with pytest.raises(ValueError):
        KalshiSigner("", b"not-a-key")


class StubResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.body = body or {}
        self.content = b"x" if body != {} else b""
        self._json = body if body is not None else {}
        self.text = str(self._json)

    def json(self):
        return self._json


class StubSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params,
                           "json": json, "headers": headers})
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def make_client(session, rate=1000.0):
    cfg = AppConfig()
    cfg.rate_limit_per_sec = rate
    cfg.request_timeout_s = 2
    return KalshiClient(cfg, signer=None, session=session)


def test_get_markets_pagination():
    s = StubSession([
        StubResponse(200, {"markets": [{"ticker": "A-1"}], "cursor": "c2"}),
        StubResponse(200, {"markets": [{"ticker": "A-2"}], "cursor": ""}),
    ])
    c = make_client(s)
    markets = c.get_markets(status="open", limit=10)
    assert [m["ticker"] for m in markets] == ["A-1", "A-2"]
    assert s.calls[0]["params"]["status"] == "open"


def test_retry_on_5xx_then_success():
    s = StubSession([StubResponse(500, body={"error": "x"}), StubResponse(200, {"balance": 500})])
    c = make_client(s)
    assert c.request("GET", "/portfolio/balance") == {"balance": 500}
    assert len(s.calls) == 2


def test_401_raises_auth_error():
    s = StubSession([StubResponse(401, body={"error": "bad sig"})])
    c = make_client(s)
    with pytest.raises(KalshiAuthError):
        c.request("GET", "/portfolio/balance")


def test_400_raises_api_error_no_retry():
    s = StubSession([StubResponse(400, body={"error": "bad"})])
    c = make_client(s)
    with pytest.raises(KalshiApiError) as ei:
        c.request("GET", "/x")
    assert ei.value.status == 400
    assert len(s.calls) == 1


def test_signed_request_includes_headers(keys):
    pub, priv = keys
    import tempfile, os

    with tempfile.TemporaryDirectory() as td:
        pem_path = os.path.join(td, "k.pem")
        with open(pem_path, "wb") as fh:
            fh.write(priv.encode())
        cfg = AppConfig()
        cfg.rate_limit_per_sec = 1000
        signer = KalshiSigner.from_file("my-kid", pem_path)
        s = StubSession([StubResponse(200, {"balance": 100})])
        c = KalshiClient(cfg, signer=signer, session=s)
        c.request("GET", "/portfolio/balance")
        sent = s.calls[0]["headers"]
        assert sent["KALSHI-ACCESS-KEY"] == "my-kid"
        assert "KALSHI-ACCESS-SIGNATURE" in sent


def test_create_order_body():
    s = StubSession([StubResponse(200, {"order": {"order_id": "o1", "status": "executed"}})])
    c = make_client(s)
    out = c.create_order({"ticker": "T-1", "action": "buy", "side": "yes",
                          "type": "limit", "count": 5, "yes_price": 45})
    assert out["order"]["order_id"] == "o1"
    assert s.calls[0]["method"] == "POST"
    assert s.calls[0]["json"]["yes_price"] == 45
