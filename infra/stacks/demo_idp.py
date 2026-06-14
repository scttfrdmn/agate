"""Demo OIDC IdP — a throwaway Cognito User Pool (demo-readiness #43).

Real login requires an OIDC IdP whose JWKS the broker/agent verify (SEC-4). For a
demo without a campus IdP, this stack stands up a self-contained Cognito User Pool
that issues real RS256 JWTs. A pre-token trigger maps the demo user's
`custom:affiliation` / `custom:tenant` / `custom:courses` / `custom:grant`
attributes to the top-level `agate` claim names the gateway consumes — so the demo
token scopes exactly like a campus token, with no gateway changes.

This is explicitly a DEMO convenience (design §5 says hook into the campus IdP, not
run a User Pool). It is its own stack so production deployments simply omit it and
point the broker at the real IdP. Cognito User Pools have no idle clock at demo
scale (MAU-billed; the demo's handful of users is free-tier).

Outputs the issuer + JWKS URL + audience to wire into the broker/agent OIDC config.
"""

from __future__ import annotations

import aws_cdk as cdk
from agate.names import HANDLE
from aws_cdk import (
    Stack,
)
from aws_cdk import (
    aws_cognito as cognito,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from constructs import Construct
from infra.assets import LAMBDA_ASSET_EXCLUDES


class DemoIdpStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Pre-token trigger: surface custom:* attrs as agate claim names.
        pretoken_fn = lambda_.Function(
            self,
            "PreToken",
            function_name=f"{HANDLE}-demo-pretoken",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="infra.functions.demo_idp.pretoken.handler",
            code=lambda_.Code.from_asset(".", exclude=LAMBDA_ASSET_EXCLUDES),
            timeout=cdk.Duration.seconds(5),
            description="agate demo IdP — map custom:* attrs to agate claim names",
        )

        # Custom attributes carrying the agate scope. Mutable so a demo operator can
        # flip a user student<->researcher to show tiering live.
        def _attr() -> cognito.StringAttribute:
            return cognito.StringAttribute(mutable=True, max_len=256, min_len=0)

        pool = cognito.UserPool(
            self,
            "DemoUserPool",
            user_pool_name=f"{HANDLE}-demo",
            self_sign_up_enabled=False,  # operator-created demo users only
            sign_in_aliases=cognito.SignInAliases(email=True, username=True),
            custom_attributes={
                "affiliation": _attr(),
                "tenant": _attr(),
                "courses": _attr(),
                "grant": _attr(),
            },
            lambda_triggers=cognito.UserPoolTriggers(pre_token_generation=pretoken_fn),
            removal_policy=cdk.RemovalPolicy.DESTROY,  # throwaway demo pool
        )

        # A hosted-UI domain so the pool has a discovery/JWKS endpoint + login page.
        pool.add_domain(
            "DemoDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"{HANDLE}-demo-{self.account}"
            ),
        )

        # OIDC client for the SPA (the `aud` the broker/agent pin).
        client = pool.add_client(
            "SpaClient",
            user_pool_client_name=f"{HANDLE}-spa",
            generate_secret=False,  # public SPA client (PKCE)
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True, implicit_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.PROFILE],
            ),
            id_token_validity=cdk.Duration.hours(1),
        )

        issuer = f"https://cognito-idp.{self.region}.amazonaws.com/{pool.user_pool_id}"

        # --- Outputs: the OIDC config to set on the broker/agent stacks ----
        cdk.CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        cdk.CfnOutput(self, "OidcIssuer", value=issuer)
        cdk.CfnOutput(self, "OidcJwksUrl", value=f"{issuer}/.well-known/jwks.json")
        cdk.CfnOutput(self, "OidcAudience", value=client.user_pool_client_id)

        self.user_pool = pool
        self.client = client
