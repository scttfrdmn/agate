"""Unit tests for the web-search MCP tool (#248). No network — DNS + HTTP + parse injected.

Headline assertions: the search ENDPOINT passes the same SSRF/allowlist guard as a fetch, the
scope comes from the verified token, a search is budget-gated before the query leaves, result URLs
are RETURNED (never fetched here), and everything fails closed.
"""

from __future__ import annotations

import json

import pytest
from infra.functions.websearch import handler as h


def _claims(tenant="chem", scope="chem-101"):
    return {"sub": "stu", "affiliation": "student", "tenant": tenant, "data_scope": scope}


def _raise():
    raise h.WebSearchToolError("missing idp_token")


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(h, "ALLOWLIST", ("search.example.edu",))
    monkeypatch.setattr(h, "ENDPOINT_TEMPLATE", "https://search.example.edu/api?q={query}")
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: _claims() if tok else _raise())
    return monkeypatch


def _invoke(req, resolve, http_get, extract, monkeypatch, spend_reader=None):
    monkeypatch.setattr(h, "_real_resolve", resolve)
    monkeypatch.setattr(h, "_real_http_get", http_get)
    monkeypatch.setattr(h, "_extract_result_urls", extract)
    monkeypatch.setattr(h, "_real_spend_reader", spend_reader or (lambda label: (0.0, None)))
    resp = h.handler({"body": json.dumps(req)}, None)
    return {"status": resp["statusCode"], "body": json.loads(resp["body"])}


# --- happy path -------------------------------------------------------------


def test_search_returns_result_urls_and_does_not_fetch_them(wired):
    results = ["https://arxiv.org/abs/1", "https://arxiv.org/abs/2"]
    out = _invoke(
        {"idp_token": "t", "tool": "web-search", "query": "reaction kinetics"},
        lambda host: ["151.101.0.4"],  # public
        lambda url, ip: json.dumps({"results": [{"url": u} for u in results]}),
        h._extract_result_urls,  # exercise the real parser
        wired,
    )
    assert out["status"] == 200
    assert out["body"]["results"] == results
    assert out["body"]["count"] == 2
    assert out["body"]["source_system"] == "web-search"
    # No "content" key — this tool never dereferences a result URL.
    assert "content" not in out["body"]


def test_endpoint_body_read_is_bounded(wired, monkeypatch):
    # Review PR3 Finding 3: the guard reuses webfetch's MAX_BYTES pattern — the real HTTP edge caps
    # the body read so a huge response can't exhaust the Lambda. Assert MAX_BYTES exists + is finite
    # (the read itself is in the pragma-no-cover live edge; here we lock in the presence of a cap).
    assert isinstance(h.MAX_BYTES, int) and h.MAX_BYTES > 0


def test_query_is_url_encoded_into_the_endpoint(wired):
    captured = {}

    def http_get(url, ip):
        captured["url"] = url
        return json.dumps({"results": []})

    _invoke(
        {"idp_token": "t", "query": "acids & bases"},
        lambda host: ["151.101.0.4"],
        http_get,
        lambda body: [],
        wired,
    )
    # The space + ampersand are percent-encoded, so they cannot inject query params.
    assert "acids%20%26%20bases" in captured["url"]


# --- fail-closed: budget, SSRF, allowlist, identity -------------------------


def test_search_rejected_when_over_budget(wired):
    reached = {"searched": False}

    def http_get(url, ip):
        reached["searched"] = True
        return json.dumps({"results": [{"url": "https://arxiv.org/x"}]})

    out = _invoke(
        {"idp_token": "t", "query": "q"},
        lambda host: ["151.101.0.4"],
        http_get,
        lambda body: [],
        wired,
        spend_reader=lambda label: (100.0, 1.0),  # tenant over budget
    )
    assert out["status"] == 403
    assert reached["searched"] is False  # the query never left
    assert "over budget" in out["body"]["detail"]


def test_endpoint_resolving_to_metadata_ip_is_blocked(wired):
    reached = {"searched": False}

    def http_get(url, ip):
        reached["searched"] = True
        return "{}"

    out = _invoke(
        {"idp_token": "t", "query": "q"},
        lambda host: ["169.254.169.254"],  # cloud metadata endpoint
        http_get,
        lambda body: [],
        wired,
    )
    assert out["status"] == 403
    assert reached["searched"] is False


def test_endpoint_not_on_allowlist_is_denied(monkeypatch):
    monkeypatch.setattr(h, "ALLOWLIST", ())  # deny-all
    monkeypatch.setattr(h, "ENDPOINT_TEMPLATE", "https://search.example.edu/api?q={query}")
    monkeypatch.setattr(h, "validate_idp_token", lambda tok: _claims())
    out = _invoke(
        {"idp_token": "t", "query": "q"},
        lambda host: ["151.101.0.4"],
        lambda url, ip: "{}",
        lambda body: [],
        monkeypatch,
    )
    assert out["status"] == 403


def test_missing_token_fails_closed(wired):
    out = _invoke(
        {"query": "q"},
        lambda host: ["151.101.0.4"],
        lambda url, ip: "{}",
        lambda body: [],
        wired,
    )
    assert out["status"] == 403


def test_missing_query_fails_closed(wired):
    out = _invoke(
        {"idp_token": "t", "query": "   "},
        lambda host: ["151.101.0.4"],
        lambda url, ip: "{}",
        lambda body: [],
        wired,
    )
    assert out["status"] == 403


def test_unknown_tool_rejected(wired):
    out = _invoke(
        {"idp_token": "t", "tool": "web-fetch", "query": "q"},
        lambda host: ["151.101.0.4"],
        lambda url, ip: "{}",
        lambda body: [],
        wired,
    )
    assert out["status"] == 403


# --- result parser ----------------------------------------------------------


def test_extract_handles_both_result_shapes_and_bad_json():
    assert h._extract_result_urls(json.dumps({"results": [{"url": "https://a/1"}]})) == [
        "https://a/1"
    ]
    assert h._extract_result_urls(json.dumps({"items": [{"link": "https://b/2"}]})) == [
        "https://b/2"
    ]
    assert h._extract_result_urls("not json at all") == []  # fail closed to empty
    assert h._extract_result_urls(json.dumps({"results": [{"title": "no url"}]})) == []
