"""Tests for the web-search guard (#248). Pure — no network.

web-search reuses the web-fetch SSRF/allowlist/cascade guard (single egress-safety implementation),
so these assert the reuse holds — the same exfiltration vectors are blocked on the SEARCH ENDPOINT —
plus the search-specific budget gate and error type.
"""

from __future__ import annotations

import pytest
from agate.websearch import (
    WebSearchError,
    gate_search,
    is_allowed_search_host,
    is_safe_search_ip,
    parse_search_allowlist,
    validate_search_endpoint,
)

ALLOW = ("search.example.edu", "discovery.lib.example.edu")


def test_endpoint_allowlist_parsing_and_deny_all_default():
    assert parse_search_allowlist("a.edu, b.edu") == ("a.edu", "b.edu")
    # Empty allowlist denies everything (egress default-deny), same as web-fetch.
    assert is_allowed_search_host("search.example.edu", ()) is False


def test_validate_endpoint_happy():
    assert (
        validate_search_endpoint("https://search.example.edu/q?x=1", ALLOW) == "search.example.edu"
    )
    # subdomain of an allowed parent is fine
    assert (
        validate_search_endpoint("https://discovery.lib.example.edu/api", ALLOW)
        == "discovery.lib.example.edu"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://search.example.edu/q",  # non-https
        "https://evil.com/q",  # not allowlisted
        "https://169.254.169.254/latest/meta-data/",  # metadata via raw IP host
        "ftp://search.example.edu/q",  # non-https scheme
        "https://search-example.edu/q",  # lookalike, not a subdomain of example.edu
    ],
)
def test_validate_endpoint_rejects_unsafe(url):
    with pytest.raises(WebSearchError):
        validate_search_endpoint(url, ALLOW)


@pytest.mark.parametrize(
    "ip", ["169.254.169.254", "127.0.0.1", "10.0.0.1", "192.168.1.1", "100.64.0.1", "::1"]
)
def test_unsafe_endpoint_ips_blocked(ip):
    assert is_safe_search_ip(ip) is False


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1"])
def test_public_endpoint_ips_allowed(ip):
    assert is_safe_search_ip(ip) is True


def test_gate_search_allows_within_budget():
    d = gate_search(
        tenant="chem",
        scope="arts-sci/chemistry",
        price_usd=0.002,
        spend_lookup=lambda _l: (1.0, 100.0),
    )
    assert d.allowed is True


def test_gate_search_rejects_over_budget_and_names_node():
    # The dept node is exhausted → reject before the query leaves, naming the breaching node.
    def lookup(label):
        return (100.0, 100.0) if label == "arts-sci/chemistry" else (0.0, 100.0)

    d = gate_search(tenant="chem", scope="arts-sci/chemistry", price_usd=0.01, spend_lookup=lookup)
    assert d.allowed is False
    assert d.cascade.breaching_node == "arts-sci/chemistry"


def test_gate_search_no_budget_rows_allows():
    d = gate_search(
        tenant="chem",
        scope="arts-sci/chemistry",
        price_usd=0.5,
        spend_lookup=lambda _l: (0.0, None),
    )
    assert d.allowed is True


def test_gate_search_has_a_tenant_floor_like_fetch():
    # An UNSCOPED caller still gets the tenant node checked — no "empty nodes → unconditional
    # allow" footgun (the search budget floor equals the fetch floor, review MED #2).
    d = gate_search(tenant="chem", scope="", price_usd=0.01, spend_lookup=lambda _l: (100.0, 1.0))
    assert d.allowed is False  # tenant node over budget → rejected
