"""Runtime → governed-tool bridge for agent-cell research (#248).

The research loop (`agate.research_loop`) needs two governed edges — `search` and `fetch`. Like the
memory bridge (#130b), the container must NOT reach the web out itself: it runs under ONE shared
execution role (not tenant-tagged), so it invokes the already-reviewed web-search / web-fetch tool
Lambdas, FORWARDING the verified `idp_token` it received. Each tool re-verifies identity at its own
boundary, derives tenant/scope from the token, runs the SSRF/allowlist/budget guards, and acts
under the tenant-fenced credential. The security boundary is enforced ONCE, server-side; the
container trusts nothing of its own about identity or egress.

This keeps the loop's governed-egress invariant honest end to end: `search` returns only URLs the
web-search tool surfaced, and `fetch` dereferences a URL ONLY through the web-fetch tool (which
re-validates it). The loop already refuses to fetch a URL no search surfaced; here that URL is also
re-validated by web-fetch, so there is exactly one egress implementation.

OPT-IN / fail-closed: when a tool ARN is unset the corresponding edge returns empty / raises — a
research cell cannot silently reach an ungoverned path. A search failure yields no URLs (the loop
proceeds with what it has); a fetch failure raises so the loop never treats unfetched bytes as
evidence.
"""

from __future__ import annotations

import json
import os

import boto3

REGION = os.environ.get("AGATE_REGION", "us-east-1")
WEBSEARCH_TOOL_ARN = os.environ.get("AGATE_WEBSEARCH_TOOL_ARN", "")
WEBFETCH_TOOL_ARN = os.environ.get("AGATE_WEBFETCH_TOOL_ARN", "")

_lambda = None


def _client():
    global _lambda
    if _lambda is None:
        _lambda = boto3.client("lambda", region_name=REGION)
    return _lambda


def _invoke(arn: str, req: dict) -> dict | None:
    """Invoke a tool Lambda with one MCP request envelope; return its parsed 200 body or None on
    any non-200 / failure. Never raises here — callers decide how a failure maps to their edge."""
    if not arn:
        return None
    try:
        resp = _client().invoke(
            FunctionName=arn,
            InvocationType="RequestResponse",
            Payload=json.dumps({"body": json.dumps(req)}).encode("utf-8"),
        )
        out = json.loads(resp["Payload"].read() or b"{}")
        if out.get("statusCode") != 200:
            return None
        return json.loads(out.get("body") or "{}")
    except Exception:  # noqa: BLE001 — a tool failure maps to an empty/raised edge, not a crash
        return None


class ResearchFetchError(RuntimeError):
    """A governed fetch could not be served — the loop must not treat this as evidence."""


def make_search(idp_token: str):
    """Build the loop's `search(query) -> [url,...]` edge, forwarding the verified token to the
    web-search tool. Returns [] when the tool is unwired or fails — the loop then plans with the
    URLs it already has (a search failure is not fatal to a partial answer)."""

    def search(query: str) -> list[str]:
        body = _invoke(
            WEBSEARCH_TOOL_ARN, {"idp_token": idp_token, "tool": "web-search", "query": query}
        )
        if not body:
            return []
        results = body.get("results")
        return [u for u in results if isinstance(u, str)] if isinstance(results, list) else []

    return search


def make_fetch(idp_token: str):
    """Build the loop's `fetch(url) -> content` edge, forwarding the verified token to the
    web-fetch tool (which RE-VALIDATES the URL through its own SSRF/allowlist/budget guard). Raises
    `ResearchFetchError` on any failure so the loop never records unfetched/blocked bytes as
    evidence — the single egress implementation stays authoritative."""

    def fetch(url: str) -> str:
        body = _invoke(WEBFETCH_TOOL_ARN, {"idp_token": idp_token, "tool": "web-fetch", "url": url})
        if not body or "content" not in body:
            raise ResearchFetchError(f"web-fetch did not return content for {url!r}")
        return str(body["content"])

    return fetch
