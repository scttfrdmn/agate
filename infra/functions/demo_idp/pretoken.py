"""Cognito pre-token-generation trigger for the demo IdP.

Cognito stores custom attributes prefixed `custom:` and would emit them as
`custom:affiliation` etc. in the id_token. The broker/agent consume the `agate`
claim names (`affiliation`, `tenant`, `courses`, `grant`) via `claims_to_tags`,
so this trigger copies the demo user's custom attributes up to those top-level
claim names. This makes the demo Cognito User Pool issue tokens the gateway
verifies and scopes exactly like a real campus IdP would — no gateway changes.

Pure attribute mapping; no AWS calls. Cognito invokes it synchronously at token
issue (per-request, no clock).
"""

from __future__ import annotations

# eduPerson-style claims the gateway's claims_to_tags understands. `role` carries
# the operator role (admin) for the governed-access console (Phase 9 Track 1).
_CLAIMS = ("affiliation", "tenant", "courses", "grant", "role")


def handler(event: dict, context: object) -> dict:
    """Surface custom:<name> attributes as top-level <name> claims in the id_token.

    Cognito's pre-token-generation event carries the user's attributes under
    request.userAttributes; we add the mapped claims under
    response.claimsOverrideDetails.claimsToAddOrOverride.
    """
    attrs = (event.get("request") or {}).get("userAttributes") or {}
    add: dict[str, str] = {}
    for name in _CLAIMS:
        value = attrs.get(f"custom:{name}")
        if value:
            add[name] = value

    event.setdefault("response", {})["claimsOverrideDetails"] = {"claimsToAddOrOverride": add}
    return event
