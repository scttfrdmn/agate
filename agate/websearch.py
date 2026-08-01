"""Guard for the gated web-search capability (#248) — pure, fail-closed.

The prerequisite for capped research agent cells: a research agent needs governed SEARCH, not just
fetch-a-known-URL (web-fetch, #192). This module is the security boundary for calling an
institution-approved search ENDPOINT, and it deliberately REUSES the web-fetch guard so there is
exactly one egress-safety implementation to reason about:

  * the search endpoint URL passes the SAME default-deny checks as any fetch — https-only, host on
    the institution's search allowlist (empty allowlist = deny-all), and every resolved IP public
    (`is_safe_ip` blocks private/loopback/link-local/metadata/CGNAT);
  * a search is a PRICED action, gated on the budget cascade before the request leaves — the same
    hierarchical rule the chat/fetch/slurm paths use (#81/#120);
  * the result URLs a search returns are NOT fetched here — a later `web-fetch` re-validates each
    through its own guard, so web-search opens no new egress path (it only reaches the one approved
    search endpoint).

Like `webfetch`, DNS is resolved by the caller (the Lambda) and the resolved IPs are passed in, so
this module stays AWS- and network-free and is exhaustively unit-testable. The search allowlist is a
DISTINCT config from the fetch allowlist: an institution approves specific search endpoints (e.g. a
library discovery API), which is a much narrower set than the pages a fetch may read.
"""

from __future__ import annotations

from dataclasses import dataclass

from cost.precall import CascadeResult, evaluate_priced_cascade

from agate.webfetch import (
    WebFetchError,
    fetch_cascade_nodes,
    is_allowed_host,
    is_safe_ip,
    parse_allowlist,
    validate_url,
)

# The search-endpoint allowlist is parsed exactly like the fetch allowlist (comma/space-separated
# hosts, empty = deny-all) — re-exported so a caller has one obvious import for both.
parse_search_allowlist = parse_allowlist

# Re-export the shared IP/host guards so a caller can check a resolved endpoint IP without reaching
# into webfetch (single implementation, one import surface for the search path).
is_safe_search_ip = is_safe_ip
is_allowed_search_host = is_allowed_host


class WebSearchError(ValueError):
    """A search endpoint/query that fails the guard. Fail closed — never queried."""


def validate_search_endpoint(url: str, allowlist: tuple[str, ...]) -> str:
    """Validate the SEARCH ENDPOINT's scheme + host against the search allowlist (NOT its IPs — the
    caller resolves and checks those with `is_safe_search_ip`). Returns the lowercased host on
    success; raises `WebSearchError` otherwise. This is the same guard as a fetch target, applied to
    the endpoint the agent queries — https-only, allowlisted domain, no bare-IP host."""
    try:
        return validate_url(url, allowlist)
    except WebFetchError as exc:
        # Re-wrap so callers catch a search-specific error; the guard logic is identical.
        raise WebSearchError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class SearchDecision:
    """The budget-cascade verdict for a priced search — `allowed` plus the underlying
    `CascadeResult` (which names the first breaching node + reason on reject)."""

    allowed: bool
    cascade: CascadeResult
    reason: str


def gate_search(*, tenant: str, scope: str, price_usd: float, spend_lookup) -> SearchDecision:
    """Gate a web search on the budget cascade BEFORE the query leaves — SAME signature and floor as
    `webfetch.gate_fetch`, so a search can never have a weaker budget guarantee than a fetch. Builds
    the cascade from the caller's scope ancestors via `fetch_cascade_nodes`, which ALWAYS yields at
    least the tenant node (so there is no "empty nodes → unconditional allow" footgun). The first
    node to reject short-circuits and is named; a node with no budget imposes no cap. Returns the
    decision; the handler searches only on allow. `spend_lookup(label) -> (spend, budget|None)`."""
    nodes = fetch_cascade_nodes(tenant, scope, spend_lookup)
    result = evaluate_priced_cascade(price_usd=price_usd, nodes=nodes)
    return SearchDecision(allowed=result.decision == "allow", cascade=result, reason=result.reason)
