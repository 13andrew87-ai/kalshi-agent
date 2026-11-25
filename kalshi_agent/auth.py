"""RSA-PSS request signing for the Kalshi Trade API v2.

Kalshi authenticates every request with three headers:
    KALSHI-ACCESS-KEY:       the API key id (UUID from the dashboard)
    KALSHI-ACCESS-TIMESTAMP: unix milliseconds as a string
    KALSHI-ACCESS-SIGNATURE: base64( RSA-PSS-SHA256( timestamp + METHOD + path ) )

The signed message is ``timestamp + METHOD + path`` where ``path`` includes
the ``/trade-api/v2`` prefix and excludes any query string.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class KalshiSigner:
    def __init__(self, key_id: str, private_key_pem: bytes, salt_length: str = "digest"):
        if not key_id:
            raise ValueError("api_key_id is required for signed requests")
        self.key_id = key_id
        self._key: rsa.RSAPrivateKey = serialization.load_pem_private_key(private_key_pem, password=None)
        if not isinstance(self._key, rsa.RSAPrivateKey):
            raise ValueError("private key must be an RSA key (Kalshi API keys are RSA)")
        self._salt_length = (
            padding.PSS.DIGEST_LENGTH if salt_length == "digest" else padding.PSS.MAX_LENGTH
        )

    @classmethod
    def from_file(cls, key_id: str, pem_path: str, salt_length: str = "digest") -> "KalshiSigner":
        with open(pem_path, "rb") as fh:
            return cls(key_id, fh.read(), salt_length=salt_length)

    @staticmethod
    def timestamp_ms() -> str:
        return str(int(datetime.now(timezone.utc).timestamp() * 1000))

    def sign_message(self, message: str) -> str:
        sig = self._key.sign(
            message.encode("utf-8"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=self._salt_length),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("ascii")

    def headers(self, method: str, path: str) -> dict[str, str]:
        """Build signed headers.  `path` must be the URL path WITHOUT query string,
        e.g. '/trade-api/v2/markets'."""
        path = path.split("?")[0]
        ts = self.timestamp_ms()
        sig = self.sign_message(f"{ts}{method.upper()}{path}")
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }


def generate_keypair() -> tuple[str, str]:
    """Generate an RSA-2048 keypair for Kalshi.  Returns (public_pem, private_pem)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return pub, priv
