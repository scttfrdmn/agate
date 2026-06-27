"""Web-fetch MCP server (#192) — the gated reach beyond the corpus.

AgentCore Gateway invokes this Lambda as an MCP-Lambda target. It is the EFFECT half of
the §5 split: #113 IAM already fenced WHICH agents may invoke this gateway tool, and
AgentCore Cedar permitted the `CallTool`; this server enforces what the fetch may DO.
The shape mirrors the slurm tool exactly — verify the inbound identity, derive the
boundary ONLY from the verified credential (never the tool payload), then act under it.

The fetch is the most security-sensitive action in agate: the Lambda has NO VPC (NO
CLOCKS), so the `agate.webfetch` SSRF guard IS the boundary. Every fetch and every
redirect hop is validated (https-only, host allowlist, public-IP only — blocks the
metadata endpoint + internal services), the socket is PINNED to the guard-validated IP
(no second DNS lookup — TOCTOU rebinding defence), and the connection follows NO
automatic redirects: a `Location` is re-validated by hand before the next hop. Fails
closed: any verification/scoping/allowlist/SSRF error returns an error envelope, never
bytes.

NOTE: pricing/budget-gating of fetches (folding the cost into the #81 cascade, like the
slurm tool gates a submit) is a deliberate follow-up — today the controls are the host
allowlist + the SSRF guard + IAM/Cedar deny-by-absence, not a per-fetch budget debit.
"""

from __future__ import annotations

import json
import os
import time

from agate.identity import acting_as_from_session
from agate.jwt_verify import TokenError, config_from_env, verify_token
from agate.tags import ClaimsError, claims_to_tags, role_session_name
from agate.webfetch import (
    WebFetchError,
    gate_fetch,
    is_safe_ip,
    parse_allowlist,
    validate_url,
)

# Institution-configured host allowlist (comma/space separated). EMPTY = deny all — the
# capability is inert until an institution names the hosts an agent may reach.
ALLOWLIST = parse_allowlist(os.environ.get("AGATE_WEBFETCH_ALLOWLIST", ""))
MAX_BYTES = int(os.environ.get("AGATE_WEBFETCH_MAX_BYTES", str(2 * 1024 * 1024)))
MAX_REDIRECTS = int(os.environ.get("AGATE_WEBFETCH_MAX_REDIRECTS", "3"))
TIMEOUT_S = int(os.environ.get("AGATE_WEBFETCH_TIMEOUT_S", "10"))
# Flat per-fetch price for the budget cascade (#120). Non-zero default so the gate
# actually bites — a $0 price always fits any budget (the gate would be a no-op). An
# institution tunes it; vendor-quoted pricing for a paid API is a later refinement.
FETCH_PRICE_USD = float(os.environ.get("AGATE_WEBFETCH_PRICE_USD", "0.001"))
SPEND_TABLE = os.environ.get("AGATE_SPEND_TABLE", "")
BUDGET_TABLE = os.environ.get("AGATE_BUDGET_TABLE", "")
REGION = os.environ.get("AGATE_REGION") or os.environ.get("AWS_REGION") or "us-east-1"


class WebFetchToolError(ValueError):
    """A web-fetch call that cannot be served safely. Fail closed."""


def validate_idp_token(token: str) -> dict:
    """Verify the campus-IdP token (real RS256/JWKS) — the SAME verifier the broker,
    retrieval proxy, and slurm tool use. The inbound identity is the verified user the
    agent acts for."""
    if not token or not isinstance(token, str):
        raise WebFetchToolError("missing idp_token")
    try:
        return verify_token(token, **config_from_env())
    except TokenError as exc:
        raise WebFetchToolError(f"token verification failed: {exc}") from exc


def safe_fetch(url: str, *, resolve, fetch) -> dict:
    """Fetch `url` under the full SSRF guard, following at most MAX_REDIRECTS hops, each
    re-validated. `resolve(host) -> [ip,...]` and `fetch(url, pinned_ip) -> (status,
    headers, body, location|None)` are injected transports (the DNS + HTTP edge), so this
    stays unit-testable. Returns {url (final), status, bytes, content}. Raises on any guard
    failure — fail closed, never returns bytes from an unvalidated hop.

    TOCTOU defence: every resolved IP is validated, and the FIRST validated IP is PINNED
    and handed to `fetch` so the socket connects to the address the guard approved — not a
    second, attacker-controlled re-resolution (DNS rebinding)."""
    seen = 0
    current = url
    while True:
        host = validate_url(current, ALLOWLIST)  # https + allowlist (+ reject raw IP)
        ips = resolve(host)
        if not ips:
            raise WebFetchToolError(f"could not resolve {host}")
        # EVERY resolved address must be public — a host that resolves to ANY private/
        # metadata IP is rejected (defeats DNS rebinding to a single bad record).
        for ip in ips:
            if not is_safe_ip(ip):
                raise WebFetchToolError(f"host {host} resolves to a blocked address")
        # Pin to the first validated IP: the socket connects HERE, not a fresh lookup.
        status, _headers, body, location = fetch(current, ips[0])
        if status in (301, 302, 303, 307, 308) and location:
            seen += 1
            if seen > MAX_REDIRECTS:
                raise WebFetchToolError("too many redirects")
            current = location  # re-validated at the top of the loop (no auto-follow)
            continue
        data = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
        if len(data) > MAX_BYTES:
            data = data[:MAX_BYTES]
        return {
            "url": current,  # the FINAL, guard-approved URL (provenance can't be laundered)
            "status": status,
            "bytes": len(data),
            "content": data.decode("utf-8", errors="replace"),
        }


def fetch_tool(tags, subject, url: str, *, resolve, fetch, spend_reader) -> dict:
    """`web-fetch`: budget-gate, then validate + fetch one allowlisted URL, attributing the
    action to the verified user (#137). The url's content is what the agent asked for;
    tenant/scope are the verified credential's, used for attribution + the budget cascade."""
    # Gate the (priced) fetch on the budget cascade BEFORE any bytes leave — over-budget is
    # rejected pre-call, naming the breaching node (the chokepoint/slurm pattern, #120/#81).
    decision = gate_fetch(
        tenant=tags.tenant, scope=tags.scope, price_usd=FETCH_PRICE_USD, spend_lookup=spend_reader
    )
    if not decision.allowed:
        raise WebFetchToolError(
            f"fetch rejected: over budget at {decision.cascade.breaching_node!r} "
            f"({decision.reason})"
        )
    result = safe_fetch(url, resolve=resolve, fetch=fetch)
    session_name = role_session_name(tags.tenant, subject)
    acting = acting_as_from_session(
        session_name,
        agent=f"{tags.tenant}/web-fetch",
        remit={"scope": tags.scope, "tool": "web-fetch", "url": result["url"]},
    )
    return {
        **result,
        "price_usd": FETCH_PRICE_USD,
        "source_system": "web",
        "source_item": result["url"],
        "actingAs": acting.to_dict(),
    }


# --- live AWS/network edge (injected into the pure logic above) --------------


def _real_resolve(host: str) -> list:  # pragma: no cover
    """Resolve a host to its IPs (the DNS edge). Wired at runtime."""
    import socket

    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def _real_fetch(url: str, pinned_ip: str):  # pragma: no cover
    """Fetch a URL with the socket PINNED to `pinned_ip` (the guard-validated address)
    while SNI + certificate validation stay on the URL's real hostname — so the connection
    goes to the address the SSRF guard approved, not a second DNS lookup urllib would do
    (TOCTOU rebinding defence). Redirects are DISABLED (the caller re-validates each hop).
    Returns (status, headers, body, location). Stdlib only (http.client)."""
    import http.client
    import socket
    import ssl
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or 443
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

    class _PinnedHTTPSConnection(http.client.HTTPSConnection):
        # Connect the raw socket to the validated IP, then wrap TLS with server_hostname =
        # the real host (so SNI + cert check verify the hostname, not the IP). This is the
        # only place the connect target is chosen — no re-resolution can sneak in.
        def connect(self):
            sock = socket.create_connection((pinned_ip, port), timeout=self.timeout)
            self.sock = self._context.wrap_socket(sock, server_hostname=host)

    ctx = ssl.create_default_context()
    conn = _PinnedHTTPSConnection(host, port, timeout=TIMEOUT_S, context=ctx)
    try:
        conn.request("GET", path, headers={"Host": host, "User-Agent": "agate-webfetch/1.0"})
        resp = conn.getresponse()
        status = resp.status
        headers = {k: v for k, v in resp.getheaders()}
        location = headers.get("Location")
        if status in (301, 302, 303, 307, 308):
            return status, headers, b"", location
        return status, headers, resp.read(MAX_BYTES + 1), None
    finally:
        conn.close()


def _real_spend_reader(label: str) -> tuple[float, float | None]:  # pragma: no cover
    """Read the live (spend, budget) for a cascade node from the spend/budget tables. A
    missing budget row => (spend, None) = no cap at that node. Wired at deploy."""
    import boto3

    ddb = boto3.resource("dynamodb", region_name=REGION)
    period = time.strftime("%Y-%m", time.gmtime())
    spend_item = ddb.Table(SPEND_TABLE).get_item(Key={"pk": f"scope#{label}#{period}"}).get("Item")
    budget_item = (
        ddb.Table(BUDGET_TABLE).get_item(Key={"pk": f"scope#{label}#{period}"}).get("Item")
    )
    spend = float(spend_item["spend_usd"]) if spend_item and "spend_usd" in spend_item else 0.0
    budget = (
        float(budget_item["budget_usd"]) if budget_item and "budget_usd" in budget_item else None
    )
    return spend, budget


def process(req: dict) -> dict:
    """Route one MCP tool call. `req` carries the verified `idp_token`, the `tool`
    (`web-fetch`), and the `url`. Tenant/scope come from the token; the url's host must be
    allowlisted and resolve to a public address, and the fetch must fit the budget cascade."""
    claims = validate_idp_token(req.get("idp_token", ""))
    try:
        tags = claims_to_tags(claims)
    except ClaimsError as exc:
        raise WebFetchToolError(f"cannot scope session: {exc}") from exc
    subject = str(claims.get("sub") or claims.get("subject") or "agate-user")

    tool = req.get("tool", "web-fetch")
    if tool != "web-fetch":
        raise WebFetchToolError(f"unknown tool: {tool!r}")
    url = req.get("url")
    if not isinstance(url, str) or not url:
        raise WebFetchToolError("missing url")
    return fetch_tool(
        tags, subject, url,
        resolve=_real_resolve, fetch=_real_fetch, spend_reader=_real_spend_reader,
    )


def handler(event: dict, context: object) -> dict:
    """MCP-Lambda target entry point. Fail-closed."""
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode("utf-8")
        req = json.loads(body) if isinstance(body, str) else body
        return _resp(200, process(req))
    except (WebFetchToolError, WebFetchError) as exc:
        return _resp(403, {"error": "not_entitled", "detail": str(exc)})
    except Exception:  # noqa: BLE001 — last-resort fail-closed
        import logging

        logging.exception("webfetch_tool_error")
        return _resp(500, {"error": "webfetch_tool_error"})


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
