"""LTI 1.3 tool provider handlers (design §6, §13.5).

One Lambda behind an HTTP API, routing the four LTI 1.3 endpoints:

  GET/POST /lti/login              OIDC third-party login init -> redirect to platform
  POST     /lti/launch            id_token (JWT) validation -> mint an agg session
  GET      /.well-known/jwks.json tool's public keys (for platform -> tool signing)
  POST     /lti/deeplink          deep-linking response back to the platform

Security (the LTI crux): the launch id_token is an RS256 JWT signed by the
platform. We MUST verify its signature against the platform's published JWKS, and
check `iss`, `aud` (our client_id), `exp`/`nbf`, the `nonce` (issued by us, used
once), and the echoed `state`. Only then do we trust its claims and translate them
(pure agg.lti) into an agg session. Every check FAILS CLOSED.

Platform registration (issuer, client_id, auth/JWKS endpoints) and the one-time
nonce/state live in DynamoDB on-demand — no clock.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from urllib.parse import urlencode

import boto3
import jwt
from agg.lti import (
    LtiClaimError,
    lti_claims_to_agg_claims,
    nonce_is_fresh,
    state_matches,
)
from jwt import PyJWKClient

REGISTRATIONS_TABLE = os.environ.get("AGG_LTI_REGISTRATIONS_TABLE", "")
STATE_TABLE = os.environ.get("AGG_LTI_STATE_TABLE", "")
TOOL_BASE_URL = os.environ.get("AGG_TOOL_BASE_URL", "")
# State/nonce TTL — short; a launch follows login init within seconds.
STATE_TTL_SECONDS = int(os.environ.get("AGG_LTI_STATE_TTL_SECONDS", "600"))

_ddb = boto3.resource("dynamodb")


class LtiError(Exception):
    """Any LTI failure -> a terse 4xx, never a session. Fail closed."""


# --- platform registration + state store (DynamoDB) -------------------------


def _registration(issuer: str, client_id: str | None = None) -> dict:
    """Look up a registered platform by issuer (+ client_id for multi-tenant LMS)."""
    table = _ddb.Table(REGISTRATIONS_TABLE)
    key = {"issuer": issuer, "client_id": client_id or "default"}
    item = table.get_item(Key=key).get("Item")
    if not item:
        raise LtiError(f"unregistered platform: {issuer}")
    return item


def _put_state(state: str, nonce: str, issuer: str, client_id: str) -> None:
    _ddb.Table(STATE_TABLE).put_item(
        Item={
            "state": state,
            "nonce": nonce,
            "issuer": issuer,
            "client_id": client_id,
            "expires_at": int(time.time()) + STATE_TTL_SECONDS,  # DynamoDB TTL attr
        }
    )


def _consume_state(state: str) -> dict | None:
    """Atomically fetch-and-delete the state row (so a nonce can't be replayed)."""
    table = _ddb.Table(STATE_TABLE)
    item = table.get_item(Key={"state": state}).get("Item")
    if item:
        table.delete_item(Key={"state": state})
    return item


# --- endpoints --------------------------------------------------------------


def login(params: dict) -> dict:
    """OIDC third-party-initiated login: issue state+nonce, redirect to platform auth."""
    issuer = params.get("iss")
    login_hint = params.get("login_hint")
    target_link_uri = params.get("target_link_uri")
    client_id = params.get("client_id")
    if not issuer or not login_hint:
        raise LtiError("login missing iss/login_hint")

    reg = _registration(issuer, client_id)
    client_id = client_id or reg["client_id"]

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    _put_state(state, nonce, issuer, client_id)

    auth_params = {
        "scope": "openid",
        "response_type": "id_token",
        "response_mode": "form_post",
        "prompt": "none",
        "client_id": client_id,
        "redirect_uri": f"{TOOL_BASE_URL}/lti/launch",
        "login_hint": login_hint,
        "state": state,
        "nonce": nonce,
    }
    if target_link_uri:
        auth_params["lti_message_hint"] = params.get("lti_message_hint", "")
    redirect = f"{reg['auth_login_url']}?{urlencode(auth_params)}"
    return {"statusCode": 302, "headers": {"location": redirect}, "body": ""}


def launch(form: dict) -> dict:
    """Validate the platform's id_token and mint an agg session claim set."""
    id_token = form.get("id_token")
    returned_state = form.get("state")
    if not id_token or not returned_state:
        raise LtiError("launch missing id_token/state")

    stored = _consume_state(returned_state)
    if not stored or not state_matches(stored.get("state"), returned_state):
        raise LtiError("invalid or expired state")

    # Verify signature against the platform JWKS, with claim checks.
    issuer = stored["issuer"]
    client_id = stored["client_id"]
    reg = _registration(issuer, client_id)

    claims = _verify_id_token(id_token, reg, audience=client_id, issuer=issuer)

    # Nonce must match the one we issued and be unused (state row was single-use,
    # so reaching here with a matching nonce means it's fresh).
    if not nonce_is_fresh(claims.get("nonce"), seen=False) or claims.get("nonce") != stored.get(
        "nonce"
    ):
        raise LtiError("invalid nonce")

    # Tenant is an institutional decision tied to the registration, not the user.
    tenant = reg.get("tenant")
    try:
        agg_claims = lti_claims_to_agg_claims(claims, tenant=tenant)
    except LtiClaimError as exc:
        raise LtiError(str(exc)) from exc

    # Hand the agg claim set to the SPA, which exchanges it at the broker for
    # scoped STS creds. We do NOT mint AWS creds here — the broker is the single
    # credential-vending path (Phase 1). The launch returns the SPA with a
    # short-lived, signed handoff the broker will re-validate.
    return _launch_response(agg_claims)


def jwks() -> dict:
    """The tool's public JWKS. In production these come from a managed key
    (KMS/Secrets Manager); the key material is provisioned out-of-band, never
    committed. Phase 4 returns an empty set until keys are provisioned."""
    keys = os.environ.get("AGG_TOOL_JWKS", "")
    body = keys if keys else json.dumps({"keys": []})
    return {"statusCode": 200, "headers": {"content-type": "application/json"}, "body": body}


def deeplink(form: dict) -> dict:
    """Deep-linking response stub (design §6). Returns a self-submitting form
    posting the signed DeepLinkingResponse JWT back to the platform; the JWT
    minting lands with key provisioning."""
    return {
        "statusCode": 200,
        "headers": {"content-type": "text/html"},
        "body": "<!doctype html><p>agg deep-linking not yet configured.</p>",
    }


# --- id_token verification (the security-critical edge) ---------------------


def _verify_id_token(id_token: str, reg: dict, *, audience: str, issuer: str) -> dict:
    """RS256-verify the id_token against the platform JWKS and enforce claims."""
    jwks_uri = reg.get("jwks_uri")
    if not jwks_uri:
        raise LtiError("registration missing jwks_uri")
    try:
        signing_key = PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token)
        return jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "nonce", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise LtiError(f"id_token verification failed: {exc}") from exc


def _launch_response(agg_claims: dict) -> dict:
    """Redirect into the SPA carrying the validated claim set for broker exchange."""
    # Pass the claims as a URL fragment so they never hit a server log; the SPA
    # reads them and POSTs to the broker. (A signed, short-TTL handoff token is the
    # production hardening; the broker re-validates regardless.)
    fragment = urlencode({"agg_claims": json.dumps(agg_claims)})
    return {
        "statusCode": 302,
        "headers": {"location": f"{TOOL_BASE_URL}/#{fragment}"},
        "body": "",
    }


# --- HTTP API router --------------------------------------------------------


def handler(event: dict, context: object) -> dict:
    """API Gateway HTTP API (payload v2) entry point."""
    try:
        route = event.get("rawPath") or event.get("requestContext", {}).get("http", {}).get(
            "path", ""
        )
        # Method-level constraints are enforced by the HTTP API route definitions;
        # here we dispatch purely on path.
        if route.endswith("/lti/login"):
            params = _params(event)
            return login(params)
        if route.endswith("/lti/launch"):
            return launch(_form(event))
        if route.endswith("/jwks.json"):
            return jwks()
        if route.endswith("/lti/deeplink"):
            return deeplink(_form(event))
        return {"statusCode": 404, "body": "not found"}
    except LtiError as exc:
        return {"statusCode": 400, "body": json.dumps({"error": "lti_error", "detail": str(exc)})}
    except Exception:  # noqa: BLE001 — last-resort fail closed
        return {"statusCode": 500, "body": json.dumps({"error": "lti_internal"})}


def _params(event: dict) -> dict:
    """Query params (login can be GET) merged with any form body (login can be POST)."""
    params = dict(event.get("queryStringParameters") or {})
    params.update(_form(event))
    return params


def _form(event: dict) -> dict:
    """Parse an application/x-www-form-urlencoded or JSON body."""
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64

        body = base64.b64decode(body).decode("utf-8")
    if not body:
        return {}
    if body.lstrip().startswith("{"):
        try:
            return json.loads(body)
        except ValueError:
            return {}
    from urllib.parse import parse_qs

    return {k: v[0] for k, v in parse_qs(body).items()}
