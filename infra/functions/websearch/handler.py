"""Web-search MCP server (#248) — governed SEARCH, the prerequisite for capped research agents.

AgentCore Gateway invokes this Lambda as an MCP-Lambda target, exactly like `web-fetch`: #113 IAM
fences WHICH agents may invoke it, AgentCore Cedar permits the `CallTool`, and this server enforces
what the search may DO. It is the EFFECT half of the §5 split — verify the inbound identity, derive
the boundary ONLY from the verified credential (never the tool payload), then act under it.

The shape mirrors the web-fetch tool exactly, reusing the SAME egress-safety implementation (the
`agate.websearch` guard re-exports web-fetch's SSRF/allowlist checks): the SEARCH ENDPOINT the
agent queries must be https, on the institution's SEARCH allowlist (a DISTINCT, narrower config
than the fetch allowlist), and resolve to public IPs only — with the socket PINNED to the guard-
validated IP (TOCTOU rebinding defence) and NO auto-redirects. A search is a PRICED action gated on
the budget cascade before the query leaves.

Crucially, this tool returns RESULT URLs — it does NOT fetch them. A later `web-fetch` re-validates
each result URL through its own guard before any bytes are read, so web-search opens NO new egress
path (it only reaches the one approved search endpoint). Fails closed: any verification / scoping /
allowlist / SSRF / budget error returns an error envelope, never results.
"""

from __future__ import annotations

import json
import os
import time
from urllib.parse import quote

from agate.identity import acting_as_from_session
from agate.jwt_verify import TokenError, config_from_env, verify_token
from agate.tags import ClaimsError, claims_to_tags, role_session_name
from agate.websearch import (
    WebSearchError,
    gate_search,
    is_safe_search_ip,
    parse_search_allowlist,
    validate_search_endpoint,
)

# The SEARCH-endpoint allowlist — a DISTINCT, narrower config than the fetch allowlist (an
# institution approves specific search endpoints, e.g. a library discovery API). EMPTY = deny-all:
# the capability is inert until an institution names an endpoint. The endpoint TEMPLATE carries a
# `{query}` placeholder the query is URL-encoded into.
ALLOWLIST = parse_search_allowlist(os.environ.get("AGATE_WEBSEARCH_ALLOWLIST", ""))
ENDPOINT_TEMPLATE = os.environ.get("AGATE_WEBSEARCH_ENDPOINT", "")
MAX_RESULTS = int(os.environ.get("AGATE_WEBSEARCH_MAX_RESULTS", "10"))
# Cap the endpoint response body read (like web-fetch's MAX_BYTES) so a misbehaving allowlisted
# endpoint returning a huge body can't exhaust the Lambda (DoS defense-in-depth).
MAX_BYTES = int(os.environ.get("AGATE_WEBSEARCH_MAX_BYTES", str(2 * 1024 * 1024)))
TIMEOUT_S = int(os.environ.get("AGATE_WEBSEARCH_TIMEOUT_S", "10"))
# Flat per-search price for the budget cascade (#120) — non-zero default so the gate bites (a $0
# price always fits any budget). An institution tunes it; vendor pricing is a later refinement.
SEARCH_PRICE_USD = float(os.environ.get("AGATE_WEBSEARCH_PRICE_USD", "0.005"))
SPEND_TABLE = os.environ.get("AGATE_SPEND_TABLE", "")
BUDGET_TABLE = os.environ.get("AGATE_BUDGET_TABLE", "")
REGION = os.environ.get("AGATE_REGION") or os.environ.get("AWS_REGION") or "us-east-1"


class WebSearchToolError(ValueError):
    """A web-search call that cannot be served safely. Fail closed."""


def validate_idp_token(token: str) -> dict:
    """Verify the campus-IdP token (real RS256/JWKS) — the SAME verifier the broker, retrieval
    proxy, slurm, and web-fetch tools use. The inbound identity is the verified user the agent
    acts for."""
    if not token or not isinstance(token, str):
        raise WebSearchToolError("missing idp_token")
    try:
        return verify_token(token, **config_from_env())
    except TokenError as exc:
        raise WebSearchToolError(f"token verification failed: {exc}") from exc


def _endpoint_url(query: str) -> str:
    """Build the concrete endpoint URL for `query` from the configured template. The query is
    URL-encoded so it cannot inject path/host segments; the resulting URL is then re-validated by
    the SSRF guard (https + allowlist) before it is queried."""
    if not ENDPOINT_TEMPLATE:
        raise WebSearchToolError("no search endpoint configured")
    q = quote(query.strip(), safe="")
    if "{query}" in ENDPOINT_TEMPLATE:
        return ENDPOINT_TEMPLATE.replace("{query}", q)
    joiner = "&" if "?" in ENDPOINT_TEMPLATE else "?"
    return f"{ENDPOINT_TEMPLATE}{joiner}q={q}"


def safe_search(query: str, *, resolve, http_get, extract) -> list[str]:
    """Query the guard-validated search endpoint and return candidate RESULT URLs (never fetched
    here). `resolve(host) -> [ip,...]`, `http_get(url, pinned_ip) -> body`, and
    `extract(body) -> [url,...]` are injected transports (DNS + HTTP + result parsing), so this
    stays unit-testable. Fails closed: the endpoint passes the FULL SSRF guard (https, allowlist,
    public-IP only, pinned socket) before any query leaves."""
    endpoint = _endpoint_url(query)
    host = validate_search_endpoint(endpoint, ALLOWLIST)  # https + allowlist (+ reject raw IP)
    ips = resolve(host)
    if not ips:
        raise WebSearchToolError(f"could not resolve {host}")
    # EVERY resolved address must be public — a host resolving to ANY private/metadata IP is
    # rejected (defeats DNS rebinding to a single bad record).
    for ip in ips:
        if not is_safe_search_ip(ip):
            raise WebSearchToolError(f"endpoint {host} resolves to a blocked address")
    body = http_get(endpoint, ips[0])  # socket pinned to the validated IP
    urls: list[str] = []
    for url in extract(body):
        # Each result URL is returned as-is; it is NOT fetched here. web-fetch re-validates it
        # through its own guard before any bytes are read, so no egress path opens here.
        if isinstance(url, str) and url and url not in urls:
            urls.append(url)
        if len(urls) >= MAX_RESULTS:
            break
    return urls


def search_tool(tags, subject, query: str, *, resolve, http_get, extract, spend_reader) -> dict:
    """`web-search`: budget-gate, then query the allowlisted endpoint and return result URLs,
    attributing the action to the verified user (#137). tenant/scope are the verified credential's,
    used for attribution + the budget cascade — never taken from the payload."""
    if not isinstance(query, str) or not query.strip():
        raise WebSearchToolError("missing query")
    # Gate the (priced) search on the budget cascade BEFORE the query leaves — over-budget is
    # rejected pre-call, naming the breaching node (same tenant floor as gate_fetch, #248).
    decision = gate_search(
        tenant=tags.tenant,
        scope=tags.scope,
        price_usd=SEARCH_PRICE_USD,
        spend_lookup=spend_reader,
    )
    if not decision.allowed:
        raise WebSearchToolError(
            f"search rejected: over budget at {decision.cascade.breaching_node!r} "
            f"({decision.reason})"
        )
    results = safe_search(query, resolve=resolve, http_get=http_get, extract=extract)
    session_name = role_session_name(tags.tenant, subject)
    acting = acting_as_from_session(
        session_name,
        agent=f"{tags.tenant}/web-search",
        remit={"scope": tags.scope, "tool": "web-search", "query": query.strip()[:256]},
    )
    return {
        "results": results,
        "count": len(results),
        "price_usd": SEARCH_PRICE_USD,
        "source_system": "web-search",
        "actingAs": acting.to_dict(),
    }


# --- live AWS/network edge (injected into the pure logic above) --------------


def _real_resolve(host: str) -> list:  # pragma: no cover
    """Resolve a host to its IPs (the DNS edge). Wired at runtime."""
    import socket

    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def _real_http_get(url: str, pinned_ip: str) -> str:  # pragma: no cover
    """GET a URL with the socket PINNED to `pinned_ip` (the guard-validated address) while SNI +
    cert validation stay on the URL's real hostname — the connection goes to the address the SSRF
    guard approved, not a second DNS lookup. Redirects DISABLED. Mirrors webfetch._real_fetch."""
    import http.client
    import socket
    import ssl
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or 443
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

    class _PinnedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection((pinned_ip, port), timeout=self.timeout)
            self.sock = self._context.wrap_socket(sock, server_hostname=host)

    ctx = ssl.create_default_context()
    conn = _PinnedHTTPSConnection(host, port, timeout=TIMEOUT_S, context=ctx)
    try:
        conn.request("GET", path, headers={"Host": host, "User-Agent": "agate-websearch/1.0"})
        resp = conn.getresponse()
        # Bounded read — never slurp an unbounded body from the endpoint (mirrors webfetch).
        return resp.read(MAX_BYTES + 1)[:MAX_BYTES].decode("utf-8", errors="replace")
    finally:
        conn.close()


def _extract_result_urls(body: str) -> list:  # pragma: no cover
    """Parse result URLs from a JSON search response. Expects a shape like
    {"results": [{"url": "..."}, ...]} or {"items": [{"link": "..."}]}; tolerant of both.
    A non-JSON body yields no results (fail closed to empty, never an unvalidated guess)."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return []
    rows = data.get("results") or data.get("items") or []
    urls = []
    for row in rows:
        if isinstance(row, dict):
            u = row.get("url") or row.get("link")
            if isinstance(u, str) and u:
                urls.append(u)
    return urls


def _real_spend_reader(label: str) -> tuple[float, float | None]:  # pragma: no cover
    """Read the live (spend, budget) for a cascade node from the spend/budget tables. A missing
    budget row => (spend, None) = no cap at that node. Wired at deploy (mirrors webfetch)."""
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
    """Route one MCP tool call. `req` carries the verified `idp_token`, the `tool` (`web-search`),
    and the `query`. Tenant/scope come from the token; the endpoint host must be allowlisted and
    resolve to a public address, and the search must fit the budget cascade."""
    claims = validate_idp_token(req.get("idp_token", ""))
    try:
        tags = claims_to_tags(claims)
    except ClaimsError as exc:
        raise WebSearchToolError(f"cannot scope session: {exc}") from exc
    subject = str(claims.get("sub") or claims.get("subject") or "agate-user")

    tool = req.get("tool", "web-search")
    if tool != "web-search":
        raise WebSearchToolError(f"unknown tool: {tool!r}")
    return search_tool(
        tags,
        subject,
        req.get("query", ""),
        resolve=_real_resolve,
        http_get=_real_http_get,
        extract=_extract_result_urls,
        spend_reader=_real_spend_reader,
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
    except (WebSearchToolError, WebSearchError) as exc:
        return _resp(403, {"error": "not_entitled", "detail": str(exc)})
    except Exception:  # noqa: BLE001 — last-resort fail-closed
        import logging

        logging.exception("websearch_tool_error")
        return _resp(500, {"error": "websearch_tool_error"})


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
