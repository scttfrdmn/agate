"""Shared real JWT/JWKS verification (closes SEC-4).

ONE verifier for every entry point that trusts a campus-IdP token — the broker, the
Tier 1 choke point, and the agent path. Each had (or inherited) a Phase-1 placeholder
that accepted any JSON as trusted claims; consolidating real verification here means
the signature/issuer/audience/expiry checks can't drift or be forgotten in one place.

RS256 against the IdP's published JWKS (the same proven pattern the LTI handler uses):
- signature verified against the key the token's `kid` selects from the JWKS,
- `iss` and `aud` pinned to the configured values,
- `exp`/`iat` enforced, `sub` required,
- algorithm pinned to RS256 (no `alg=none`, no HS/RS confusion — an asymmetric public
  key is supplied, so a forged HS256 token can't validate against it either).

The JWKS client is injectable so tests verify real RS256 tokens with an in-test
keypair and no network. Any failure raises `TokenError` — callers fail closed.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

import jwt
from jwt import PyJWKClient


class TokenError(Exception):
    """Token failed verification. Callers MUST treat this as a hard deny."""


class SigningKeyResolver(Protocol):
    """Resolves the verification key for a token (the JWKS client in production;
    a fixed key in tests)."""

    def get_signing_key_from_jwt(self, token: str) -> Any: ...


# Cache one PyJWKClient per JWKS URL (it caches keys internally, refreshing on
# unknown kid). Module-level so warm Lambda invocations reuse fetched keys.
_jwks_clients: dict[str, PyJWKClient] = {}


def _client_for(jwks_url: str) -> PyJWKClient:
    client = _jwks_clients.get(jwks_url)
    if client is None:
        client = PyJWKClient(jwks_url)
        _jwks_clients[jwks_url] = client
    return client


def verify_token(
    token: str,
    *,
    jwks_url: str,
    issuer: str,
    audience: str,
    require: tuple[str, ...] = ("exp", "iat", "sub", "aud", "iss"),
    resolver: SigningKeyResolver | None = None,
) -> dict[str, Any]:
    """Verify a campus-IdP JWT and return its claims, or raise TokenError.

    `resolver` overrides the JWKS lookup (tests inject an in-test key). In
    production it defaults to the cached PyJWKClient for `jwks_url`.
    """
    if not token:
        raise TokenError("no token presented")
    if not jwks_url or not issuer or not audience:
        raise TokenError("verifier misconfigured: jwks_url/issuer/audience required")

    keys = resolver if resolver is not None else _client_for(jwks_url)
    try:
        signing_key = keys.get_signing_key_from_jwt(token)
        key = getattr(signing_key, "key", signing_key)
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],  # pin: no alg=none, no HS confusion
            audience=audience,
            issuer=issuer,
            options={"require": list(require)},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(f"token verification failed: {exc}") from exc


def config_from_env(prefix: str = "AGATE") -> dict[str, str]:
    """Read the verifier config (JWKS URL / issuer / audience) from env vars set at
    deploy time, e.g. AGATE_OIDC_JWKS_URL / AGATE_OIDC_ISSUER / AGATE_OIDC_AUDIENCE.
    Missing values are returned empty; verify_token then fails closed."""
    return {
        "jwks_url": os.environ.get(f"{prefix}_OIDC_JWKS_URL", ""),
        "issuer": os.environ.get(f"{prefix}_OIDC_ISSUER", ""),
        "audience": os.environ.get(f"{prefix}_OIDC_AUDIENCE", ""),
    }
