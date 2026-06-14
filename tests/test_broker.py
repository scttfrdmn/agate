"""Unit tests for the broker handler — no live AWS (STS is stubbed).

Proves the broker's two non-negotiable behaviours: it fails CLOSED on un-scopable
claims (vends no creds), and on good claims it assumes the role passing exactly the
four derived `agate:` tags (including the computed tier).
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

# The handler lives under infra/functions/broker; make `infra` importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.functions.broker import handler as broker  # noqa: E402


class _FakeSts:
    def __init__(self):
        self.last_call = None

    def assume_role(self, **kwargs):
        self.last_call = kwargs
        return {
            "Credentials": {
                "AccessKeyId": "ASIAFAKE",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": dt.datetime(2026, 6, 11, 12, 0, 0, tzinfo=dt.UTC),
            }
        }


@pytest.fixture
def stub_sts(monkeypatch):
    fake = _FakeSts()
    monkeypatch.setattr(broker, "_sts", fake)
    role_arn = "arn:aws:iam::123:role/agate-authenticated"
    monkeypatch.setattr(broker, "AUTHENTICATED_ROLE_ARN", role_arn)
    return fake


def test_vends_scoped_creds_with_four_tags(stub_sts):
    claims = {"sub": "u123", "affiliation": "researcher", "tenant": "kempner", "grant": True}
    result = broker.vend_credentials(claims, subject="u123")

    assert result["credentials"]["accessKeyId"] == "ASIAFAKE"
    # Derived tier made it into the STS Tags (the whole reason the broker exists).
    sent = {t["Key"]: t["Value"] for t in stub_sts.last_call["Tags"]}
    assert sent["agate:affiliation"] == "researcher"
    assert sent["agate:tenant"] == "kempner"
    assert sent["agate:tier"] == "frontier"
    assert set(stub_sts.last_call["TransitiveTagKeys"]) == set(sent.keys())
    assert result["scope"]["tier"] == "frontier"
    # #79: the RoleSessionName encodes the tenant (`<tenant>@<subject>`) so the meter
    # can attribute spend unforgeably from the ARN, not from client requestMetadata.
    assert stub_sts.last_call["RoleSessionName"] == "kempner@u123"


def test_fails_closed_on_missing_tenant(stub_sts):
    with pytest.raises(broker.BrokerError):
        broker.vend_credentials({"affiliation": "faculty"}, subject="u1")
    # And no STS call was made.
    assert stub_sts.last_call is None


@pytest.fixture
def verified_token(monkeypatch):
    """Simulate a VERIFIED token: validate_idp_token returns the decoded claims.
    Real signature/JWKS verification is covered by tests/test_jwt_verify.py; here we
    exercise the broker's scoping path given an already-verified claim set."""

    def _decode(token):
        claims = json.loads(token)
        # Mirror verify_token's fail-closed contract for the unverifiable cases the
        # handler relies on (missing/blank token).
        if not isinstance(claims, dict):
            raise broker.BrokerError("bad token")
        return claims

    monkeypatch.setattr(broker, "validate_idp_token", _decode)


def test_handler_returns_403_on_bad_claims(stub_sts, verified_token):
    event = {"body": json.dumps({"idp_token": json.dumps({"affiliation": "faculty"})})}
    resp = broker.handler(event, None)
    assert resp["statusCode"] == 403
    assert "credentials" not in resp["body"]


def test_handler_200_on_good_claims(stub_sts, verified_token):
    token = json.dumps(
        {"sub": "u1", "affiliation": "student", "tenant": "chem", "courses": ["CHEM-101"]}
    )
    event = {"body": json.dumps({"idp_token": token})}
    resp = broker.handler(event, None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["scope"]["tier"] == "oss"
    assert body["scope"]["courses"] == ["CHEM-101"]


def test_handler_rejects_unverifiable_token_when_unconfigured(stub_sts):
    # With no OIDC config and the REAL validate_idp_token, an unsigned/garbage token
    # must fail closed (403) — the SEC-4 placeholder path is gone.
    event = {"body": json.dumps({"idp_token": "not-a-real-jwt"})}
    resp = broker.handler(event, None)
    assert resp["statusCode"] == 403
    assert "credentials" not in resp["body"]


# --- source-IP allowlist (pure) ---------------------------------------------


def test_ip_allowed_empty_allowlist_allows_all():
    assert broker.ip_allowed("203.0.113.5", "") is True
    assert broker.ip_allowed("", "") is True  # no fence configured


def test_ip_allowed_exact_and_cidr():
    assert broker.ip_allowed("203.0.113.5", "203.0.113.5") is True
    assert broker.ip_allowed("203.0.113.5", "203.0.113.0/24") is True
    assert broker.ip_allowed("198.51.100.7", "203.0.113.0/24") is False


def test_ip_allowed_multi_entry_and_whitespace():
    allow = "198.51.100.0/24, 203.0.113.5"
    assert broker.ip_allowed("203.0.113.5", allow) is True
    assert broker.ip_allowed("198.51.100.99", allow) is True
    assert broker.ip_allowed("192.0.2.1", allow) is False


def test_ip_allowed_fails_closed_on_blank_ip_and_bad_entry():
    # An allowlist is set but the source IP is missing → deny.
    assert broker.ip_allowed("", "203.0.113.0/24") is False
    # A malformed allowlist entry must not silently widen access.
    assert broker.ip_allowed("203.0.113.5", "not-a-cidr") is False


def test_handler_denies_off_allowlist_source_ip(stub_sts, verified_token, monkeypatch):
    monkeypatch.setattr(broker, "IP_ALLOWLIST", "203.0.113.0/24")
    token = json.dumps({"sub": "u1", "affiliation": "student", "tenant": "chem"})
    event = {
        "body": json.dumps({"idp_token": token}),
        "requestContext": {"http": {"sourceIp": "192.0.2.99"}},
    }
    resp = broker.handler(event, None)
    assert resp["statusCode"] == 403
    assert "credentials" not in resp["body"]


def test_handler_allows_on_allowlist_source_ip(stub_sts, verified_token, monkeypatch):
    monkeypatch.setattr(broker, "IP_ALLOWLIST", "203.0.113.0/24")
    token = json.dumps({"sub": "u1", "affiliation": "student", "tenant": "chem"})
    event = {
        "body": json.dumps({"idp_token": token}),
        "requestContext": {"http": {"sourceIp": "203.0.113.42"}},
    }
    resp = broker.handler(event, None)
    assert resp["statusCode"] == 200
