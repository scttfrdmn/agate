"""SSRF guard for the gated web-fetch capability (#192) — pure, fail-closed.

The web-fetch Lambda runs with no VPC (NO CLOCKS), so there is no network boundary
protecting it: this module IS the security boundary. Every fetch and every redirect
hop must pass `validate_url` AND have its resolved IPs checked by `is_safe_ip`. The
checks are deliberately default-deny:

  * https only — no http, file, gopher, ftp, data, etc.
  * host must be on the institution's allowlist — an EMPTY allowlist denies everything
    (the opposite of an ingress allowlist: for an egress guard, default-deny).
  * every resolved IP must be public — private / loopback / link-local / reserved /
    multicast / unspecified addresses are blocked, which covers the cloud metadata
    endpoint (169.254.169.254, fd00:ec2::254) and internal services.

DNS is resolved by the caller (the Lambda) and the resulting IPs are passed in, so this
module stays AWS- and network-free and is exhaustively unit-testable. The caller must
pin the connection to a checked IP (or disable redirects and re-run the full guard on
each `Location`) to defeat DNS-rebinding / TOCTOU.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


class WebFetchError(ValueError):
    """A URL/host/IP that fails the guard. Fail closed — never fetched."""


# RFC 6598 carrier-grade NAT / shared address space — not "private" per `ipaddress`.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def parse_allowlist(raw: str | None) -> tuple[str, ...]:
    """Parse a comma/space-separated host allowlist (from env/config) into lowercased
    hosts. Empty/None → an empty tuple, which `is_allowed_host` treats as deny-all."""
    if not raw:
        return ()
    parts = [p.strip().lower() for p in raw.replace(",", " ").split()]
    return tuple(p for p in parts if p)


def is_allowed_host(host: str, allowlist: tuple[str, ...]) -> bool:
    """Whether `host` is permitted by `allowlist`. An entry matches the host exactly OR
    as a parent domain (`example.edu` allows `lib.example.edu`), segment-wise so
    `evil-example.edu` is NOT matched by `example.edu`. Empty allowlist → deny-all."""
    if not allowlist or not host:
        return False
    h = host.strip().lower().rstrip(".")
    for entry in allowlist:
        e = entry.rstrip(".")
        if h == e or h.endswith("." + e):
            return True
    return False


def is_safe_ip(ip: str) -> bool:
    """Whether a resolved IP is a PUBLIC address safe to connect to. Blocks private,
    loopback, link-local (incl. the 169.254.169.254 / fd00:ec2::254 metadata endpoint),
    reserved, multicast, and unspecified. Unparseable → unsafe (fail closed)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    ):
        return False
    # RFC 6598 shared/CGNAT space (100.64.0.0/10) is NOT flagged private by `ipaddress`,
    # but AWS uses it for internal routing (EKS pod networking, some NAT paths) — block it.
    if isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT:
        return False
    # IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254) must be checked on the mapped v4.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return is_safe_ip(str(mapped))
    return True


def validate_url(url: str, allowlist: tuple[str, ...]) -> str:
    """Validate a fetch target's scheme + host (NOT its IPs — the caller resolves and
    checks those with `is_safe_ip`). Returns the lowercased host on success; raises
    `WebFetchError` otherwise. Call this on the initial URL AND on every redirect target."""
    if not url or not isinstance(url, str):
        raise WebFetchError("missing url")
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise WebFetchError(f"only https is allowed (got {parts.scheme or 'no scheme'})")
    host = (parts.hostname or "").lower()
    if not host:
        raise WebFetchError("url has no host")
    # A bare IP literal as host bypasses DNS + the host allowlist — always reject (use an
    # allowlisted domain). NB: WebFetchError subclasses ValueError, so detect the IP
    # literal WITHOUT raising inside the try (that would be swallowed by `except`).
    if _is_ip_literal(host):
        raise WebFetchError("a raw IP host is not allowed; use an allowlisted domain")
    if not is_allowed_host(host, allowlist):
        raise WebFetchError(f"host not on the allowlist: {host}")
    return host


def _is_ip_literal(host: str) -> bool:
    """Whether `host` is a bare IPv4/IPv6 literal (no DNS name)."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
