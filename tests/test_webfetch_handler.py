"""Unit tests for the web-fetch MCP tool (#192). No network — DNS + HTTP injected.

The headline assertions: the SSRF guard runs on the initial URL AND on every redirect
hop (a redirect to a private/metadata host is blocked even though the first host was
fine), the scope comes from the verified token, and everything fails closed.
"""

from __future__ import annotations

import json

import pytest
from infra.functions.webfetch import handler as h


def _claims(tenant="chem", scope="chem-101"):
    return {"sub": "stu", "affiliation": "student", "tenant": tenant, "data_scope": scope}


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(h, "ALLOWLIST", ("example.edu", "arxiv.org"))
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: _claims() if tok else _raise())
    return monkeypatch


def _raise():
    raise h.WebFetchToolError("missing idp_token")


def _invoke(req, resolve, fetch, monkeypatch, spend_reader=None):
    monkeypatch.setattr(h, "_real_resolve", resolve)
    monkeypatch.setattr(h, "_real_fetch", fetch)
    # Default: a generous budget so the cascade allows (gate tested separately).
    monkeypatch.setattr(h, "_real_spend_reader", spend_reader or (lambda label: (0.0, None)))
    resp = h.handler({"body": json.dumps(req)}, None)
    return {"status": resp["statusCode"], "body": json.loads(resp["body"])}


# --- happy path -------------------------------------------------------------


def test_fetch_allowlisted_public_host(wired):
    resolve = lambda host: ["151.101.0.4"]  # public  # noqa: E731
    fetch = lambda url, ip: (200, {}, b"hello from the web", None)  # noqa: E731
    out = _invoke(
        {"idp_token": "t", "tool": "web-fetch", "url": "https://arxiv.org/abs/1"},
        resolve, fetch, wired,
    )
    assert out["status"] == 200
    assert out["body"]["content"] == "hello from the web"
    assert out["body"]["url"] == "https://arxiv.org/abs/1"
    assert out["body"]["source_system"] == "web"
    assert out["body"]["source_item"] == "https://arxiv.org/abs/1"


def test_fetch_rejected_when_over_budget(wired):
    # A scope node at/over its budget rejects the priced fetch BEFORE any bytes leave.
    reached = {"fetched": False}

    def fetch(url, ip):
        reached["fetched"] = True
        return (200, {}, b"leaked", None)

    out = _invoke(
        {"idp_token": "t", "url": "https://arxiv.org/a"},
        lambda host: ["151.101.0.4"],
        fetch,
        wired,
        spend_reader=lambda label: (5.0, 5.0),  # spent == budget → no headroom
    )
    assert out["status"] == 403
    assert reached["fetched"] is False  # gated pre-fetch, nothing left


def test_fetch_allowed_within_budget_reports_price(wired):
    out = _invoke(
        {"idp_token": "t", "url": "https://arxiv.org/a"},
        lambda host: ["151.101.0.4"],
        lambda u, ip: (200, {}, b"ok", None),
        wired,
        spend_reader=lambda label: (0.0, 100.0),
    )
    assert out["status"] == 200
    assert out["body"]["price_usd"] == h.FETCH_PRICE_USD


def test_fetch_is_pinned_to_the_validated_ip(wired):
    # TOCTOU defence: the IP handed to fetch() must be the one the guard validated, so the
    # socket can't connect to a second (attacker-rebound) resolution.
    seen = {}
    resolve = lambda host: ["151.101.0.4"]  # noqa: E731

    def fetch(url, ip):
        seen["ip"] = ip
        return (200, {}, b"ok", None)

    _invoke({"idp_token": "t", "url": "https://arxiv.org/a"}, resolve, fetch, wired)
    assert seen["ip"] == "151.101.0.4"


# --- SSRF: the load-bearing cases ------------------------------------------


def test_blocks_non_allowlisted_host(wired):
    out = _invoke(
        {"idp_token": "t", "url": "https://attacker.com/x"},
        lambda h: ["8.8.8.8"], lambda u, ip: (200, {}, b"", None), wired,
    )
    assert out["status"] == 403


def test_blocks_host_resolving_to_metadata_ip(wired):
    # Host is allowlisted, but DNS resolves to the IMDS address — blocked (rebinding).
    out = _invoke(
        {"idp_token": "t", "url": "https://example.edu/x"},
        lambda host: ["169.254.169.254"], lambda u, ip: (200, {}, b"secret", None), wired,
    )
    assert out["status"] == 403
    assert "content" not in out["body"]  # no bytes leaked from the blocked host


def test_blocks_redirect_to_private_host(wired):
    # First host is fine + public; it 302s to a private host → the hop is re-validated
    # and rejected. The redirect target's bytes are never returned.
    calls = {"n": 0}

    def resolve(host):
        return {"example.edu": ["151.101.0.4"], "internal.local": ["10.0.0.5"]}.get(host, [])

    def fetch(url, ip):
        calls["n"] += 1
        if "example.edu" in url:
            return (302, {}, b"", "https://internal.local/secret")
        return (200, {}, b"SHOULD NOT BE REACHED", None)

    # internal.local isn't allowlisted either, so it's blocked at validate_url too — use a
    # host that IS allowlisted but resolves private to prove the IP re-check specifically.
    out = _invoke(
        {"idp_token": "t", "url": "https://example.edu/start"}, resolve, fetch, wired
    )
    assert out["status"] == 403


def test_redirect_to_allowlisted_private_ip_still_blocked(wired):
    # A redirect to an allowlisted host that resolves to a private IP must still be blocked
    # by the per-hop IP re-check (not just the allowlist).
    def resolve(host):
        return {"arxiv.org": ["151.101.0.4"], "example.edu": ["192.168.0.9"]}.get(host, [])

    def fetch(url, ip):
        if "arxiv.org" in url:
            return (301, {}, b"", "https://example.edu/inner")
        return (200, {}, b"leaked", None)

    out = _invoke({"idp_token": "t", "url": "https://arxiv.org/a"}, resolve, fetch, wired)
    assert out["status"] == 403


def test_too_many_redirects(wired):
    monkeypatch = wired
    monkeypatch.setattr(h, "MAX_REDIRECTS", 1)
    out = _invoke(
        {"idp_token": "t", "url": "https://arxiv.org/a"},
        lambda host: ["151.101.0.4"],
        lambda u, ip: (302, {}, b"", "https://arxiv.org/loop"),
        monkeypatch,
    )
    assert out["status"] == 403


# --- fail closed ------------------------------------------------------------


def test_missing_token_is_403(wired):
    out = _invoke(
        {"url": "https://arxiv.org/a"},
        lambda h: ["8.8.8.8"],
        lambda u, ip: (200, {}, b"", None),
        wired,
    )
    assert out["status"] == 403


def test_empty_allowlist_denies_all(monkeypatch):
    monkeypatch.setattr(h, "ALLOWLIST", ())
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: _claims())
    out = _invoke(
        {"idp_token": "t", "url": "https://arxiv.org/a"},
        lambda host: ["151.101.0.4"], lambda u, ip: (200, {}, b"", None), monkeypatch,
    )
    assert out["status"] == 403
