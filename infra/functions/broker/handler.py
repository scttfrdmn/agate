"""The claims -> scoped-STS broker (design §3.1, the load-bearing crux).

Per-request, scales to zero (Lambda) — no clock. The flow:

  1. Receive the campus-IdP token (OIDC id_token / SAML assertion) from the SPA.
  2. Validate it against the IdP's published keys (JWKS for OIDC).
  3. Run the PURE `claims_to_tags()` to derive the four `agate:` session tags,
     INCLUDING the derived `agate:tier` (which Cognito principal-tag mapping cannot
     compute on its own — this is why the broker exists).
  4. Call `sts:AssumeRole` on the authenticated role, passing the computed tags
     as session `Tags`. The returned credentials are the role *narrowed by those
     tags* — exactly the user's entitlement, nothing more.

We use AssumeRole (not AssumeRoleWithWebIdentity) precisely because the tier is
*derived*: AssumeRoleWithWebIdentity can only echo principal-tag claims already
present in the token, so it cannot carry a value the broker computes. AssumeRole's
`Tags` parameter can. The broker's own execution role is trusted to assume the
authenticated role; it holds no model/data permissions itself.

Security posture (memo §10.1): this is the single most security-critical path.
It FAILS CLOSED — any validation or translation error vends NO credentials.
"""

from __future__ import annotations

import ipaddress
import json
import os

import boto3
from agate.jwt_verify import TokenError, config_from_env, verify_token
from agate.tags import ClaimsError, claims_to_tags

# Resolved at deploy time (set as Lambda env vars by the identity stack).
AUTHENTICATED_ROLE_ARN = os.environ.get("AGATE_AUTHENTICATED_ROLE_ARN", "")
SESSION_DURATION_SECONDS = int(os.environ.get("AGATE_SESSION_DURATION_SECONDS", "900"))  # 15 min
# Optional source-IP allowlist (comma-separated CIDRs/IPs). Empty = allow all.
# HTTP APIs (API Gateway v2) have no resource policy, so we fence the source IP
# here in the handler instead. Fails closed: a malformed allowlist denies all.
IP_ALLOWLIST = os.environ.get("AGATE_IP_ALLOWLIST", "").strip()

_sts = boto3.client("sts")


def _source_ip(event: dict) -> str:
    """The caller's IP from an API Gateway v2 (HTTP API) proxy event."""
    return str(((event.get("requestContext") or {}).get("http") or {}).get("sourceIp") or "")


def ip_allowed(source_ip: str, allowlist: str) -> bool:
    """True if source_ip is within any CIDR/IP in the allowlist. Empty allowlist =
    allow all (no restriction configured). Fails closed on a malformed entry or a
    missing/blank source IP when an allowlist IS set."""
    if not allowlist:
        return True
    if not source_ip:
        return False
    try:
        addr = ipaddress.ip_address(source_ip)
    except ValueError:
        return False
    for entry in (e.strip() for e in allowlist.split(",")):
        if not entry:
            continue
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            # A bad allowlist entry must not silently widen access.
            return False
    return False


class BrokerError(Exception):
    """Vend-no-credentials error. The handler maps this to a 4xx, never a creds payload."""


def validate_idp_token(token: str) -> dict[str, object]:
    """Verify the campus-IdP token (real RS256/JWKS) and return its claims.

    Uses the shared `agate.jwt_verify` — signature against the IdP JWKS, plus
    iss/aud/exp/sub. The OIDC config (JWKS URL / issuer / audience) comes from env
    set at deploy time. If the verifier is unconfigured or the token fails any
    check, this raises BrokerError and the broker vends NO credentials (fail closed).
    There is no unsigned-token path — that was the SEC-4 placeholder, now removed.
    """
    cfg = config_from_env()
    try:
        return verify_token(token, **cfg)
    except TokenError as exc:
        raise BrokerError(str(exc)) from exc


def vend_credentials(claims: dict[str, object], *, subject: str) -> dict[str, object]:
    """Translate claims -> tags and assume the authenticated role narrowed by them."""
    if not AUTHENTICATED_ROLE_ARN:
        raise BrokerError("broker misconfigured: no authenticated role ARN")

    try:
        tags = claims_to_tags(claims)
    except ClaimsError as exc:
        # Fail closed: we could not scope safely, so we vend nothing.
        raise BrokerError(f"cannot scope session: {exc}") from exc

    # RoleSessionName ties every downstream CloudTrail / Bedrock log line to the
    # federated subject (security memo §4: fully attributable).
    session_name = _safe_session_name(subject)

    resp = _sts.assume_role(
        RoleArn=AUTHENTICATED_ROLE_ARN,
        RoleSessionName=session_name,
        DurationSeconds=SESSION_DURATION_SECONDS,
        Tags=tags.to_sts_tags(),
        # Transitive so the tags survive any further role chaining a tool performs.
        TransitiveTagKeys=[t["Key"] for t in tags.to_sts_tags()],
    )
    creds = resp["Credentials"]
    return {
        "credentials": {
            "accessKeyId": creds["AccessKeyId"],
            "secretAccessKey": creds["SecretAccessKey"],
            "sessionToken": creds["SessionToken"],
            "expiration": creds["Expiration"].isoformat(),
        },
        # Echo the scope back for the SPA's display only — NOT authority.
        "scope": {
            "affiliation": tags.affiliation,
            "tenant": tags.tenant,
            "courses": list(tags.courses),
            "tier": tags.tier,
        },
    }


def _safe_session_name(subject: str) -> str:
    """STS RoleSessionName: <=64 chars, [\\w+=,.@-]."""
    import re

    name = re.sub(r"[^\w+=,.@-]", "-", subject or "agate-user")
    return name[:64] or "agate-user"


def handler(event: dict, context: object) -> dict:
    """Lambda entry point (API Gateway v2 HTTP API proxy event)."""
    try:
        # Optional network fence before any work (the HTTP API itself has no
        # resource policy). Returns the same terse 403 as an entitlement failure.
        if not ip_allowed(_source_ip(event), IP_ALLOWLIST):
            return _resp(403, {"error": "not_entitled"})

        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode("utf-8")
        payload = json.loads(body) if isinstance(body, str) else body

        token = payload.get("idp_token", "")
        claims = validate_idp_token(token)
        subject = str(claims.get("sub") or claims.get("subject") or "agate-user")
        result = vend_credentials(claims, subject=subject)
        return _resp(200, result)
    except BrokerError as exc:
        # Deliberately terse: do not leak why scoping failed to the client.
        return _resp(403, {"error": "not_entitled", "detail": str(exc)})
    except Exception:  # noqa: BLE001 — last-resort fail-closed
        return _resp(500, {"error": "broker_error"})


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
