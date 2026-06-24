"""CDK synth assertions for the IdentityStack CORS wiring. No deploy.

The SPA SigV4-signs the retrieval HTTP API; the signer emits an x-amz-content-sha256
header, so the preflight must allow it or the browser blocks the request ("Failed to
fetch") before it is ever sent. This locks that header into the retrieval CORS so the
regression (a signed call's preflight rejected) can't return.
"""

from __future__ import annotations

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk import assertions  # noqa: E402
from infra.stacks.identity import IdentityStack  # noqa: E402

_ENV = cdk.Environment(account="111122223333", region="us-east-1")


def _template():
    app = cdk.App()
    return assertions.Template.from_stack(IdentityStack(app, "agate-identity-synth", env=_ENV))


def test_retrieval_cors_allows_sigv4_content_sha256_header():
    t = _template()
    apis = t.find_resources("AWS::ApiGatewayV2::Api")
    # The retrieval API is the SigV4-signed one (it has a CORS config allowing the
    # authorization header); the plain broker API only allows content-type.
    signed = [
        a
        for a in apis.values()
        if "authorization"
        in (a["Properties"].get("CorsConfiguration") or {}).get("AllowHeaders", [])
    ]
    assert signed, "expected a SigV4-signed HTTP API with an authorization CORS header"
    for api in signed:
        headers = api["Properties"]["CorsConfiguration"]["AllowHeaders"]
        assert "x-amz-content-sha256" in headers, (
            "the SigV4 signer emits x-amz-content-sha256; the preflight must allow it"
        )
