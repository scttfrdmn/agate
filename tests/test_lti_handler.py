"""Tests for the LTI handler's security-critical edge: id_token verification,
state/nonce single-use, and fail-closed behaviour. No AWS, no network — the JWKS
signing key and DynamoDB state are stubbed; a real in-test RSA keypair signs the
tokens so the RS256 path is exercised for real.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agg.lti import CLAIM_CONTEXT, CLAIM_ROLES  # noqa: E402

from lti import handler as lti  # noqa: E402

ISSUER = "https://lms.example.edu"
CLIENT_ID = "agg-client-1"
INSTRUCTOR = "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"
LEARNER = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def _make_id_token(private_key, *, nonce: str, roles=None, ctx=True, **overrides) -> str:
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "user-7",
        "nonce": nonce,
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        CLAIM_ROLES: roles if roles is not None else [LEARNER],
    }
    if ctx:
        claims[CLAIM_CONTEXT] = {"id": "CHEM-101", "label": "chem"}
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256")


@pytest.fixture
def wired(monkeypatch, keypair):
    private_key, public_key = keypair

    # Stub registration lookup.
    reg = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "jwks_uri": "https://lms.example.edu/jwks",
        "auth_login_url": "https://lms.example.edu/auth",
        "tenant": "harvard-chem",
    }
    monkeypatch.setattr(lti, "_registration", lambda issuer, client_id=None: reg)

    # Stub the JWKS signing-key resolution to return our in-test public key.
    class _SigningKey:
        key = public_key

    monkeypatch.setattr(
        lti.PyJWKClient,
        "get_signing_key_from_jwt",
        lambda self, token: _SigningKey(),
    )

    # In-memory state store.
    store: dict[str, dict] = {}
    monkeypatch.setattr(
        lti,
        "_put_state",
        lambda s, n, i, c: store.__setitem__(
            s, {"state": s, "nonce": n, "issuer": i, "client_id": c}
        ),
    )

    def _consume(state):
        return store.pop(state, None)

    monkeypatch.setattr(lti, "_consume_state", _consume)
    monkeypatch.setattr(lti, "TOOL_BASE_URL", "https://agg.example.edu")
    return private_key, store


def test_valid_launch_mints_agg_claims(wired):
    private_key, store = wired
    # Seed a state/nonce as login() would.
    store["st8"] = {"state": "st8", "nonce": "n0", "issuer": ISSUER, "client_id": CLIENT_ID}
    token = _make_id_token(private_key, nonce="n0", roles=[LEARNER])

    resp = lti.launch({"id_token": token, "state": "st8"})
    assert resp["statusCode"] == 302
    loc = resp["headers"]["location"]
    assert "agg_claims" in loc
    assert "harvard-chem" in loc  # tenant from registration
    # state consumed (single use)
    assert "st8" not in store


def test_instructor_launch_carries_faculty(wired):
    private_key, store = wired
    store["s2"] = {"state": "s2", "nonce": "n2", "issuer": ISSUER, "client_id": CLIENT_ID}
    token = _make_id_token(private_key, nonce="n2", roles=[INSTRUCTOR])
    resp = lti.launch({"id_token": token, "state": "s2"})
    assert "faculty" in resp["headers"]["location"]


def test_replayed_state_is_rejected(wired):
    private_key, store = wired
    store["s3"] = {"state": "s3", "nonce": "n3", "issuer": ISSUER, "client_id": CLIENT_ID}
    token = _make_id_token(private_key, nonce="n3")
    lti.launch({"id_token": token, "state": "s3"})  # first use ok
    # Second use: state row already consumed -> rejected.
    with pytest.raises(lti.LtiError, match="state"):
        lti.launch({"id_token": token, "state": "s3"})


def test_nonce_mismatch_rejected(wired):
    private_key, store = wired
    store["s4"] = {"state": "s4", "nonce": "EXPECTED", "issuer": ISSUER, "client_id": CLIENT_ID}
    token = _make_id_token(private_key, nonce="DIFFERENT")
    with pytest.raises(lti.LtiError, match="nonce"):
        lti.launch({"id_token": token, "state": "s4"})


def test_tampered_signature_rejected(wired, keypair):
    private_key, store = wired
    store["s5"] = {"state": "s5", "nonce": "n5", "issuer": ISSUER, "client_id": CLIENT_ID}
    # Sign with a DIFFERENT key than the JWKS will present.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _make_id_token(other, nonce="n5")
    with pytest.raises(lti.LtiError, match="verification failed"):
        lti.launch({"id_token": token, "state": "s5"})


def test_expired_token_rejected(wired):
    private_key, store = wired
    store["s6"] = {"state": "s6", "nonce": "n6", "issuer": ISSUER, "client_id": CLIENT_ID}
    token = _make_id_token(private_key, nonce="n6", exp=int(time.time()) - 10)
    with pytest.raises(lti.LtiError, match="verification failed"):
        lti.launch({"id_token": token, "state": "s6"})


def test_wrong_audience_rejected(wired):
    private_key, store = wired
    store["s7"] = {"state": "s7", "nonce": "n7", "issuer": ISSUER, "client_id": CLIENT_ID}
    token = _make_id_token(private_key, nonce="n7", aud="some-other-client")
    with pytest.raises(lti.LtiError, match="verification failed"):
        lti.launch({"id_token": token, "state": "s7"})


def test_launch_missing_fields_fails_closed(wired):
    with pytest.raises(lti.LtiError):
        lti.launch({"id_token": "x"})  # no state
    with pytest.raises(lti.LtiError):
        lti.launch({"state": "y"})  # no token


def test_jwks_endpoint_returns_empty_set_by_default(wired, monkeypatch):
    monkeypatch.delenv("AGG_TOOL_JWKS", raising=False)
    resp = lti.jwks()
    assert resp["statusCode"] == 200
    assert '"keys": []' in resp["body"] or '"keys":[]' in resp["body"]


# --- login (OIDC third-party init) ------------------------------------------


def test_login_issues_state_and_redirects_to_platform(wired):
    _, store = wired
    resp = lti.login({"iss": ISSUER, "login_hint": "u1", "client_id": CLIENT_ID})
    assert resp["statusCode"] == 302
    loc = resp["headers"]["location"]
    assert loc.startswith("https://lms.example.edu/auth?")
    assert "state=" in loc and "nonce=" in loc
    assert "response_type=id_token" in loc
    # State+nonce persisted for the upcoming launch.
    assert len(store) == 1


def test_login_missing_fields_fails_closed(wired):
    with pytest.raises(lti.LtiError):
        lti.login({"iss": ISSUER})  # no login_hint


# --- HTTP API router + body parsing -----------------------------------------


def _event(path: str, *, method: str = "GET", body: str = "", query: dict | None = None):
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method, "path": path}},
        "queryStringParameters": query,
        "body": body,
    }


def test_router_dispatches_login(wired):
    resp = lti.handler(
        _event("/lti/login", query={"iss": ISSUER, "login_hint": "u1", "client_id": CLIENT_ID}),
        None,
    )
    assert resp["statusCode"] == 302


def test_router_jwks(wired):
    resp = lti.handler(_event("/.well-known/jwks.json"), None)
    assert resp["statusCode"] == 200


def test_router_deeplink(wired):
    resp = lti.handler(_event("/lti/deeplink", method="POST"), None)
    assert resp["statusCode"] == 200


def test_router_unknown_path_404(wired):
    resp = lti.handler(_event("/nope"), None)
    assert resp["statusCode"] == 404


def test_router_maps_lti_error_to_400(wired):
    # login with missing login_hint -> LtiError -> 400 (not a 500).
    resp = lti.handler(_event("/lti/login", query={"iss": ISSUER}), None)
    assert resp["statusCode"] == 400


def test_form_parses_urlencoded_and_json():
    assert lti._form({"body": "a=1&b=two"}) == {"a": "1", "b": "two"}
    assert lti._form({"body": '{"a": 1}'}) == {"a": 1}
    assert lti._form({"body": ""}) == {}


def test_form_handles_base64_body():
    import base64

    encoded = base64.b64encode(b"x=9").decode()
    assert lti._form({"body": encoded, "isBase64Encoded": True}) == {"x": "9"}
