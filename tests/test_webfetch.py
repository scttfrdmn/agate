"""Adversarial tests for the web-fetch SSRF guard (#192). Pure — no network.

The guard IS the security boundary (the fetch Lambda has no VPC), so these exercise
the exfiltration vectors: the cloud metadata endpoint, private/loopback ranges, http://,
non-allowlisted hosts, raw-IP hosts, and IPv4-mapped IPv6.
"""

from __future__ import annotations

import pytest
from agate.webfetch import (
    WebFetchError,
    is_allowed_host,
    is_safe_ip,
    parse_allowlist,
    validate_url,
)

ALLOW = ("example.edu", "arxiv.org")


# --- allowlist parsing + matching ------------------------------------------


def test_parse_allowlist_comma_or_space():
    assert parse_allowlist("a.edu, b.org  c.net") == ("a.edu", "b.org", "c.net")
    assert parse_allowlist("") == ()
    assert parse_allowlist(None) == ()


def test_empty_allowlist_denies_everything():
    assert is_allowed_host("example.edu", ()) is False


def test_host_match_exact_and_subdomain_but_not_lookalike():
    assert is_allowed_host("example.edu", ALLOW) is True
    assert is_allowed_host("lib.example.edu", ALLOW) is True
    # segment-wise: a look-alike parent must NOT match
    assert is_allowed_host("evil-example.edu", ALLOW) is False
    assert is_allowed_host("example.edu.attacker.com", ALLOW) is False


# --- IP safety (the metadata-endpoint + private-range guard) ---------------


@pytest.mark.parametrize(
    "ip",
    [
        "169.254.169.254",  # AWS IMDS — the headline SSRF target
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "192.168.1.1",  # private
        "172.16.0.1",  # private
        "0.0.0.0",  # unspecified
        "fd00::1",  # private v6
        "::1",  # loopback v6
        "fe80::1",  # link-local v6
        "::ffff:169.254.169.254",  # IPv4-mapped IMDS
        "not-an-ip",  # unparseable → unsafe
    ],
)
def test_unsafe_ips_blocked(ip):
    assert is_safe_ip(ip) is False


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "151.101.0.4", "2606:4700::1111"])
def test_public_ips_allowed(ip):
    assert is_safe_ip(ip) is True


# --- validate_url -----------------------------------------------------------


def test_validate_url_happy():
    assert validate_url("https://lib.example.edu/paper.pdf", ALLOW) == "lib.example.edu"


def test_validate_url_rejects_non_https():
    for url in ["http://example.edu/x", "file:///etc/passwd", "gopher://example.edu"]:
        with pytest.raises(WebFetchError):
            validate_url(url, ALLOW)


def test_validate_url_rejects_non_allowlisted_host():
    with pytest.raises(WebFetchError):
        validate_url("https://attacker.com/x", ALLOW)


def test_validate_url_rejects_raw_ip_host():
    # A raw IP host bypasses DNS + the host allowlist — always rejected.
    with pytest.raises(WebFetchError):
        validate_url("https://169.254.169.254/latest/meta-data/", ALLOW)
    with pytest.raises(WebFetchError):
        validate_url("https://8.8.8.8/", ALLOW)


def test_validate_url_rejects_empty_or_hostless():
    with pytest.raises(WebFetchError):
        validate_url("", ALLOW)
    with pytest.raises(WebFetchError):
        validate_url("https:///nohost", ALLOW)


def test_validate_url_empty_allowlist_denies():
    with pytest.raises(WebFetchError):
        validate_url("https://example.edu/x", ())
