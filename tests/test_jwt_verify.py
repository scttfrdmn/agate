"""Tests for the shared JWT verifier (SEC-4). Real RS256 with an in-test keypair,
no network — the JWKS resolver is injected."""

from __future__ import annotations

import time

import jwt
import pytest
from agg.jwt_verify import TokenError, verify_token
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://idp.example.edu"
AUDIENCE = "agg-app"
JWKS = "https://idp.example.edu/.well-known/jwks.json"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


class _Resolver:
    """Injected JWKS resolver returning a fixed public key (mimics PyJWKClient)."""

    def __init__(self, public_key):
        self._k = public_key

    def get_signing_key_from_jwt(self, token):
        class _K:
            key = self._k

        return _K()


def _token(private_key, **over):
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-7",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "affiliation": "student",
        "tenant": "chem",
    }
    claims.update(over)
    return jwt.encode(claims, private_key, algorithm="RS256")


def _verify(token, public_key, **over):
    kw = dict(jwks_url=JWKS, issuer=ISSUER, audience=AUDIENCE, resolver=_Resolver(public_key))
    kw.update(over)
    return verify_token(token, **kw)


def test_valid_token_returns_claims(keypair):
    priv, pub = keypair
    claims = _verify(_token(priv), pub)
    assert claims["sub"] == "user-7"
    assert claims["tenant"] == "chem"


def test_tampered_signature_rejected(keypair):
    priv, pub = keypair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(TokenError, match="verification failed"):
        _verify(_token(other), pub)  # signed by a different key than JWKS presents


def test_expired_rejected(keypair):
    priv, pub = keypair
    with pytest.raises(TokenError):
        _verify(_token(priv, exp=int(time.time()) - 10), pub)


def test_wrong_audience_rejected(keypair):
    priv, pub = keypair
    with pytest.raises(TokenError):
        _verify(_token(priv, aud="someone-else"), pub)


def test_wrong_issuer_rejected(keypair):
    priv, pub = keypair
    with pytest.raises(TokenError):
        _verify(_token(priv, iss="https://evil.example"), pub)


def test_missing_required_claim_rejected(keypair):
    priv, pub = keypair
    # drop `sub` (required) by signing a minimal token
    import jwt as _jwt

    t = _jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "iat": int(time.time()), "exp": int(time.time()) + 60},
        priv,
        algorithm="RS256",
    )
    with pytest.raises(TokenError):
        _verify(t, pub)


def test_alg_none_rejected(keypair):
    priv, pub = keypair
    # An unsigned alg=none token must never validate.
    t = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "x"}, key=None, algorithm="none")
    with pytest.raises(TokenError):
        _verify(t, pub)


def test_empty_token_fails_closed(keypair):
    _, pub = keypair
    with pytest.raises(TokenError):
        _verify("", pub)


def test_misconfigured_verifier_fails_closed(keypair):
    priv, pub = keypair
    with pytest.raises(TokenError, match="misconfigured"):
        _verify(_token(priv), pub, issuer="")
