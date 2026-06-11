"""Unit tests for the broker handler — no live AWS (STS is stubbed).

Proves the broker's two non-negotiable behaviours: it fails CLOSED on un-scopable
claims (vends no creds), and on good claims it assumes the role passing exactly the
four derived `agg:` tags (including the computed tier).
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
    monkeypatch.setattr(broker, "AUTHENTICATED_ROLE_ARN", "arn:aws:iam::123:role/agg-authenticated")
    return fake


def test_vends_scoped_creds_with_four_tags(stub_sts):
    claims = {"sub": "u123", "affiliation": "researcher", "tenant": "kempner", "grant": True}
    result = broker.vend_credentials(claims, subject="u123")

    assert result["credentials"]["accessKeyId"] == "ASIAFAKE"
    # Derived tier made it into the STS Tags (the whole reason the broker exists).
    sent = {t["Key"]: t["Value"] for t in stub_sts.last_call["Tags"]}
    assert sent["agg:affiliation"] == "researcher"
    assert sent["agg:tenant"] == "kempner"
    assert sent["agg:tier"] == "frontier"
    assert set(stub_sts.last_call["TransitiveTagKeys"]) == set(sent.keys())
    assert result["scope"]["tier"] == "frontier"


def test_fails_closed_on_missing_tenant(stub_sts):
    with pytest.raises(broker.BrokerError):
        broker.vend_credentials({"affiliation": "faculty"}, subject="u1")
    # And no STS call was made.
    assert stub_sts.last_call is None


def test_handler_returns_403_on_bad_claims(stub_sts):
    event = {"body": json.dumps({"idp_token": json.dumps({"affiliation": "faculty"})})}
    resp = broker.handler(event, None)
    assert resp["statusCode"] == 403
    assert "credentials" not in resp["body"]


def test_handler_200_on_good_claims(stub_sts):
    token = json.dumps(
        {"sub": "u1", "affiliation": "student", "tenant": "chem", "courses": ["CHEM-101"]}
    )
    event = {"body": json.dumps({"idp_token": token})}
    resp = broker.handler(event, None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["scope"]["tier"] == "oss"
    assert body["scope"]["courses"] == ["CHEM-101"]
